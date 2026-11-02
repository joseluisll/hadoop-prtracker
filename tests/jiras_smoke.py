"""Offline smoke test of list_my_jiras.py: match made-up JIRA issues with made-up PRs."""
import os, sys
import urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import list_my_jiras as lmj

YETUS = ":confetti_ball: **+1 overall**\n| +1 :green_heart: | unit | 1m | | passed |"


def issue(key, status="Open", summary="Some | summary"):
    return {"key": key, "fields": {"status": {"name": status}, "summary": summary}}


def pr(number, title, state="OPEN", build=("COMPLETED", "SUCCESS"), yetus_at="2026-10-02T00:00:00Z",
       reviews=()):
    return {
        "number": number, "title": title, "url": f"https://example.org/{number}", "state": state,
        "isDraft": False,
        "comments": {"nodes": [{"author": {"login": "hadoop-yetus"}, "createdAt": yetus_at,
                                "body": YETUS}]},
        "latestReviews": {"nodes": [{"author": {"login": who}, "state": s, "submittedAt": at}
                                    for who, s, at in reviews]},
        "commits": {"nodes": [{"commit": {
            "committedDate": "2026-10-01T00:00:00Z",
            "statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "CheckRun", "name": "Build", "status": build[0],
                 "conclusion": build[1]}]}}}}]},
    }


PRS = {
    "HADOOP-1": [pr(10, "HADOOP-1. Old try", state="CLOSED"),
                 pr(11, "HADOOP-1. Fix", reviews=[("alice", "APPROVED", "2026-10-03T00:00:00Z")])],
    "HADOOP-2": [pr(20, "HADOOP-3. Mentions HADOOP-2 only later")],
    "HADOOP-4": [pr(40, "HADOOP-4. Stale", yetus_at="2026-09-01T00:00:00Z",
                    build=("COMPLETED", "ACTION_REQUIRED"),
                    reviews=[("bob", "CHANGES_REQUESTED", "2026-10-03T00:00:00Z")])],
    "HADOOP-5": [pr(50, "HADOOP-5. Merged", state="MERGED", build=("IN_PROGRESS", None))],
}


def fake_graphql(_query, variables, _token):
    key = variables["q"].split()[2]
    return {"search": {"nodes": [*PRS.get(key, []), None]}}


lmj.graphql = fake_graphql

pages = [[issue("HADOOP-1"), issue("HADOOP-2")], [issue("HADOOP-4"), issue("HADOOP-5")]]
seen = []


def fake_jira_get(_base, path, params):
    assert path == "search" and 'reporter = "me"' in params["jql"]
    assert "resolution = Unresolved" in params["jql"]
    seen.append(params["startAt"])
    return {"issues": pages[len(seen) - 1], "total": 4}


lmj.core.jira_get = fake_jira_get
issues = lmj.my_issues("me")
assert seen == ["0", "2"] and len(issues) == 4, seen

FORK = "somebody/hadoop"


def run(number, name, status="completed", conclusion="success", head_sha="",
        created_at="2026-10-02T00:00:00Z"):
    return {"id": number, "name": name, "status": status, "conclusion": conclusion,
            "head_sha": head_sha, "created_at": created_at,
            "html_url": f"https://example.org/runs/{number}"}


TIPS = {
    "HADOOP-6": "tip6",
    "HADOOP-7": "tip7",
    "HADOOP-8": "tip8",
    "HADOOP-9": "tip9",
    "HADOOP-10": "tip10",
    "HADOOP-11": "tip11",
    "HADOOP-13": "tip13",
}

RUNS = {
    "HADOOP-6": [run(1, "Build", head_sha="tip6")],
    "yetus/HADOOP-6": [run(2, "Yetus", conclusion="failure")],
    "HADOOP-7": [run(3, "Build", conclusion="failure", head_sha="other")],
    "yetus/HADOOP-7": [run(4, "Yetus", conclusion="failure")],
    "HADOOP-8": [run(5, "Build", status="queued", conclusion=None, head_sha="tip8")],
    "HADOOP-10": [run(6, "Build", status="in_progress", conclusion=None, head_sha="tip10")],
    "yetus/HADOOP-11": [run(7, "Yetus", conclusion="failure")],
    "HADOOP-13": [run(9, "Build", conclusion="cancelled", head_sha="tip13")],
    "yetus/HADOOP-13": [run(10, "Yetus", conclusion="success")],
}

