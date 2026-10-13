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
import re
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


LOGIN_ERRORS = (
    "Incorrect email or password",
    "Your account has been locked",
    "too many login attempts",
)


def _settle_after_login(page, captured: list, timeout: float) -> None:
    """Wait for the job board, translating the known dead ends into clear errors."""
    deadline = time.monotonic() + timeout
    nudged = False
    while time.monotonic() < deadline:
        if captured:
            return
        url = page.url
        if "/mfa-verify" in url:
            raise RuntimeError(
                "account has two-factor auth enabled - a headless login cannot pass it; "
                "run once with LF_HEADFUL=1 to complete it by hand"
            )
        # Deliberately not logged: page text can contain job details.
        body = page.inner_text("body")
        for message in LOGIN_ERRORS:
            if message in body:
                raise RuntimeError(f"Liveforce rejected the login: {message!r} - check LF_EMAIL / LF_PASSWORD")
        if "/job-board" in url:
            return
        if not nudged and "/login" not in url:
            # Crew with several agencies land on the account chooser instead.
            log(f"landed on {url.replace(APP_URL, '')}, going to the job board")
            page.goto(JOB_BOARD_URL, wait_until="domcontentloaded")
            nudged = True
        page.wait_for_timeout(1_000)
    raise RuntimeError(f"login did not reach the job board within {timeout:.0f}s (stuck on {page.url})")


def capture_requests(email: str, password: str) -> list[dict]:
    """Log in with a headless browser and capture the live JobsAvailable calls."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    with sync_playwright() as p:
        headful = os.environ.get("LF_HEADFUL") == "1"
        browser = p.chromium.launch(headless=not headful, slow_mo=500 if headful else 0)
        page = browser.new_page()
        page.set_default_timeout(60_000)

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
        # Never wait for "networkidle" here: the app holds websockets open for chat
        # and GraphQL subscriptions, so the network never goes quiet.
        page.goto(f"{APP_URL}/login", wait_until="domcontentloaded")
        # The login form arrives with a lazy-loaded chunk, so wait for it rather
        # than racing it.
        page.wait_for_selector("#email")
        page.fill("#email", email)
        page.fill("#password", password)
        try:
            page.get_by_role("checkbox").first.check(timeout=2_000)  # "Remember me?" - longer session
        except Exception:
            pass
        page.get_by_role("button", name="Login").click()
        _settle_after_login(page, captured, timeout=180 if headful else 90)

        # The board may render from cache without re-querying; a reload forces it.
        for _ in range(2):
            if captured:
                break
            page.goto(JOB_BOARD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(8_000)
        ended_on = page.url
        browser.close()

    if not captured:
        raise RuntimeError(f"logged in but captured no JobsAvailable request (ended on {ended_on})")
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


TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def ntfy_topic(raw: str) -> str:
    """Normalise NTFY_TOPIC, tolerating a pasted URL and rejecting junk early.

    A bad topic otherwise surfaces as a raw urllib traceback from inside notify(),
    which says nothing useful about the actual mistake.
    """
    topic = raw.strip().strip("/")
    if "/" in topic:  # someone pasted https://ntfy.sh/my-topic instead of my-topic
        topic = topic.rsplit("/", 1)[-1]
    if not TOPIC_PATTERN.match(topic):
        raise RuntimeError(
            "NTFY_TOPIC must be 1-64 characters, letters/digits/'-'/'_' only "
            "(no spaces, no '.', no URL) - it is the topic name, not a link"
        )
    return topic


def notify(title: str, body: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    pushover_token = os.environ.get("PUSHOVER_TOKEN")
    if topic:
        url = f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{ntfy_topic(topic)}"
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
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf8", "replace")
            raise RuntimeError(f"ntfy refused the push (HTTP {exc.code}): {detail}") from None
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
    failures = 0
    while True:
        try:
            state = check_once(state, email, password, quiet_first_run=not args.notify_first_run)
            failures = 0
        except Exception as exc:  # keep a resident watcher alive across transient failures
            log(f"check failed: {type(exc).__name__}: {exc}")
            failures += 1
            # Nobody reads logs on a headless host, so say it out loud once.
            if failures == 3:
                notify("Liveforce watcher is stuck", f"{type(exc).__name__}: {exc}"[:400])
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
