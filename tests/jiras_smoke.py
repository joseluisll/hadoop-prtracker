"""Offline smoke test of list_my_jiras.py: match made-up JIRA issues with made-up PRs."""
import os, sys
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

lines = lmj.report(issues, "apache/hadoop", None).splitlines()
assert lines[0].startswith("| Ticket | Status | Summary | PR |"), lines[0]
one, two, four, five = lines[2:6]
assert "[#11](https://example.org/11), open" in one and "+1, 2026-10-02 |" in one, one
assert "| Pass | alice, 2026-10-03 |" in one, one
assert "Some \\| summary" in one, one
assert "| No PR | - | - | - |" in two, two
assert "older than head" in four and "Awaiting approval to run" in four, four
assert "changes by bob, 2026-10-03" in four, four
assert "merged" in five and "| Running |" in five, five
assert lines[-1] == "4 unresolved issues", lines[-1]
print("jiras smoke ok")
