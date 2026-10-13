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
for bad in ("my topic", "lf.shifts", "", "a" * 65, "lf-shifts-change-me-7f3a9c"):
    try:
        watch.ntfy_topic(bad)
        check(f"rejects {bad!r}", False)
    except RuntimeError:
        check(f"rejects {bad!r}", True)


# --- extract_shifts: real "JobCompressed" payload -------------------------
def board(jobs, roles, strings):
    return {"data": {"jobsAvailable": {"v": strings, "j": jobs, "r": roles}}}


SYD = (-33.8688, 151.2093)          # origin
FAR = (-37.8246, 144.9554)          # Melbourne, ~700km
STRINGS = ["", "Bankwest", "Westin Hotel", "Australia/Sydney", "Brand Ambassador", "V8 Supercars"]
one = board(
    [{"i": 500, "t": "$1", "l": "$2", "n": "$3", "x": SYD[0], "y": SYD[1]}],
    [{"i": 900, "j": 500, "n": "$4", "s": "2026-11-02T04:00:00Z"}],
    STRINGS,
)
shifts = watch.extract_shifts(one)
check("a role becomes one shift", list(shifts) == ["r:900"])
label = shifts["r:900"]
check("the label leads with the client", label.startswith("Bankwest"))
check("the label names the role", "Brand Ambassador" in label)
check("the label names the venue", "Westin Hotel" in label)
check("the label shows the distance", "(0km)" in label)
check("the label shows a local date", "Mon 02 Nov" in label)

far = board(
    [{"i": 501, "t": "$5", "l": "$2", "n": "$3", "x": FAR[0], "y": FAR[1]}],
    [{"i": 901, "j": 501, "n": "$4", "s": "2026-11-02T04:00:00Z"}],
    STRINGS,
)
check("a job beyond the radius is dropped", watch.extract_shifts(far) == {})

nowhere = board(
    [{"i": 502, "t": "$1", "l": "", "n": "$3", "x": 0, "y": 0}],
    [{"i": 902, "j": 502, "n": "$4", "s": "2026-11-02T04:00:00Z"}],
    STRINGS,
)
kept = watch.extract_shifts(nowhere)
check("a job without coordinates is kept, not hidden", list(kept) == ["r:902"])
check("a job without coordinates says so", "no coordinates" in kept["r:902"])

orphan = board([], [{"i": 903, "j": 999, "n": "$4", "s": "2026-11-02T04:00:00Z"}], STRINGS)
check("a role whose job is absent is skipped", watch.extract_shifts(orphan) == {})

for bad in ({}, {"data": {}}, {"data": {"jobsAvailable": {"j": []}}}):
    try:
        watch.extract_shifts(bad)
        check(f"a broken payload raises, not returns empty ({bad})", False)
    except RuntimeError:
        check(f"a broken payload raises, not returns empty ({bad})", True)


# --- check_once: baseline, notify, no repeats -------------------------------
fresh_state_file()
pushes = []
watch.notify = lambda title, body: pushes.append((title, body))
AGENCY = {"url": "u", "headers": {}, "body": "b", "company_id": 151, "agency": "Mash Staffing Australia"}
watch.capture_requests = lambda e, p: [dict(AGENCY)]
watch.post = lambda spec: one

state = watch.check_once({"requests": [], "seen": []}, "e", "p", quiet_first_run=True)
check("first run notifies nothing", pushes == [])
check("first run records the board", len(state["seen"]) == 1)

state = watch.check_once(state, "e", "p", quiet_first_run=True)
check("an unchanged board is silent", pushes == [])

watch.post = lambda spec: board(
    [{"i": 503, "t": "$5", "l": "$2", "n": "$3", "x": SYD[0], "y": SYD[1]}],
    [{"i": 904, "j": 503, "n": "$4", "s": "2026-11-09T04:00:00Z"}],
    STRINGS,
)
state = watch.check_once(state, "e", "p", quiet_first_run=True)
check("a new shift pushes once", len(pushes) == 1)
check("the push names the new shift", "V8 Supercars" in pushes[0][1])
check("the push counts only what is new", pushes[0][0].startswith("1 new"))
check("the push names the agency", "Mash Staffing Australia" in pushes[0][1])

watch.check_once(state, "e", "p", quiet_first_run=True)
check("the same shift is not pushed twice", len(pushes) == 1)
check("state survives a restart", json.loads(watch.STATE_FILE.read_text("utf8"))["seen"])


# --- two agencies ------------------------------------------------------------
fresh_state_file()
pushes.clear()
a = {"url": "u", "headers": {}, "body": "b", "company_id": 151, "agency": "Mash"}
bb = {"url": "u", "headers": {}, "body": "b", "company_id": 537, "agency": "Maverick"}
# Same job id on both boards: the collision this keying exists to prevent.
same_id = one  # identical role id 900 served by both agencies
watch.post = lambda spec: same_id
state = watch.check_once({"requests": [a, bb], "seen": []}, "e", "p", quiet_first_run=True)
check("both agencies are polled", len(state["seen"]) == 2)
check("a shared job id is kept apart", set(state["seen"]) == {"Mash/r:900", "Maverick/r:900"})
check("baselining two boards is silent", pushes == [])

watch.post = lambda spec: board(
    [{"i": 500, "t": "$1", "l": "$2", "n": "$3", "x": SYD[0], "y": SYD[1]}],
    [{"i": 900, "j": 500, "n": "$4", "s": "2026-11-02T04:00:00Z"},
     {"i": 905, "j": 500, "n": "$4", "s": "2026-11-05T04:00:00Z"}],
    STRINGS,
)
state = watch.check_once(state, "e", "p", quiet_first_run=True)
check("a new shift on each board pushes once", len(pushes) == 1 and pushes[0][0].startswith("2 new"))
check("the push distinguishes the boards", "Mash:" in pushes[0][1] and "Maverick:" in pushes[0][1])


# --- legacy state migration --------------------------------------------------
fresh_state_file()
pushes.clear()
watch.post = lambda spec: same_id
legacy = {"requests": [a], "seen": ["r:900", "j:500"]}  # keys from before agency prefixes
state = watch.check_once(legacy, "e", "p", quiet_first_run=True)
check("legacy state re-baselines silently", pushes == [])
check("legacy keys are replaced, not kept", state["seen"] == ["Mash/r:900"])



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
