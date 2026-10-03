"""Offline smoke test of pr_graph: builds and renders the graph of five fake PRs."""
import os, sys, tempfile, xml.dom.minidom
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pr_graph as g
def pr(n, status="WAITING FOR REVIEW"):
    return {"number": n, "title": f"HADOOP-{n}. thing <{n}> & co", "status": status,
            "url": f"https://github.com/o/r/pull/{n}", "jira": f"HADOOP-{n}"}
def dep(n, verdict="CONFIRMED", open_=True, author="me", ref=None):
    return {"ref": ref or f"#{n}", "number": n, "jira": f"HADOOP-{n}", "open": open_, "author": author,
            "state": "OPEN" if open_ else "MERGED", "title": f"HADOOP-{n}. dep {n}",
            "verdict": verdict, "verdict_reason": f"why {n}", "reasons": [f"reason {n}"]}
prs = [pr(1), pr(2, "YETUS FAILED"), pr(3), pr(4), pr(5, "DRAFT")]
deps = {
    1: {"depends_on": [dep(2), dep(9, "CI-FIX", author="bob"), dep(8, open_=False)], "blocks": [],
        "overlaps": [{"ref": "#2", "count": 1, "files": ["a"]}]},   # same pair as a dependency: dropped
    2: {"depends_on": [], "blocks": [dep(1, "WEAK")],               # same edge seen from #2: one arrow
        "overlaps": [{"ref": "#3", "count": 2, "files": ["x", "y"], "clashes": [1]}]},
    3: {"depends_on": [dep(7, "LIKELY", ref="HADOOP-7")], "blocks": [], "overlaps": []},
    4: {"depends_on": [], "blocks": [], "overlaps": []},
}
graph = g.build_graph(prs, deps)
assert set(graph.nodes) == {"#1", "#2", "#3", "#4", "#5", "#9", "HADOOP-7"}, graph.nodes   # merged #8 left out
assert not graph.nodes["#9"].mine and graph.nodes["#9"].status == "by bob, open"
assert graph.nodes["#1"].title == "thing <1> & co" and graph.nodes["#1"].label == "#1  HADOOP-1"
assert graph.nodes["#5"].title.startswith("(not analysed)") and graph.nodes["HADOOP-7"].label == "HADOOP-7"
edges = {(e.source, e.target): e for e in graph.edges}
assert set(edges) == {("#2", "#1"), ("#9", "#1"), ("HADOOP-7", "#3")}, edges
assert edges[("#2", "#1")].verdict == "CONFIRMED"           # strongest of CONFIRMED and WEAK
assert "reason 1" in edges[("#2", "#1")].reasons and "reason 2" in edges[("#2", "#1")].reasons
assert [(a, b) for a, b, _ in graph.overlaps] == [("#2", "#3")]
groups, alone = g._groups(graph)
assert sorted(alone) == ["#4", "#5"] and sorted(groups[0]) == ["#1", "#2", "#3", "#9", "HADOOP-7"]
cols = g._columns(groups[0], graph.edges)
level = {n: i for i, c in enumerate(cols) for n in c}
assert level["#2"] < level["#1"] and level["#9"] < level["#1"] and level["HADOOP-7"] < level["#3"]
# A cycle still lays out.
cyc = g.build_graph([pr(1), pr(2)], {1: {"depends_on": [dep(2)], "blocks": [dep(2)]}})
assert len(cyc.edges) == 2 and len(g._columns(["#1", "#2"], cyc.edges)) == 2
svg = g.render_svg(graph, "Open PRs of me into o/r:trunk", "2026-10-03 12:00 UTC")
xml.dom.minidom.parseString(svg)                            # well-formed, titles escaped
assert "thing &lt;1&gt; &amp; co" in svg and svg.count("<rect x=") == 7
assert "WEAK</text>" not in svg and "CI-FIX</text>" in svg   # the legend shows what is drawn
assert g.render_svg(graph, "x", "t") == g.render_svg(graph, "x", "t")   # deterministic
xml.dom.minidom.parseString(g.render_svg(g.build_graph([], {})))       # nothing open
with tempfile.TemporaryDirectory() as d:
    path = g.write_svg(os.path.join(d, "pr-graph.svg"), svg)
    assert open(path, encoding="utf-8").read() == svg and os.listdir(d) == ["pr-graph.svg"]
# Unknown verdicts are drawn as UNVERIFIED; a self-reference adds nothing.
odd = g.build_graph([pr(1), pr(2)], {1: {"depends_on": [dep(2, "???"), dep(1)]}, 2: {}})
assert [(e.source, e.verdict) for e in odd.edges] == [("#2", "UNVERIFIED")]
xml.dom.minidom.parseString(g.render_svg(odd))
print("graph_smoke: OK")
