"""Offline smoke test of fix_dependencies.plan_all: prints the plan for three fake PRs."""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
for n, ch, notes in fd.plan_all([A, B, C_], "o/r", "https://j", None, False, False, False, "me"):
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
