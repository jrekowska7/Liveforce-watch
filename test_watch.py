"""Zero-dependency tests: run with `python test_watch.py`.

The browser login is never exercised here -- it needs real credentials and a real
Liveforce session. Everything around it is, including the rule that matters most:
a rejected password must stop the watcher instead of being retried.
"""
import json
import sys
import tempfile
from pathlib import Path

import watch

FAILED = []


def check(name, condition):
    print(f"{'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        FAILED.append(name)


def fresh_state_file():
    watch.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
    return watch.STATE_FILE


# --- ntfy_topic -------------------------------------------------------------
check("bare topic passes through", watch.ntfy_topic("lf-shifts-abc123") == "lf-shifts-abc123")
check("full url is reduced to the topic", watch.ntfy_topic("https://ntfy.sh/lf-shifts-abc123") == "lf-shifts-abc123")
check("host prefix is stripped", watch.ntfy_topic("ntfy.sh/lf-shifts-abc123") == "lf-shifts-abc123")
for bad in ("my topic", "lf.shifts", "", "a" * 65):
    try:
        watch.ntfy_topic(bad)
        check(f"rejects {bad!r}", False)
    except RuntimeError:
        check(f"rejects {bad!r}", True)


# --- extract_shifts ---------------------------------------------------------
payload = {"data": {"jobsAvailable": {"j": [
    {"i": "77", "n": "Bar Staff", "d": "2026-11-02", "v": "Sydney Olympic Park",
     "r": [{"i": "301", "n": "Bartender"}]},
    {"i": "78", "n": "Usher", "d": "2026-11-03"},
]}}}
shifts = watch.extract_shifts(payload)
check("every job and role is keyed", set(shifts) == {"j:77", "j:78", "r:301"})
check("label is built from the job's own strings", "Bar Staff" in shifts["j:77"])
check("ids are not used as labels", shifts["j:78"] != "id 78")
check("empty payload is not an error", watch.extract_shifts({}) == {})


# --- check_once: baseline, notify, no repeats -------------------------------
fresh_state_file()
pushes = []
watch.notify = lambda title, body: pushes.append((title, body))
watch.capture_requests = lambda e, p: [{"url": "u", "headers": {}, "body": "b"}]
board = dict(payload)
watch.post = lambda spec: board

state = watch.check_once({"requests": [], "seen": []}, "e", "p", quiet_first_run=True)
check("first run notifies nothing", pushes == [])
check("first run records the board", len(state["seen"]) == 3)

state = watch.check_once(state, "e", "p", quiet_first_run=True)
check("an unchanged board is silent", pushes == [])

board = {"data": {"jobsAvailable": {"j": [{"i": "99", "n": "Steward", "d": "2026-11-09"}]}}}
state = watch.check_once(state, "e", "p", quiet_first_run=True)
check("a new shift pushes once", len(pushes) == 1)
check("the push names the new shift", "Steward" in pushes[0][1])
check("the push counts only what is new", pushes[0][0].startswith("1 new"))

watch.check_once(state, "e", "p", quiet_first_run=True)
check("the same shift is not pushed twice", len(pushes) == 1)
check("state survives a restart", json.loads(watch.STATE_FILE.read_text("utf8"))["seen"])


# --- main(): fatal vs transient --------------------------------------------
import os
os.environ["LF_EMAIL"], os.environ["LF_PASSWORD"] = "e", "p"
fresh_state_file()


def run_main(argv, failure):
    calls = []

    def fake_check_once(*a, **k):
        calls.append(1)
        raise failure

    watch.check_once = fake_check_once
    pushes.clear()
    sys.argv = ["watch.py"] + argv
    return watch.main(), calls, pushes


rejected = watch.LoginRejected("Liveforce rejected the login: 'Incorrect email or password'")
code, calls, pushes_seen = run_main(["--loop", "--interval", "0"], rejected)
check("a rejected password exits non-zero", code == 2)
check("a rejected password is tried exactly once, never looped", len(calls) == 1)
check("a rejected password is announced", len(pushes_seen) == 1 and "stopped" in pushes_seen[0][0])

code, calls, pushes_seen = run_main([], RuntimeError("socket timeout"))
check("a one-shot transient failure exits 1", code == 1)
check("a one-shot failure does not push", pushes_seen == [])

print()
if FAILED:
    sys.exit(f"{len(FAILED)} test(s) failed: {FAILED}")
print("ALL TESTS PASS")
