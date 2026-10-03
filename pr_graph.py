#!/usr/bin/env python3
"""SVG graph of the relationships between your open pull requests.

pr_manager.py writes it on every 'Plan all' (D), from the dependencies
analyze_pr.py already found for each PR, so nothing is fetched here:

* an arrow goes from the PR to merge first to the PR that waits on it, drawn
  in the colour of its verdict (CONFIRMED, CI-FIX, LIKELY, ...); one arrow per
  pair, with the strongest verdict when both sides report it;
* a dotted grey line joins two PRs that share files and nothing more;
* an open PR or JIRA of somebody else that one of yours needs is a dashed box;
* PRs with no relationship at all are listed apart, below the graph.

Prerequisites sit on the left, so the columns read as a merge order. Hovering
an arrow shows why the dependency was found; a box links to its PR.

Pure Python, no Graphviz needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

DEFAULT_GRAPH_FILE = "pr-graph.svg"

# Colour and dash pattern of each verdict of analyze_pr.py, strongest first:
# when both PRs report the same pair, the stronger verdict wins.
VERDICT_STYLE = {
    "CONFIRMED": ("#1f6feb", ""),
    "CI-FIX": ("#d97706", ""),
    "DISCOVERED": ("#7c3aed", ""),
    "LIKELY": ("#0891b2", ""),
    "UNVERIFIED": ("#6b7280", "6 4"),
    "WEAK": ("#9ca3af", "6 4"),
    "UNSUPPORTED": ("#dc2626", "2 4"),
    "STALE": ("#dc2626", "2 4"),
}
VERDICT_RANK = {verdict: rank for rank, verdict in enumerate(VERDICT_STYLE)}
OVERLAP_STYLE = ("#9ca3af", "1 3")
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


@dataclass
class Graph:
    nodes: dict[str, Node]                  # by '#8704', or by JIRA key when there is no PR
    edges: list[Edge]
    overlaps: list[tuple[str, str, str]]    # (a, b, what they share)


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #
def _node(ref: str, jira: str, title: str, status: str, url: str, mine: bool) -> Node:
    if jira and title.upper().startswith(jira.upper()):
        title = title[len(jira):].lstrip(" .:-")      # 'HADOOP-1. Fix x' -> 'Fix x'
    label = f"{ref}  {jira}" if jira and ref != jira else ref
    return Node(label, title, status, url, mine)


def build_graph(prs: Iterable[dict[str, Any]], deps_by_number: dict[int, dict[str, Any]]) -> Graph:
    """The graph of your open PRs.

    ``prs`` has one dict per open PR: number, title, status, url and jira (may
    be empty). ``deps_by_number`` maps a PR number to what
    analyze_pr.collect_dependencies returned for it; a PR missing from it was
    not analysed, and says so in its box.
    """
    nodes: dict[str, Node] = {}
    for pr in prs:
        ref = f"#{pr['number']}"
        nodes[ref] = _node(ref, pr.get("jira") or "", pr.get("title") or "",
                           pr.get("status") or "", pr.get("url") or "", mine=True)
        if pr["number"] not in deps_by_number:
            nodes[ref].title = "(not analysed) " + nodes[ref].title
    mine = set(nodes)
    edges: dict[tuple[str, str], Edge] = {}

    def node_for(entry: dict[str, Any]) -> str | None:
        """The node of a dependency, added when it is not yours; None when merged."""
        ref = entry.get("ref") or ""
        if ref in mine:
            return ref
        if not ref or not entry.get("open"):
            return None
        if ref not in nodes:
            if ref.startswith("#"):
                status = (f"by {entry.get('author') or 'unknown'}, "
                          f"{(entry.get('state') or 'unknown').lower()}")
            else:
                status = "JIRA, no pull request"
            nodes[ref] = _node(ref, entry.get("jira") or "", entry.get("title") or "", status,
                               entry.get("url") or "", mine=False)
        return ref

    def add_edge(source: str, target: str, entry: dict[str, Any]) -> None:
        verdict = entry.get("verdict")
        verdict = verdict if verdict in VERDICT_RANK else "UNVERIFIED"
        reasons = [str(r) for r in entry.get("reasons") or []]
        if entry.get("verdict_reason"):
            reasons.append(f"{verdict}: {entry['verdict_reason']}")
        edge = edges.setdefault((source, target), Edge(source, target, verdict))
        if VERDICT_RANK[verdict] < VERDICT_RANK[edge.verdict]:
            edge.verdict = verdict
        edge.reasons += [r for r in reasons if r not in edge.reasons]

    for number, deps in deps_by_number.items():
        me = f"#{number}"
        if me not in mine:
            continue
        for key, waits in (("depends_on", True), ("blocks", False)):
            for entry in deps.get(key, []):
                other = node_for(entry)
                if other and other != me:
                    add_edge(*((other, me) if waits else (me, other)), entry)

    overlaps: dict[frozenset[str], tuple[str, str, str]] = {}
    for number, deps in deps_by_number.items():
        me = f"#{number}"
        for overlap in deps.get("overlaps", []):
            other = overlap.get("ref") or ""
            pair = frozenset((me, other))
            if len(pair) < 2 or not pair <= mine or pair in overlaps \
                    or (me, other) in edges or (other, me) in edges:
                continue
            what = f"{overlap.get('count', 0)} file(s) in common"
            if overlap.get("clashes"):
                what += f", {len(overlap['clashes'])} line clash(es)"
            if overlap.get("files"):
                what += ": " + ", ".join(overlap["files"])
            overlaps[pair] = (me, other, what)
    return Graph(nodes, list(edges.values()), list(overlaps.values()))


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def _groups(graph: Graph) -> tuple[list[list[str]], list[str]]:
    """The connected groups of nodes, largest first, and the nodes on their own."""
    neighbours: dict[str, set[str]] = {n: set() for n in graph.nodes}
    for a, b in [(e.source, e.target) for e in graph.edges] + \
                [(a, b) for a, b, _ in graph.overlaps]:
        neighbours[a].add(b)
        neighbours[b].add(a)
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


def _columns(group: list[str], edges: list[Edge]) -> list[list[str]]:
    """Each node one column right of its rightmost prerequisite (an edge that
    closes a cycle is ignored), then ordered within the columns to keep arrows
    short."""
    members = set(group)
    succ: dict[str, list[str]] = {n: [] for n in group}
    for e in edges:
        if e.source in members and e.target in members:
            succ[e.source].append(e.target)

    # Depth first: dropping the edges back to a node still on the stack leaves
    # a DAG, and the order nodes finish in is a reversed topological order.
    after: dict[str, list[str]] = {n: [] for n in group}
    finished: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> None:
        on_stack.add(node)
        for nxt in succ[node]:
            if nxt in on_stack:
                continue
            after[node].append(nxt)
            if nxt not in finished:
                visit(nxt)
        on_stack.discard(node)
        finished.append(node)

    for node in sorted(group):
        if node not in finished:
            visit(node)

    level = dict.fromkeys(group, 0)
    before: dict[str, list[str]] = {n: [] for n in group}
    for node in reversed(finished):
        for nxt in after[node]:
            level[nxt] = max(level[nxt], level[node] + 1)
            before[nxt].append(node)
    columns: list[list[str]] = [[] for _ in range(max(level.values()) + 1)]
    for node in sorted(group):
        columns[level[node]].append(node)

    def by_barycentre(column: list[str], fixed: list[str], links: dict[str, list[str]]) -> None:
        pos = {n: i for i, n in enumerate(fixed)}
        def key(node: str) -> float:
            linked = [pos[n] for n in links[node] if n in pos]
            return sum(linked) / len(linked) if linked else len(fixed)
        column.sort(key=key)

    for _ in range(4):
        for i in range(1, len(columns)):
            by_barycentre(columns[i], columns[i - 1], before)
        for i in range(len(columns) - 2, -1, -1):
            by_barycentre(columns[i], columns[i + 1], after)
    return columns


def _layout(graph: Graph) -> tuple[dict[str, tuple[float, float]], float, float | None, float]:
    """Top-left corner of every box, the width of the graph, the top of the
    'no relationship' grid (None without one) and the bottom of it all."""
    groups, alone = _groups(graph)
    boxes: dict[str, tuple[float, float]] = {}
    y, width = float(TOP), 0.0
    for group in groups:
        columns = _columns(group, graph.edges)
        height = max(map(len, columns)) * (NODE_H + GAP) - GAP
        for i, column in enumerate(columns):
            top = y + (height - (len(column) * (NODE_H + GAP) - GAP)) / 2
            for j, node in enumerate(column):
                boxes[node] = (MARGIN + i * (NODE_W + COL_GAP), top + j * (NODE_H + GAP))
        width = max(width, len(columns) * (NODE_W + COL_GAP) - COL_GAP)
        y += height + GROUP_GAP
    alone_top = None
    if alone:
        per_row = max(3, int((width + GAP) // (NODE_W + GAP)))
        alone_top = y + 22 if groups else y
        for k, node in enumerate(sorted(alone)):
            boxes[node] = (MARGIN + k % per_row * (NODE_W + GAP),
                           alone_top + k // per_row * (NODE_H + GAP))
        width = max(width, min(len(alone), per_row) * (NODE_W + GAP) - GAP)
        y = alone_top + ((len(alone) - 1) // per_row + 1) * (NODE_H + GAP)
    return boxes, width, alone_top, y


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _stroke(style: tuple[str, str], width: float) -> str:
    colour, dash = style
    return (f'fill="none" stroke="{colour}" stroke-width="{width}"'
            + (f' stroke-dasharray="{dash}"' if dash else ""))


def _curve(x1: float, y1: float, x2: float, y2: float) -> str:
    """From the right side of one box to the left side of another; backwards
    (only in a cycle) it swings out and back in."""
    bend = max(40.0, (x2 - x1) / 2) if x2 > x1 else 80.0
    return f"M{x1:.0f},{y1:.1f} C{x1 + bend:.0f},{y1:.1f} {x2 - bend:.0f},{y2:.1f} {x2:.0f},{y2:.1f}"


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


def _ports(graph: Graph, boxes: dict[str, tuple[float, float]]) -> tuple[list[float], list[float]]:
    """Where each edge leaves its source and enters its target: spread along
    the side of the box, in the order of the boxes at the other end."""
    out_y, in_y = [0.0] * len(graph.edges), [0.0] * len(graph.edges)
    for port, own, other in ((out_y, "source", "target"), (in_y, "target", "source")):
        by_node: dict[str, list[int]] = {}
        for k, e in enumerate(graph.edges):
            by_node.setdefault(getattr(e, own), []).append(k)
        for node, ks in by_node.items():
            ks.sort(key=lambda k: boxes[getattr(graph.edges[k], other)][1])
            for slot, k in enumerate(ks, 1):
                port[k] = boxes[node][1] + NODE_H * slot / (len(ks) + 1)
    return out_y, in_y


def render_svg(graph: Graph, heading: str = "Open pull requests", generated: str = "") -> str:
    boxes, width, alone_top, bottom = _layout(graph)

    # The legend shows only what is drawn.
    legend = [(v, VERDICT_STYLE[v]) for v in VERDICT_STYLE if any(e.verdict == v for e in graph.edges)]
    if graph.overlaps:
        legend.append(("shares files", OVERLAP_STYLE))
    legend_w = sum(44 + 7 * len(name) for name, _ in legend)
    total_w = max(width, legend_w, 760) + 2 * MARGIN
    total_h = bottom + MARGIN

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="Helvetica, Arial, sans-serif">',
        "<style>.label{font-size:13px;font-weight:bold;fill:#111827}"
        ".title{font-size:12px;fill:#1f2937}.status,.small{font-size:11px;fill:#4b5563}"
        ".head{font-size:16px;font-weight:bold;fill:#111827}</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<defs>",
        *(f'<marker id="arrow-{v}" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" '
          f'markerHeight="8" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
          for v, (colour, _) in VERDICT_STYLE.items()),
        "</defs>",
    ]
    summary = (f"{sum(n.mine for n in graph.nodes.values())} open PR(s), "
               f"{len(graph.edges)} dependency(ies), {len(graph.overlaps)} file overlap(s)"
               + (f" - generated {generated}" if generated else "")
               + ". Arrows point from the PR to merge first.")
    out.append(f'<text x="{MARGIN}" y="{MARGIN + 6}" class="head">{escape(heading)}</text>'
               f'<text x="{MARGIN}" y="{MARGIN + 24}" class="small">{escape(summary)}</text>')
    x = MARGIN
    for name, style in legend:
        out.append(f'<path d="M{x},{MARGIN + 44} h22" {_stroke(style, 2.5)}/>'
                   f'<text x="{x + 27}" y="{MARGIN + 48}" class="small">{name}</text>')
        x += 44 + 7 * len(name)
    if alone_top is not None and alone_top > TOP:
        out.append(f'<text x="{MARGIN}" y="{alone_top - 8:.0f}" class="small">'
                   f'No relationship found:</text>')

    for a, b, what in graph.overlaps:
        (ax, ay), (bx, by) = sorted((boxes[a], boxes[b]))
        ay, by = ay + NODE_H / 2, by + NODE_H / 2
        if ax == bx:   # same column: a bracket on the right side
            right = ax + NODE_W
            d = f"M{right:.0f},{ay:.1f} C{right + 60:.0f},{ay:.1f} {right + 60:.0f},{by:.1f} {right:.0f},{by:.1f}"
        else:
            d = _curve(ax + NODE_W, ay, bx, by)
        out.append(f'<path d="{d}" {_stroke(OVERLAP_STYLE, 2)}>'
                   f'<title>{escape(a)} and {escape(b)}: {escape(what)}</title></path>')
    out_y, in_y = _ports(graph, boxes)
    for k in sorted(range(len(graph.edges)), key=lambda k: -VERDICT_RANK[graph.edges[k].verdict]):
        e = graph.edges[k]   # the strongest drawn last, on top
        why = "\n".join([f"{e.source} before {e.target} - {e.verdict}"] + e.reasons)
        d = _curve(boxes[e.source][0] + NODE_W, out_y[k], boxes[e.target][0], in_y[k])
        out.append(f'<path d="{d}" {_stroke(VERDICT_STYLE[e.verdict], 2.2)} '
                   f'marker-end="url(#arrow-{e.verdict})"><title>{escape(why)}</title></path>')
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
