"""Offline smoke test of fix_dependencies.plan_all: prints the plan for three fake PRs."""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import analyze_pr as core, fix_dependencies as fd
fd.viewer_login = lambda t: "me"
J = lambda key, links=(): core.Jira(key=key, found=True, links=list(links)) if 'found' in core.Jira.__dataclass_fields__ else None
def pr(n, author="me", body="", title=None):
    return {"number": n, "title": title or f"HADOOP-{n}. thing {n}", "author": {"login": author}, "body": body}
def dep(n, author="me", verdict="CONFIRMED", key=None):
    return {"ref": f"#{n}", "number": n, "title": f"HADOOP-{n}. thing {n}", "jira": key or f"HADOOP-{n}",
            "open": True, "author": author, "verdict": verdict, "verdict_reason": f"why {n}",
            "sources": ["code"], "reasons": []}
# A(1) depends on B(2, mine), C(3, someone else); A required by D(4, mine, not in run)
A = fd.Target(pr(1), J("HADOOP-1"), {"depends_on": [dep(2), dep(3, "bob")], "blocks": [dep(4)]})
B = fd.Target(pr(2), J("HADOOP-2"), {"depends_on": [], "blocks": []})   # B's own analysis misses A
C_ = fd.Target(pr(3, "bob"), J("HADOOP-3"), {"depends_on": [], "blocks": [dep(1)]})
for n, ch, notes in fd.plan_all([A, B, C_], "o/r", "https://j", None, False, False, "me"):
    print(f"== #{n}")
    for c in ch: print("  ", c.kind, "|", c.headline, "|", c.key); 
    for x in notes: print("   note:", x)
    for c in ch:
        if c.kind == "pr-body": print("     " + c.preview.replace("\n", "\n     "))
# add_to_block keeps other lines
body = "intro\n\n" + fd.build_block([dep(9)], []) + "\n\nrest"
out = fd.add_to_block(body, "required", "#1", "- #1 (HADOOP-1) - x")
print(out); print(core.split_managed_block(out)[1])
print("idempotent:", fd.add_to_block(out, "required", "#1", "- #1 (HADOOP-1) - x") == out)
assert core.split_managed_block(out)[1] == {"depends": ["#9"], "required": ["#1"]}
assert out.split()[0] == "intro" and out.split()[-1] == "rest"   # text around the block is kept
assert fd.add_to_block(out, "required", "#1", "- #1 (HADOOP-1) - x") == out
plans = {n: (ch, notes) for n, ch, notes in fd.plan_all([A, B, C_], "o/r", "https://j", None, False, False, "me")}
assert sorted(c.key for c in plans[1][0]) == ["add:4:depends:1", "body:1", "link:HADOOP-1>HADOOP-4",
                                             "link:HADOOP-2>HADOOP-1", "link:HADOOP-3>HADOOP-1"]
assert [c.key for c in plans[2][0]] == ["body:2"]
assert plans[3][0] == []   # bob's PR is never edited and its link is already proposed for #1

# A subclass test failing at line 712 of its parent: a hunk over that line fixes it,
# an edit elsewhere in the same file does not.
sub = {"subsystem": "unit", "test": "hadoop.x.TestSub", "frames": ["TestParent.java:712"],
       "current": True, "last_seen": "2026-10-05", "seen_in": ["precommit"]}
parent = "m/src/test/java/hadoop/x/TestParent.java"
hunks = lambda *trunk: lambda: {parent: {"trunk": list(trunk)}}
match = core.ci_fix_match([sub], [parent], "YARN-1. fix it", "", hunks((694, 767)))
print("inherited:", match["reason"])
assert match["strength"] == "strong"
assert core.ci_fix_match([sub], [parent], "YARN-1. fix it", "", hunks((511, 527))) is None
assert core.parse_diff("+++ b/f\n@@ -10,4 +12,6 @@\n")["f"]["trunk"] == [(10, 13)]
# Recorded in JIRA but not planned: E's link to HADOOP-6 (WEAK) goes, HADOOP-7 (LIKELY) stays.
links = [{"id": str(n), "type": "Blocker", "direction": "inward", "label": "is blocked by",
          "key": f"HADOOP-{n}", "summary": "", "status": "Open", "resolution": None} for n in (6, 7)]
E = fd.Target(pr(5), J("HADOOP-5", links),
              {"depends_on": [dict(dep(6, verdict="WEAK"), sources=["jira"]),
                              dict(dep(7, verdict="LIKELY"), sources=["jira"])], "blocks": []})
(_, changes, _), = fd.plan_all([E], "o/r", "https://j", None, True, False, "me")
print("removes:", [c.key for c in changes])
assert [c.key for c in changes] == ["unlink:6"]

# --peers all pages through every open PR.
pages = iter([{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"number": 1}, None]},
              {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"number": 2}]}])
core.graphql = lambda q, v, t: {"repository": {"pullRequests": next(pages)}}
assert [p["number"] for p in core.fetch_open_prs("o/r", None)] == [1, 2]
print("plan_smoke: OK")
