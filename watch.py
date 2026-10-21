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
import datetime as dt
import json
import math
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
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


class RecaptchaBlocked(RuntimeError):
    """Liveforce's login endpoint refused the attempt on a reCAPTCHA score.

    Nothing about the credentials is wrong, and the app shows no error at all, so
    without this the only symptom is a silent 90s stall. Worth retrying soon.
    """


class LoginRejected(RuntimeError):
    """Liveforce refused the credentials.

    Retrying cannot fix this and Liveforce locks accounts after repeated failures,
    so a resident watcher must stop rather than hammer the login form.
    """


def _settle_after_login(page, captured: list, timeout: float, blocked: list | None = None) -> None:
    """Wait for the job board, translating the known dead ends into clear errors."""
    deadline = time.monotonic() + timeout
    nudged = False
    while time.monotonic() < deadline:
        if captured:
            return
        if blocked:
            raise RecaptchaBlocked(f"login blocked before the password was checked: {blocked[0]}")
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
                raise LoginRejected(f"Liveforce rejected the login: {message!r} - check LF_EMAIL / LF_PASSWORD")
        if "/job-board" in url:
            return
        if not nudged and "/login" not in url:
            # Crew with several agencies land on the account chooser instead.
            log(f"landed on {url.replace(APP_URL, '')}, going to the job board")
            page.goto(JOB_BOARD_URL, wait_until="domcontentloaded")
            nudged = True
        page.wait_for_timeout(1_000)
    # Without the page's own words a stall here is undiagnosable from a log alone.
    # Only the login screen is quoted, which carries no job details.
    said = ""
    if "/login" in page.url:
        first = next((l.strip() for l in page.inner_text("body").splitlines() if l.strip()), "")
        said = f" - the login page says {first!r}" if first else ""
    raise RuntimeError(f"login did not reach the job board within {timeout:.0f}s (stuck on {page.url}){said}")


def _agencies(page) -> list[tuple[int, str]]:
    """Every agency the crew is connected to, as (id, name).

    Read off the /accounts page logos, whose URL carries the id
    (.../logos/151/thumb-x.png) and whose title carries the name. An agency with
    an incomplete profile still has a readable job board, so it is included.
    """
    page.goto(f"{APP_URL}/accounts", wait_until="domcontentloaded")
    page.wait_for_selector("img[src*='/logos/']")
    found: list[tuple[int, str]] = []
    for img in page.locator("img[src*='/logos/']").all():
        src = img.get_attribute("src") or ""
        ident = src.partition("/logos/")[2].partition("/")[0]
        if ident.isdigit() and not any(i == int(ident) for i, _ in found):
            found.append((int(ident), (img.get_attribute("title") or f"agency {ident}").strip()))
    return found


