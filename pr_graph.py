#!/usr/bin/env python3
"""SVG graph of the planned dependencies between your open pull requests.

pr_manager.py writes it on every 'Plan all' (D). It shows exactly the
dependencies the plan writes down - in JIRA links and in the 'Depends on' /
'Required by' block of your PR descriptions (fix_dependencies.solid_lists):
open ones judged CONFIRMED, CI-FIX or DISCOVERED. Weaker or refuted ones, and
PRs that merely share files, are left out.

* an arrow goes from the PR to merge first to the PR that waits on it, in the
  colour of its verdict; one arrow per pair, with the strongest verdict when
  both sides report it, and a head at both ends when each needs the other;
* an open PR of somebody else, or a JIRA issue with no PR, that one of yours
  needs is a dashed box;
* PRs with no planned dependency are listed apart, below the graph.

Prerequisites sit on the left, so the columns read as a merge order; an arrow
that skips columns runs between the boxes, never behind one. Hovering an
arrow shows why the dependency was found; a box links to its PR.

Pure Python, no Graphviz needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

DEFAULT_GRAPH_FILE = "pr-graph.svg"

# Colour of each verdict the plan writes down, strongest first: when both PRs
# report the same pair, the stronger verdict wins. Any other is drawn grey.
VERDICT_COLOUR = {"CONFIRMED": "#1f6feb", "CI-FIX": "#d97706", "DISCOVERED": "#7c3aed"}
OTHER_COLOUR = "#6b7280"


def _rank(verdict: str) -> int:
    return list(VERDICT_COLOUR).index(verdict) if verdict in VERDICT_COLOUR else len(VERDICT_COLOUR)
STATUS_FILL = {  # the statuses of list_upstream_prs.py
    "READY TO MERGE": "#bbf7d0",
    "WAITING FOR REVIEW": "#dcfce7",
    "CI RUNNING": "#dbeafe",
    "CHANGES REQUESTED": "#ffedd5",
    "CONFLICTS": "#fde68a",
    "YETUS FAILED": "#fee2e2",
    "CI FAILED": "#fecaca",
    "DRAFT": "#f3f4f6",
}

NODE_W, NODE_H = 260, 62
COL_GAP, GAP, GROUP_GAP = 120, 24, 48     # between columns, boxes, groups
LANE_H = 14                               # the slot of an arrow crossing a column
MARGIN, TOP = 24, 94                      # TOP: room for the heading and the legend
TITLE_CHARS = 40


@dataclass
class Node:
    label: str              # '#8704  HADOOP-19972'
    title: str              # without the JIRA key in front
    status: str             # status of your PR, or 'by bob, open' for somebody else's
    url: str = ""
    mine: bool = True


@dataclass
class Edge:
    source: str             # merge this one first ...
    target: str             # ... then this one
    verdict: str
    reasons: list[str] = field(default_factory=list)
    mutual: bool = False    # each needs the other: merge them back to back


@dataclass
class Graph:
    nodes: dict[str, Node]  # by '#8704', or by JIRA key when there is no PR
    edges: list[Edge]


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #
def _node(ref: str, jira: str, title: str, status: str, url: str, mine: bool) -> Node:
    if jira and title.upper().startswith(jira.upper()):
        title = title[len(jira):].lstrip(" .:-")      # 'HADOOP-1. Fix x' -> 'Fix x'
    label = f"{ref}  {jira}" if jira and ref != jira else ref
    return Node(label, title, status, url, mine)


def build_graph(prs: Iterable[dict[str, Any]],
                planned: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]]) -> Graph:
    """The graph of your open PRs.

    ``prs`` has one dict per open PR: number, title, status, url and jira (may
    be empty). ``planned`` maps a PR number to the dependencies the plan writes
    for it, as fix_dependencies.solid_lists returns them: what it needs, and
    what waits on it. A PR missing from it was not analysed, and says so.
    """
    nodes: dict[str, Node] = {}
    for pr in prs:
        ref = f"#{pr['number']}"
        nodes[ref] = _node(ref, pr.get("jira") or "", pr.get("title") or "",
                           pr.get("status") or "", pr.get("url") or "", mine=True)
        if pr["number"] not in planned:
            nodes[ref].title = "(not analysed) " + nodes[ref].title
    mine = set(nodes)
    edges: dict[tuple[str, str], Edge] = {}

    def node_for(entry: dict[str, Any]) -> str:
        """The node of a dependency, added when it is not yours."""
        ref = entry.get("ref") or entry.get("jira") or ""
        if ref and ref not in nodes:
            if ref.startswith("#"):
                status = (f"by {entry.get('author') or 'unknown'}, "
                          f"{(entry.get('state') or 'open').lower()}")
            else:
                status = "JIRA, no pull request"
            nodes[ref] = _node(ref, entry.get("jira") or "", entry.get("title") or "", status,
                               entry.get("url") or "", mine=False)
        return ref

    def add_edge(source: str, target: str, entry: dict[str, Any]) -> None:
        verdict = entry.get("verdict") or ""
        reasons = [str(r) for r in entry.get("reasons") or []]
        if entry.get("verdict_reason"):
            reasons.append(f"{verdict}: {entry['verdict_reason']}")
        reverse = edges.get((target, source))
        edge = reverse or edges.setdefault((source, target), Edge(source, target, verdict))
        if reverse:
            edge.mutual = True
        if _rank(verdict) < _rank(edge.verdict):
            edge.verdict = verdict
        edge.reasons += [r for r in reasons if r not in edge.reasons]

    for number, (depends, required) in planned.items():
        me = f"#{number}"
        if me not in mine:
            continue
        for entry in depends:
            other = node_for(entry)
            if other and other != me:
                add_edge(other, me, entry)
        for entry in required:
            other = node_for(entry)
            if other and other != me:
                add_edge(me, other, entry)
    return Graph(nodes, list(edges.values()))


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def _groups(graph: Graph) -> tuple[list[list[str]], list[str]]:
    """The connected groups of nodes, largest first, and the nodes on their own."""
    neighbours: dict[str, set[str]] = {n: set() for n in graph.nodes}
    for e in graph.edges:
        neighbours[e.source].add(e.target)
        neighbours[e.target].add(e.source)
    seen: set[str] = set()
    groups: list[list[str]] = []
    for start in graph.nodes:
        if start in seen:
            continue
        seen.add(start)
        group, stack = [], [start]
        while stack:
            group.append(stack.pop())
            fresh = neighbours[group[-1]] - seen
            seen |= fresh
            stack += sorted(fresh)
        groups.append(group)
    alone = [g[0] for g in groups if len(g) == 1]
    groups = sorted((g for g in groups if len(g) > 1), key=lambda g: (-len(g), min(g)))
    return groups, alone


# A slot in a column: a node, or the lane of edge k crossing column i.
Slot = str | tuple[int, int]


def _columns(group: list[str], edges: list[Edge]) -> list[list[Slot]]:
    """Each node one column right of its rightmost prerequisite (an edge that
    closes a cycle is ignored); an edge that skips columns gets a lane in each
    column it crosses; then the slots are ordered to keep arrows short."""
    members = set(group)
    succ: dict[str, list[int]] = {n: [] for n in group}
    for k, e in enumerate(edges):
        if e.source in members and e.target in members:
            succ[e.source].append(k)

    # Depth first: dropping the edges back to a node still on the stack leaves
    # a DAG, and the order nodes finish in is a reversed topological order.
    forward: list[int] = []
    finished: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> None:
        on_stack.add(node)
        for k in succ[node]:
            if edges[k].target in on_stack:
                continue
            forward.append(k)
            if edges[k].target not in finished:
                visit(edges[k].target)
        on_stack.discard(node)
        finished.append(node)

    for node in sorted(group):
        if node not in finished:
            visit(node)

    level = dict.fromkeys(group, 0)
    position = {n: i for i, n in enumerate(reversed(finished))}
    for k in sorted(forward, key=lambda k: position[edges[k].source]):
        e = edges[k]
        level[e.target] = max(level[e.target], level[e.source] + 1)

    columns: list[list[Slot]] = [[] for _ in range(max(level.values()) + 1)]
    for node in sorted(group):
        columns[level[node]].append(node)
    before: dict[Slot, list[Slot]] = {}
    after: dict[Slot, list[Slot]] = {}
    for k in forward:
        e = edges[k]
        chain: list[Slot] = [e.source]
        for i in range(level[e.source] + 1, level[e.target]):
            columns[i].append((k, i))
            chain.append((k, i))
        chain.append(e.target)
        for a, b in zip(chain, chain[1:]):
            after.setdefault(a, []).append(b)
            before.setdefault(b, []).append(a)

    def by_barycentre(column: list[Slot], fixed: list[Slot], links: dict[Slot, list[Slot]]) -> None:
        pos = {s: i for i, s in enumerate(fixed)}
        def key(slot: Slot) -> float:
            linked = [pos[s] for s in links.get(slot, []) if s in pos]
            return sum(linked) / len(linked) if linked else len(fixed)
        column.sort(key=key)

    for _ in range(4):
        for i in range(1, len(columns)):
            by_barycentre(columns[i], columns[i - 1], before)
        for i in range(len(columns) - 2, -1, -1):
            by_barycentre(columns[i], columns[i + 1], after)
    return columns


@dataclass
class Layout:
    boxes: dict[str, tuple[float, float]]                 # top-left corner of each box
    lanes: dict[int, list[tuple[float, float]]]           # (x, y) where edge k crosses
    width: float
    alone_top: float | None                               # top of the 'no dependency' grid
    bottom: float


def _height(slot: Slot) -> float:
    return NODE_H if isinstance(slot, str) else LANE_H


def _layout(graph: Graph) -> Layout:
    groups, alone = _groups(graph)
    out = Layout({}, {}, 0.0, None, float(TOP))
    y = float(TOP)
    for group in groups:
        columns = _columns(group, graph.edges)
        extent = [sum(_height(s) + GAP for s in c) - GAP for c in columns]
        for i, column in enumerate(columns):
            x = MARGIN + i * (NODE_W + COL_GAP)
            top = y + (max(extent) - extent[i]) / 2
            for slot in column:
                if isinstance(slot, str):
                    out.boxes[slot] = (x, top)
                else:
                    out.lanes.setdefault(slot[0], []).append((x, top + LANE_H / 2))
                top += _height(slot) + GAP
        out.width = max(out.width, len(columns) * (NODE_W + COL_GAP) - COL_GAP)
        y += max(extent) + GROUP_GAP
    if alone:
        per_row = max(3, int((out.width + GAP) // (NODE_W + GAP)))
        out.alone_top = y + 22 if groups else y
        for k, node in enumerate(sorted(alone)):
            out.boxes[node] = (MARGIN + k % per_row * (NODE_W + GAP),
                               out.alone_top + k // per_row * (NODE_H + GAP))
        out.width = max(out.width, min(len(alone), per_row) * (NODE_W + GAP) - GAP)
        y = out.alone_top + ((len(alone) - 1) // per_row + 1) * (NODE_H + GAP)
    out.bottom = y
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _curve(x1: float, y1: float, x2: float, y2: float) -> str:
    """A curve to (x2, y2), from (x1, y1) the path is at; backwards (only in a
    cycle) it swings out and back in."""
    bend = max(40.0, (x2 - x1) / 2) if x2 > x1 else 80.0
    return f" C{x1 + bend:.0f},{y1:.1f} {x2 - bend:.0f},{y2:.1f} {x2:.0f},{y2:.1f}"


def _box(node: Node, x: float, y: float) -> str:
    title = node.title if len(node.title) <= TITLE_CHARS else node.title[:TITLE_CHARS - 1] + "…"
    fill = STATUS_FILL.get(node.status, "#f9fafb") if node.mine else "#ffffff"
    dash = "" if node.mine else ' stroke-dasharray="5 3"'
    body = (f'<rect x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{NODE_H}" rx="8" '
            f'fill="{fill}" stroke="#374151" stroke-width="1.2"{dash}/>'
            f'<text x="{x + 10:.0f}" y="{y + 18:.0f}" class="label">{escape(node.label)}</text>'
            f'<text x="{x + 10:.0f}" y="{y + 36:.0f}" class="title">{escape(title)}</text>'
            f'<text x="{x + 10:.0f}" y="{y + 53:.0f}" class="status">{escape(node.status)}</text>'
            f'<title>{escape(node.label)}: {escape(node.title)}</title>')
    return f'<a href={quoteattr(node.url)} target="_blank">{body}</a>' if node.url else body


def _ports(graph: Graph, layout: Layout) -> tuple[list[float], list[float]]:
    """Where each edge leaves its source and enters its target: spread along
    the side of the box, in the order of where the edge heads next."""
    def next_y(k: int, leaving: bool) -> float:
        e, lanes = graph.edges[k], layout.lanes.get(k)
        if lanes:
            return lanes[0 if leaving else -1][1]
        return layout.boxes[e.target if leaving else e.source][1] + NODE_H / 2

    out_y, in_y = [0.0] * len(graph.edges), [0.0] * len(graph.edges)
    for port, own, leaving in ((out_y, "source", True), (in_y, "target", False)):
        by_node: dict[str, list[int]] = {}
        for k, e in enumerate(graph.edges):
            by_node.setdefault(getattr(e, own), []).append(k)
        for node, ks in by_node.items():
            ks.sort(key=lambda k: next_y(k, leaving))
            for slot, k in enumerate(ks, 1):
                port[k] = layout.boxes[node][1] + NODE_H * slot / (len(ks) + 1)
    return out_y, in_y


def render_svg(graph: Graph, heading: str = "Open pull requests", generated: str = "") -> str:
    layout = _layout(graph)
    boxes = layout.boxes

    # The legend and the arrow heads: only what is drawn.
    verdicts = sorted({e.verdict for e in graph.edges}, key=_rank)
    colours = {v: VERDICT_COLOUR.get(v, OTHER_COLOUR) for v in verdicts}
    legend = list(colours.items())
    if any(e.mutual for e in graph.edges):
        legend.append(("<-> each needs the other", "#111827"))
    legend_w = sum(44 + 7 * len(name) for name, _ in legend)
    total_w = max(layout.width, legend_w, 760) + 2 * MARGIN
    total_h = layout.bottom + MARGIN

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="Helvetica, Arial, sans-serif">',
        "<style>.label{font-size:13px;font-weight:bold;fill:#111827}"
        ".title{font-size:12px;fill:#1f2937}.status,.small{font-size:11px;fill:#4b5563}"
        ".head{font-size:16px;font-weight:bold;fill:#111827}</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<defs>",
        *(f'<marker id="arrow-{i}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" '
          f'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
          f'fill="{colours[v]}"/></marker>' for i, v in enumerate(verdicts)),
        "</defs>",
    ]
    summary = (f"{sum(n.mine for n in graph.nodes.values())} open PR(s), "
               f"{len(graph.edges)} planned dependency(ies)"
               + (f" - generated {generated}" if generated else "")
               + ". Arrows point from the PR to merge first.")
    out.append(f'<text x="{MARGIN}" y="{MARGIN + 6}" class="head">{escape(heading)}</text>'
               f'<text x="{MARGIN}" y="{MARGIN + 24}" class="small">{escape(summary)}</text>')
    x = MARGIN
    for name, colour in legend:
        out.append(f'<path d="M{x},{MARGIN + 44} h22" stroke="{colour}" stroke-width="2.5"/>'
                   f'<text x="{x + 27}" y="{MARGIN + 48}" class="small">{escape(name)}</text>')
        x += 44 + 7 * len(name)
    if layout.alone_top is not None and graph.edges:
        out.append(f'<text x="{MARGIN}" y="{layout.alone_top - 8:.0f}" class="small">'
                   f'No planned dependency:</text>')

    out_y, in_y = _ports(graph, layout)
    marker = {v: i for i, v in enumerate(verdicts)}
    for k in sorted(range(len(graph.edges)), key=lambda k: -_rank(graph.edges[k].verdict)):
        e = graph.edges[k]   # the strongest drawn last, on top
        x, y = boxes[e.source][0] + NODE_W, out_y[k]
        d = f"M{x:.0f},{y:.1f}"
        for lane_x, lane_y in layout.lanes.get(k, []):
            d += _curve(x, y, lane_x, lane_y) + f" H{lane_x + NODE_W:.0f}"
            x, y = lane_x + NODE_W, lane_y
        d += _curve(x, y, boxes[e.target][0], in_y[k])
        what = "each needs the other: merge them back to back" if e.mutual \
            else f"{e.source} before {e.target}"
        why = "\n".join([f"{e.source} - {e.target}: {what} - {e.verdict}"] + e.reasons)
        start = f' marker-start="url(#arrow-{marker[e.verdict]})"' if e.mutual else ""
        out.append(f'<path d="{d}" fill="none" stroke="{colours[e.verdict]}" stroke-width="2.2"'
                   f'{start} marker-end="url(#arrow-{marker[e.verdict]})">'
                   f'<title>{escape(why)}</title></path>')
    out += [_box(node, *boxes[ref]) for ref, node in graph.nodes.items()]
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_svg(path: str, svg: str) -> str:
    """Write the file in one go, so a viewer reloading it never sees half of it."""
    path = os.path.abspath(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(svg)
    os.replace(tmp, path)
    return path
