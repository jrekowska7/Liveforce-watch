#!/usr/bin/env python3
"""Poll the Liveforce crew Job Board and push a phone notification on new shifts.

One-shot by default (run it from cron / Task Scheduler), or --loop to stay resident.

The GraphQL request is not hardcoded: on first run (or after the token expires) a
headless browser logs in, and the app's own JobsAvailable request is captured --
URL, headers and body -- then replayed directly on later polls. Schema changes on
Liveforce's side are picked up on the next login instead of breaking the poller.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_URL = "https://app.liveforce.co"
JOB_BOARD_URL = f"{APP_URL}/job-board"
STATE_FILE = Path(os.environ.get("LF_STATE", Path(__file__).with_name("state.json")))


def load_dotenv() -> None:
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text("utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf8"))
    return {"requests": [], "seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), "utf8")


def capture_requests(email: str, password: str) -> list[dict]:
    """Log in with a headless browser and capture the live JobsAvailable calls."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    with sync_playwright() as p:
        headful = os.environ.get("LF_HEADFUL") == "1"
        browser = p.chromium.launch(headless=not headful, slow_mo=500 if headful else 0)
        page = browser.new_page()

        def on_request(req):
            if req.method != "POST" or "gql" not in req.url:
                return
            body = req.post_data or ""
            if "JobsAvailable" not in body:
                return
            headers = {
                k: v
                for k, v in req.headers.items()
                if k.lower() in ("authorization", "content-type", "accept", "user-agent")
            }
            if not any(c["body"] == body for c in captured):
                captured.append({"url": req.url, "headers": headers, "body": body})

        page.on("request", on_request)
        page.goto(f"{APP_URL}/login", wait_until="networkidle")
        # The login form arrives with a lazy-loaded chunk, so wait for it rather
        # than racing it.
        page.wait_for_selector("#email", timeout=60_000)
        page.fill("#email", email)
        page.fill("#password", password)
        try:
            page.get_by_role("checkbox").first.check(timeout=2_000)  # "Remember me?" - longer session
        except Exception:
            pass
        page.get_by_role("button", name="Login").click()
        # A generous timeout so LF_HEADFUL=1 leaves room to finish 2FA by hand.
        page.wait_for_url("**/job-board**", timeout=180_000 if headful else 60_000)
        page.wait_for_load_state("networkidle")
        if not captured:
            page.reload(wait_until="networkidle")
        page.wait_for_timeout(3_000)
        browser.close()

    if not captured:
        raise RuntimeError("logged in but captured no JobsAvailable request")
    log(f"captured {len(captured)} JobsAvailable request(s)")
    return captured


def post(req_spec: dict) -> dict:
    req = urllib.request.Request(
        req_spec["url"],
        data=req_spec["body"].encode("utf8"),
        headers={"content-type": "application/json", **req_spec["headers"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def extract_shifts(payload: dict) -> dict[str, str]:
    """Map a stable shift key -> human label.

    Liveforce minifies field names (j = jobs, r = roles, i = id), so instead of
    trusting those letters this walks the response for objects carrying an id and
    builds the label from their own string fields.
    """
    shifts: dict[str, str] = {}

    def walk(node, parent_key=""):
        if isinstance(node, dict):
            ident = node.get("i") or node.get("id")
            if ident is not None and parent_key in ("j", "r", "jobs", "roles"):
                texts = [
                    v.strip()
                    for k, v in sorted(node.items())
                    if isinstance(v, str) and k not in ("i", "id") and v.strip() and not v.strip().isdigit()
                ]
                label = " | ".join(texts[:6]) or f"id {ident}"
                shifts[f"{parent_key}:{ident}"] = label
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)

    walk(payload.get("data") or {})
    return shifts


def notify(title: str, body: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    pushover_token = os.environ.get("PUSHOVER_TOKEN")
    if topic:
        url = f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}"
        req = urllib.request.Request(
            url,
            data=body.encode("utf8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "briefcase",
                "Click": JOB_BOARD_URL,
            },
        )
        urllib.request.urlopen(req, timeout=20).read()
    elif pushover_token:
        data = urllib.parse.urlencode(
            {
                "token": pushover_token,
                "user": os.environ["PUSHOVER_USER"],
                "title": title,
                "message": body,
                "url": JOB_BOARD_URL,
                "priority": 1,
            }
        ).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", data, timeout=20).read()
    else:
        log("no NTFY_TOPIC / PUSHOVER_TOKEN set, notification skipped")
        return
    log(f"notified: {title}")


def check_once(state: dict, email: str, password: str, quiet_first_run: bool) -> dict:
    if not state["requests"]:
        state["requests"] = capture_requests(email, password)
        save_state(state)

    shifts: dict[str, str] = {}
    try:
        for spec in state["requests"]:
            shifts.update(extract_shifts(post(spec)))
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        log(f"token rejected ({exc.code}), logging in again")
        state["requests"] = capture_requests(email, password)
        save_state(state)
        for spec in state["requests"]:
            shifts.update(extract_shifts(post(spec)))

    seen = set(state["seen"])
    new = [k for k in shifts if k not in seen]

    if new and (seen or not quiet_first_run):
        lines = [shifts[k] for k in new][:10]
        extra = f"\n+{len(new) - len(lines)} more" if len(new) > len(lines) else ""
        notify(f"{len(new)} new Liveforce shift(s)", "\n".join(lines) + extra)
    elif new:
        log(f"first run, baselining {len(new)} existing shifts without notifying")
    else:
        log(f"no new shifts ({len(shifts)} on board)")

    state["seen"] = sorted(seen | set(shifts))
    save_state(state)
    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="keep running instead of one-shot")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("LF_INTERVAL", 120)))
    ap.add_argument(
        "--max-runtime",
        type=int,
        default=int(os.environ.get("LF_MAX_RUNTIME", 0)),
        help="with --loop, exit cleanly after this many seconds (0 = never)",
    )
    ap.add_argument("--notify-first-run", action="store_true", help="also notify about shifts already on the board")
    ap.add_argument("--test-notification", action="store_true")
    args = ap.parse_args()
    load_dotenv()

    if args.test_notification:
        notify("Liveforce watcher test", "If you can read this, push delivery works.")
        return 0

    email = os.environ.get("LF_EMAIL")
    password = os.environ.get("LF_PASSWORD")
    if not email or not password:
        print("set LF_EMAIL and LF_PASSWORD", file=sys.stderr)
        return 2

    state = load_state()
    deadline = time.monotonic() + args.max_runtime if args.max_runtime else None
    while True:
        try:
            state = check_once(state, email, password, quiet_first_run=not args.notify_first_run)
        except Exception as exc:  # keep a resident watcher alive across transient failures
            log(f"check failed: {type(exc).__name__}: {exc}")
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        if deadline and time.monotonic() + args.interval > deadline:
            log("max runtime reached, exiting for the next run to take over")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