STATUSES = {
    "tip6": [{"context": "Yetus", "target_url": "https://example.org/runs/2"}],
    "tip11": [{"context": "Yetus", "target_url": "https://example.org/runs/8"},
              {"context": "Yetus", "target_url": "https://example.org/runs/7/extra"},
              {"context": "Build", "target_url": "https://example.org/runs/7"}],
    "tip13": [{"context": "Yetus", "target_url": "https://example.org/runs/10"}],
}

calls = []


def fake_rest_json(path, _token):
    calls.append(path)
    if path.startswith(f"repos/{FORK}/branches/"):
        branch = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        tip = TIPS.get(branch)
        return {"commit": {"sha": tip}} if tip else {}
    if path.startswith(f"repos/{FORK}/actions/runs?"):
        branch = urllib.parse.unquote(path.split("branch=", 1)[1].split("&", 1)[0])
        return {"workflow_runs": RUNS.get(branch, [])}
    if path.startswith(f"repos/{FORK}/commits/") and path.endswith("/status"):
        tip = path.split("/commits/", 1)[1].rsplit("/status", 1)[0]
        return {"statuses": STATUSES.get(tip, [])}
    return {}


lmj.core.rest_json = fake_rest_json

lines = lmj.report(issues, "apache/hadoop", None, FORK).splitlines()
assert lines[0].startswith("| Ticket | Status | Summary | PR |"), lines[0]
assert lines[0].endswith("| Approval | Fork build | Fork Yetus |"), lines[0]
one, two, four, five = lines[2:6]
assert "[#11](https://example.org/11), open" in one and "+1, 2026-10-02 |" in one, one
assert "| Pass | alice, 2026-10-03 |" in one, one
assert "Some \\| summary" in one, one
assert "| No PR | - | - | - |" in two, two
assert two.endswith("| No branch | No branch |"), two
assert "older than head" in four and "Awaiting approval to run" in four, four
assert "changes by bob, 2026-10-03" in four, four
assert "merged" in five and "| Running |" in five, five
assert lines[-1] == "4 unresolved issues", lines[-1]
assert not any("/actions/runs" in path for path in calls), calls

FORK_ISSUES = [issue("HADOOP-6"), issue("HADOOP-7"), issue("HADOOP-8"), issue("HADOOP-9"),
               issue("HADOOP-10"), issue("HADOOP-11"), issue("HADOOP-12"), issue("HADOOP-13")]
fork_lines = lmj.report(FORK_ISSUES, "apache/hadoop", None, FORK).splitlines()
assert fork_lines[0].endswith("| Approval | Fork build | Fork Yetus |"), fork_lines[0]
six, seven, eight, nine, ten, eleven, twelve, thirteen = fork_lines[2:10]
assert six.endswith("| [Pass](https://example.org/runs/1), 2026-10-02 | "
                    "[-1](https://example.org/runs/2), 2026-10-02 |"), six
assert "older than tip" not in six, six
assert seven.endswith("| [Fail](https://example.org/runs/3), 2026-10-02, older than tip | "
                      "[-1](https://example.org/runs/4), 2026-10-02, older than tip |"), seven
assert eight.endswith("| [Queued](https://example.org/runs/5), 2026-10-02 | None |"), eight
assert nine.endswith("| None | None |"), nine
assert ten.endswith("| [Running](https://example.org/runs/6), 2026-10-02 | None |"), ten
assert eleven.endswith("| None | [-1](https://example.org/runs/7), 2026-10-02, older than tip |"), eleven
assert twelve.endswith("| No branch | No branch |"), twelve
assert thirteen.endswith("| [Cancelled](https://example.org/runs/9), 2026-10-02 | "
                         "[+1](https://example.org/runs/10), 2026-10-02 |"), thirteen
assert "older than tip" not in thirteen, thirteen
assert not any("branch=HADOOP-12" in path for path in calls), calls

# fork_verdict: Build and Yetus labels for every conclusion.
for run, expected in [
    ({"status": "waiting"}, "Awaiting approval to run"),
    ({"status": "completed", "conclusion": "action_required"}, "Awaiting approval to run"),
    ({"status": "completed", "conclusion": "skipped"}, "Skipped"),
    ({"status": "completed", "conclusion": "neutral"}, "Neutral"),
    ({"status": "completed", "conclusion": "stale"}, "Stale"),
    ({"status": "completed", "conclusion": "timed_out"}, "Fail"),
]:
    assert lmj.fork_verdict(run) == expected, (run, expected)
for run, expected in [
    ({"status": "completed", "conclusion": "timed_out"}, "-1"),
    ({"status": "completed", "conclusion": "action_required"}, "Awaiting approval to run"),
]:
    assert lmj.fork_verdict(run, yetus=True) == expected, (run, expected)
print("jiras smoke ok")
