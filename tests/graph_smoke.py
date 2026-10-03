"""Offline smoke test of pr_graph: builds and renders the graph of a few fake PRs."""
import os, sys, tempfile, xml.dom.minidom
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pr_graph as g
def pr(n, status="WAITING FOR REVIEW"):
    return {"number": n, "title": f"HADOOP-{n}. thing <{n}> & co", "status": status,
            "url": f"https://github.com/o/r/pull/{n}", "jira": f"HADOOP-{n}"}
def dep(n, verdict="CONFIRMED", author="me", ref=None):
    return {"ref": ref or f"#{n}", "number": n, "jira": f"HADOOP-{n}", "open": True, "author": author,
            "state": "OPEN", "title": f"HADOOP-{n}. dep {n}",
            "verdict": verdict, "verdict_reason": f"why {n}", "reasons": [f"reason {n}"]}
prs = [pr(1), pr(2, "YETUS FAILED"), pr(3), pr(4), pr(5, "DRAFT"), pr(6)]
planned = {   # (what it needs, what waits on it), as fix_dependencies.solid_lists gives them
    1: ([dep(2), dep(9, "CI-FIX", author="bob")], [dep(6)]),
    2: ([], [dep(1, "CI-FIX")]),                  # the same pair seen from #2: one arrow
    3: ([dep(7, "DISCOVERED", ref="HADOOP-7")], []),
    4: ([], []),
    6: ([], []),
}
graph = g.build_graph(prs, planned)
assert set(graph.nodes) == {"#1", "#2", "#3", "#4", "#5", "#6", "#9", "HADOOP-7"}, graph.nodes
assert not graph.nodes["#9"].mine and graph.nodes["#9"].status == "by bob, open"
assert graph.nodes["#1"].title == "thing <1> & co" and graph.nodes["#1"].label == "#1  HADOOP-1"
assert graph.nodes["#5"].title.startswith("(not analysed)") and graph.nodes["HADOOP-7"].label == "HADOOP-7"
assert graph.nodes["HADOOP-7"].status == "JIRA, no pull request"
edges = {(e.source, e.target): e for e in graph.edges}
assert set(edges) == {("#2", "#1"), ("#9", "#1"), ("#1", "#6"), ("HADOOP-7", "#3")}, edges
assert edges[("#2", "#1")].verdict == "CONFIRMED"           # strongest of CONFIRMED and CI-FIX
assert "reason 1" in edges[("#2", "#1")].reasons and "reason 2" in edges[("#2", "#1")].reasons
groups, alone = g._groups(graph)
assert sorted(alone) == ["#4", "#5"] and sorted(groups[0]) == ["#1", "#2", "#6", "#9"]
# An edge that skips a column gets a lane there, between the boxes: #2 -> #3 -> #4 and #2 -> #4.
skip = g.build_graph([pr(2), pr(3), pr(4)], {3: ([dep(2)], [dep(4)]), 4: ([dep(2)], [])})
layout = g._layout(skip)
long = next(k for k, e in enumerate(skip.edges) if (e.source, e.target) == ("#2", "#4"))
assert list(layout.lanes) == [long] and layout.lanes[long][0][0] == layout.boxes["#3"][0]
lane_y = layout.lanes[long][0][1]
assert not layout.boxes["#3"][1] <= lane_y <= layout.boxes["#3"][1] + g.NODE_H   # not behind #3
# Each needs the other: one arrow with a head at both ends.
both = g.build_graph([pr(1), pr(2)], {1: ([dep(2)], []), 2: ([dep(1)], [])})
assert len(both.edges) == 1 and both.edges[0].mutual and "marker-start" in g.render_svg(both)
# A longer cycle still lays out.
cyc = g.build_graph([pr(1), pr(2), pr(3)], {1: ([dep(3)], []), 2: ([dep(1)], []), 3: ([dep(2)], [])})
assert len(cyc.edges) == 3 and len(g._columns(["#1", "#2", "#3"], cyc.edges)) == 3
svg = g.render_svg(graph, "Open PRs of me into o/r:trunk", "2026-10-03 12:00 UTC")
xml.dom.minidom.parseString(svg)                            # well-formed, titles escaped
assert "thing &lt;1&gt; &amp; co" in svg and svg.count("<rect x=") == 8
assert "CI-FIX</text>" in svg and "each needs" not in svg   # the legend shows what is drawn
assert g.render_svg(graph, "x", "t") == g.render_svg(graph, "x", "t")   # deterministic
xml.dom.minidom.parseString(g.render_svg(g.build_graph([], {})))       # nothing open
with tempfile.TemporaryDirectory() as d:
    path = g.write_svg(os.path.join(d, "pr-graph.svg"), svg)
    assert open(path, encoding="utf-8").read() == svg and os.listdir(d) == ["pr-graph.svg"]
print("graph_smoke: OK")