def _capture_once(email: str, password: str) -> list[dict]:
    """One login attempt; capture the live JobsAvailable calls for every agency."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    with sync_playwright() as p:
        headful = os.environ.get("LF_HEADFUL") == "1"
        browser = p.chromium.launch(
            headless=not headful,
            slow_mo=500 if headful else 0,
            # Chromium advertises itself as automated, which reCAPTCHA scores down.
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 412, "height": 915},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
            ),
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.set_default_timeout(60_000)

        blocked: list[str] = []

        def on_response(resp):
            if "crew-login" not in resp.url:
                return
            if resp.status == 403:
                try:
                    blocked.append(resp.text()[:200])
                except Exception:
                    blocked.append(f"HTTP {resp.status}")

        page.on("response", on_response)

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
        _settle_after_login(page, captured, timeout=180 if headful else 90, blocked=blocked)

        agencies = _agencies(page)
        log(f"connected to {len(agencies)} agency board(s): " + ", ".join(n for _, n in agencies))
        for company_id, name in agencies:
            before = len(captured)
            # ?agencyId= is how the app's own Switch button loads another board.
            page.goto(f"{JOB_BOARD_URL}?agencyId={company_id}", wait_until="domcontentloaded")
            for _ in range(3):
                page.wait_for_timeout(5_000)
                if len(captured) > before:
                    break
            for spec in captured[before:]:
                spec["company_id"] = company_id
                spec["agency"] = name
            if len(captured) == before:
                log(f"warning: no request captured for {name} - its shifts will not be watched")
        ended_on = page.url
        browser.close()

    # The board re-queries on load, so the same agency can appear more than once.
    unique: list[dict] = []
    for spec in captured:
        if spec.get("company_id") and not any(u["company_id"] == spec["company_id"] for u in unique):
            unique.append(spec)
    if not unique:
        raise RuntimeError(f"logged in but captured no JobsAvailable request (ended on {ended_on})")
    log(f"captured {len(unique)} board(s): " + ", ".join(u["agency"] for u in unique))
    return unique


def capture_requests(email: str, password: str, attempts: int = 4) -> list[dict]:
    """Log in, retrying a reCAPTCHA refusal.

    The score varies between attempts, so a refusal is worth retrying straight
    away rather than waiting out the whole poll interval. Credential rejections
    are not retried - they would only burn attempts toward an account lock.
    """
    for attempt in range(1, attempts + 1):
        try:
            return _capture_once(email, password)
        except RecaptchaBlocked as exc:
            if attempt == attempts:
                raise
            delay = 15 * attempt
            log(f"login attempt {attempt}/{attempts} blocked by reCAPTCHA, retrying in {delay}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


def post(req_spec: dict) -> dict:
    req = urllib.request.Request(
        req_spec["url"],
        data=req_spec["body"].encode("utf8"),
        headers={"content-type": "application/json", **req_spec["headers"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


SYDNEY = (-33.8688, 151.2093)
ORIGIN = (float(os.environ.get("LF_LAT", SYDNEY[0])), float(os.environ.get("LF_LON", SYDNEY[1])))
RADIUS_KM = float(os.environ.get("LF_RADIUS_KM", 100))


def _km_from_origin(lat, lon) -> float | None:
    """Great-circle distance from ORIGIN, or None when the job has no location."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not lat and not lon:
        return None
    rlat1, rlat2 = math.radians(ORIGIN[0]), math.radians(lat)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon - ORIGIN[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _deref(value, table: list):
    """Resolve Liveforce's "$12" string-table references (the JobCompressed format)."""
    if isinstance(value, str) and value.startswith("$") and value[1:].isdigit():
        index = int(value[1:])
        return table[index] if index < len(table) else ""
    return value


def _when(start_iso: str, timezone: str) -> str:
    if not start_iso:
        return "date unknown"
    try:
        moment = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return str(start_iso)
    try:
        moment = moment.astimezone(zoneinfo.ZoneInfo(timezone or "Australia/Sydney"))
        return moment.strftime("%a %d %b %H:%M")
    except Exception:
        # No tz database on this host; say so rather than imply local time.
        return moment.strftime("%a %d %b %H:%M UTC")


def post_board(spec: dict, max_pages: int = 25) -> dict[str, str]:
    """Every shift on one agency board, following the cursor to the last page.

    The captured request asks for one page of 100. A board larger than that churns
    roles in and out of page one, which looks like the total changing while nothing
    is ever "new". The response's "c" field is the cursor for the next page, null on
    the last one. Each page carries its own string table, so pages are decoded
    separately and then merged.
    """
    body = json.loads(spec["body"])
    shifts: dict[str, str] = {}
    cursor = None
    for page_number in range(1, max_pages + 1):
        body["variables"]["cursor"] = cursor
        page = post({**spec, "body": json.dumps(body)})
        shifts.update(extract_shifts(page))
        cursor = ((page.get("data") or {}).get("jobsAvailable") or {}).get("c")
        if not cursor:
            return shifts
    log(f"warning: stopped paging {spec.get('agency')} after {max_pages} pages")
    return shifts


def extract_shifts(payload: dict) -> dict[str, str]:
    """Map role id -> human label, keeping only roles within RADIUS_KM of ORIGIN.

    A role is the thing you actually apply for, so it -- not the job -- is the unit
    of notification. Jobs carry the client, venue and coordinates; roles carry the
    name and start time, and link back by job id.
    """
    board = (payload.get("data") or {}).get("jobsAvailable")
    if not isinstance(board, dict) or "j" not in board or "r" not in board:
        # Returning {} here would look exactly like "no shifts" forever.
        raise RuntimeError(f"unexpected JobsAvailable response shape: {str(payload)[:200]}")

    table = board.get("v") or []
    jobs = {}
    for job in board["j"]:
        jobs[job.get("i")] = {
            "client": str(_deref(job.get("t"), table) or "").strip() or "unknown client",
            "venue": str(_deref(job.get("l"), table) or "").strip(),
            "timezone": str(_deref(job.get("n"), table) or "").strip(),
            "km": _km_from_origin(job.get("x"), job.get("y")),
        }

    shifts: dict[str, str] = {}
    for role in board["r"]:
        job = jobs.get(role.get("j"))
        if job is None:
            continue
        if job["km"] is not None and job["km"] > RADIUS_KM:
            continue
        where = job["venue"] or "location unknown"
        if job["km"] is None:
            where += " (no coordinates)"
        else:
            where += f" ({job['km']:.0f}km)"
        name = str(_deref(role.get("n"), table) or "").strip() or "role"
        shifts[f"r:{role.get('i')}"] = " | ".join(
            (job["client"], name, _when(role.get("s"), job["timezone"]), where)
        )
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
    if "change-me" in topic.lower():
        # ntfy accepts a push to any topic, so a leftover placeholder delivers
        # nowhere and still reports success. Refuse it instead.
        raise RuntimeError(
            f"NTFY_TOPIC is still the placeholder {topic!r} - set it to the topic "
            "your phone is subscribed to in the ntfy app"
        )
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

    def poll() -> dict[str, str]:
        # Keys carry the agency: two agencies can issue the same job id, and an
        # unprefixed key would let the second one hide behind the first.
        out: dict[str, str] = {}
        for spec in state["requests"]:
            agency = spec.get("agency") or str(spec.get("company_id", ""))
            for key, label in post_board(spec).items():
                out[f"{agency}/{key}"] = f"{agency}: {label}" if agency else label
        return out

    try:
        shifts = poll()
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        log(f"token rejected ({exc.code}), logging in again")
        state["requests"] = capture_requests(email, password)
        save_state(state)
        shifts = poll()

    if any("/" not in key for key in state["seen"]):
        log("state predates per-agency keys, re-baselining without notifying")
        state["seen"] = []
        quiet_first_run = True

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
    if "example.com" in email:
        # Otherwise this only shows up as "Incorrect email or password".
        print(f"LF_EMAIL is still the placeholder {email!r}", file=sys.stderr)
        return 2

    state = load_state()
    deadline = time.monotonic() + args.max_runtime if args.max_runtime else None
    failures = 0
    while True:
        try:
            state = check_once(state, email, password, quiet_first_run=not args.notify_first_run)
            failures = 0
        except LoginRejected as exc:
            log(f"stopping: {exc}")
            notify("Liveforce watcher stopped", str(exc)[:400])
            return 2
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
