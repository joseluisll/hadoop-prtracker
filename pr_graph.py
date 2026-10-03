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
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable
from xml.sax.saxutils import escape

DEFAULT_GRAPH_FILE = "pr-graph.svg"

# Strongest first: when both PRs report the same pair, the first one wins.
VERDICT_RANK = ["CONFIRMED", "CI-FIX", "DISCOVERED", "LIKELY", "UNVERIFIED", "WEAK",
                "UNSUPPORTED", "STALE"]
VERDICT_STYLE = {  # colour, dash pattern
    "CONFIRMED": ("#1f6feb", ""),
    "CI-FIX": ("#d97706", ""),
    "DISCOVERED": ("#7c3aed", ""),
    "LIKELY": ("#0891b2", ""),
    "UNVERIFIED": ("#6b7280", "6 4"),
    "WEAK": ("#9ca3af", "6 4"),
    "UNSUPPORTED": ("#dc2626", "2 4"),
    "STALE": ("#dc2626", "2 4"),
}
STATUS_FILL = {
    "READY TO MERGE": "#bbf7d0",
    "WAITING FOR REVIEW": "#dcfce7",
    "CI RUNNING": "#dbeafe",
    "CHANGES REQUESTED": "#ffedd5",
    "CONFLICTS": "#fde68a",
    "YETUS FAILED": "#fee2e2",
    "CI FAILED": "#fecaca",
    "DRAFT": "#f3f4f6",
}
OVERLAP_COLOUR = "#9ca3af"

NODE_W, NODE_H = 260, 62
COL_GAP, ROW_GAP, COMPONENT_GAP = 120, 24, 48
MARGIN = 24
TITLE_CHARS = 40


@dataclass
class Node:
    id: str                 # '#8704', or a JIRA key with no PR
    label: str              # '#8704  HADOOP-19972'
    title: str
    status: str             # status of your PR, or 'by bob, OPEN' for somebody else's
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
    nodes: dict[str, Node]
    edges: list[Edge]
    overlaps: list[tuple[str, str, str]]    # (a, b, what they share)


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #
def _rank(verdict: str) -> int:
    return VERDICT_RANK.index(verdict) if verdict in VERDICT_RANK else len(VERDICT_RANK)


def build_graph(prs: Iterable[dict[str, Any]], deps_by_number: dict[int, dict[str, Any]]) -> Graph:
    """The graph of your open PRs.

    ``prs`` has one dict per open PR: number, title, status, url and jira (may
    be empty). ``deps_by_number`` maps a PR number to what
    analyze_pr.collect_dependencies returned for it; a PR missing from it was
    not analysed and only shows up as a box.
    """
    nodes: dict[str, Node] = {}
    for pr in prs:
        ref = f"#{pr['number']}"
        label = f"{ref}  {pr['jira']}" if pr.get("jira") else ref
        nodes[ref] = Node(ref, label, pr.get("title") or "", pr.get("status") or "",
                          pr.get("url") or "")
    mine = set(nodes)

    edges: dict[tuple[str, str], Edge] = {}
    overlaps: dict[frozenset[str], tuple[str, str, str]] = {}

    def add_edge(source: str, target: str, entry: dict[str, Any]) -> None:
        if source == target:
            return
        verdict = entry.get("verdict") or "UNVERIFIED"
        reasons = [str(r) for r in entry.get("reasons") or []]
        if entry.get("verdict_reason"):
            reasons.append(f"{verdict}: {entry['verdict_reason']}")
        edge = edges.get((source, target))
        if edge is None:
            edges[(source, target)] = Edge(source, target, verdict, reasons)
            return
        if _rank(verdict) < _rank(edge.verdict):
            edge.verdict = verdict
        edge.reasons += [r for r in reasons if r not in edge.reasons]

    def node_for(entry: dict[str, Any]) -> str | None:
        """The node of a dependency; None when it is merged, closed or unknown."""
        ref = entry.get("ref") or ""
        if ref in mine:
            return ref
        if not entry.get("open", True):
            return None
        if ref not in nodes:
            jira = entry.get("jira") or ""
            if ref.startswith("#"):
                label = f"{ref}  {jira}" if jira else ref
                who = entry.get("author") or "unknown"
                status = f"by {who}, {(entry.get('state') or 'unknown').lower()}"
            else:
                label, status = jira or ref, "JIRA, no pull request"
            nodes[ref] = Node(ref, label, entry.get("title") or "", status,
                              entry.get("url") or "", mine=False)
        return ref

    for number, deps in deps_by_number.items():
        me = f"#{number}"
        if me not in mine or not deps:
            continue
        for entry in deps.get("depends_on", []):
            other = node_for(entry)
            if other:
                add_edge(other, me, entry)
        for entry in deps.get("blocks", []):
            other = node_for(entry)
            if other:
                add_edge(me, other, entry)
    for number, deps in deps_by_number.items():
        me = f"#{number}"
        for overlap in (deps or {}).get("overlaps", []):
            other = overlap.get("ref") or ""
            pair = frozenset((me, other))
            if me not in mine or other not in mine or me == other or pair in overlaps \
                    or (me, other) in edges or (other, me) in edges:
                continue
            what = f"{overlap.get('count', 0)} file(s) in common"
            if overlap.get("clashes"):
                what += f", {len(overlap['clashes'])} line clash(es)"
            files = ", ".join(overlap.get("files") or [])
            overlaps[pair] = (me, other, f"{what}: {files}" if files else what)
    return Graph(nodes, list(edges.values()), list(overlaps.values()))


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def _components(graph: Graph) -> tuple[list[list[str]], list[str]]:
    """Connected groups of nodes (largest first), and the nodes on their own."""
    neighbours: dict[str, set[str]] = {n: set() for n in graph.nodes}
    for e in graph.edges:
        neighbours[e.source].add(e.target)
        neighbours[e.target].add(e.source)
    for a, b, _ in graph.overlaps:
        neighbours[a].add(b)
        neighbours[b].add(a)
    seen: set[str] = set()
    groups: list[list[str]] = []
    alone: list[str] = []
    for start in graph.nodes:
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            current = stack.pop()
            group.append(current)
            for nxt in sorted(neighbours[current] - seen):
                seen.add(nxt)
                stack.append(nxt)
        (groups if len(group) > 1 else alone).append(group if len(group) > 1 else start)
    groups.sort(key=lambda g: (-len(g), min(g)))
    return groups, alone


def _columns(group: list[str], edges: list[Edge]) -> list[list[str]]:
    """Longest-path layering of one group: each node one column right of its
    rightmost prerequisite. Edges closing a cycle are left out."""
    members = set(group)
    succ: dict[str, list[str]] = {n: [] for n in group}
    for e in edges:
        if e.source in members and e.target in members:
            succ[e.source].append(e.target)
    acyclic: dict[str, list[str]] = {n: [] for n in group}
    state: dict[str, int] = {}

    def visit(node: str) -> None:  # depth-first, drops back edges
        state[node] = 1
        for nxt in succ[node]:
            if state.get(nxt) == 1:
                continue
            acyclic[node].append(nxt)
            if nxt not in state:
                visit(nxt)
        state[node] = 2

    for node in sorted(group):
        if node not in state:
            visit(node)

    level = {n: 0 for n in group}
    order: list[str] = []
    done: set[str] = set()

    def topo(node: str) -> None:
        done.add(node)
        for nxt in acyclic[node]:
            if nxt not in done:
                topo(nxt)
        order.append(node)

    for node in sorted(group):
        if node not in done:
            topo(node)
    for node in reversed(order):
        for nxt in acyclic[node]:
            level[nxt] = max(level[nxt], level[node] + 1)

    columns: list[list[str]] = [[] for _ in range(max(level.values()) + 1)]
    for node in sorted(group):
        columns[level[node]].append(node)

    # A few barycentre sweeps to keep arrows short and uncrossed.
    preds: dict[str, list[str]] = {n: [] for n in group}
    for node, nexts in acyclic.items():
        for nxt in nexts:
            preds[nxt].append(node)
    for _ in range(4):
        for i in range(1, len(columns)):
            pos = {n: j for j, n in enumerate(columns[i - 1])}
            columns[i].sort(key=lambda n: (sum(pos[p] for p in preds[n] if p in pos)
                                           / max(1, sum(1 for p in preds[n] if p in pos)))
                            if any(p in pos for p in preds[n]) else len(pos))
        for i in range(len(columns) - 2, -1, -1):
            pos = {n: j for j, n in enumerate(columns[i + 1])}
            columns[i].sort(key=lambda n: (sum(pos[s] for s in acyclic[n] if s in pos)
                                           / max(1, sum(1 for s in acyclic[n] if s in pos)))
                            if any(s in pos for s in acyclic[n]) else len(pos))
    return columns


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _node_svg(node: Node, x: float, y: float) -> str:
    fill = STATUS_FILL.get(node.status, "#f9fafb") if node.mine else "#ffffff"
    dash = "" if node.mine else ' stroke-dasharray="5 3"'
    title = node.title
    if title.upper().startswith(node.label.split()[-1].upper()):
        title = title[len(node.label.split()[-1]):].lstrip(" .:-")  # 'HADOOP-1. x' -> 'x'
    body = (
        f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8" fill="{fill}" '
        f'stroke="#374151" stroke-width="1.2"{dash}/>'
        f'<text x="{x + 10}" y="{y + 18}" class="label">{escape(node.label)}</text>'
        f'<text x="{x + 10}" y="{y + 36}" class="title">{escape(_short(title, TITLE_CHARS))}</text>'
        f'<text x="{x + 10}" y="{y + 53}" class="status">{escape(node.status)}</text>'
        f'<title>{escape(node.label)}: {escape(node.title)}</title>'
    )
    if node.url:
        return f'<a href="{escape(node.url, {chr(34): "&quot;"})}" target="_blank">{body}</a>'
    return body


def _path(x1: float, y1: float, x2: float, y2: float) -> str:
    if x2 > x1:
        bend = max(40.0, (x2 - x1) / 2)
        return f"M{x1},{y1} C{x1 + bend},{y1} {x2 - bend},{y2} {x2},{y2}"
    # Backwards, only in a cycle: swing out to the right and back in.
    return f"M{x1},{y1} C{x1 + 80},{y1} {x2 - 80},{y2} {x2},{y2}"


def render_svg(graph: Graph, heading: str = "", generated: str = "") -> str:
    groups, alone = _components(graph)
    boxes: dict[str, tuple[float, float]] = {}
    y = MARGIN + 70            # room for the heading and the legend
    width = 0.0
    for group in groups:
        columns = _columns(group, graph.edges)
        height = max(len(c) for c in columns) * (NODE_H + ROW_GAP) - ROW_GAP
        for i, column in enumerate(columns):
            col_h = len(column) * (NODE_H + ROW_GAP) - ROW_GAP
            top = y + (height - col_h) / 2
            for j, node in enumerate(column):
                boxes[node] = (MARGIN + i * (NODE_W + COL_GAP), top + j * (NODE_H + ROW_GAP))
        width = max(width, len(columns) * (NODE_W + COL_GAP) - COL_GAP)
        y += height + COMPONENT_GAP

    per_row = max(1, int((max(width, 3 * NODE_W + 2 * COL_GAP) + COL_GAP) // (NODE_W + ROW_GAP)))
    alone_top = y + (22 if alone and groups else 0)
    for k, node in enumerate(sorted(alone, key=lambda n: (not graph.nodes[n].mine, n))):
        boxes[node] = (MARGIN + (k % per_row) * (NODE_W + ROW_GAP),
                       alone_top + (k // per_row) * (NODE_H + ROW_GAP))
    if alone:
        width = max(width, min(len(alone), per_row) * (NODE_W + ROW_GAP) - ROW_GAP)
        y = alone_top + ((len(alone) - 1) // per_row + 1) * (NODE_H + ROW_GAP)
    total_w = max(width, 760) + 2 * MARGIN
    total_h = y + MARGIN

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="Helvetica, Arial, sans-serif">',
        "<style>.label{font-size:13px;font-weight:bold;fill:#111827}"
        ".title{font-size:12px;fill:#1f2937}.status{font-size:11px;fill:#4b5563}"
        ".head{font-size:16px;font-weight:bold;fill:#111827}.small{font-size:11px;fill:#4b5563}"
        "</style>",
        f'<rect width="100%" height="100%" fill="#ffffff"/>',
        "<defs>",
    ]
    for verdict, (colour, _) in VERDICT_STYLE.items():
        out.append(f'<marker id="arrow-{verdict}" viewBox="0 0 10 10" refX="10" refY="5" '
                   f'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                   f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>')
    out.append("</defs>")

    mine = sum(1 for n in graph.nodes.values() if n.mine)
    text = heading or "Open pull requests"
    summary = (f"{mine} open PR(s), {len(graph.edges)} dependency(ies), "
               f"{len(graph.overlaps)} file overlap(s)")
    if generated:
        summary += f" - generated {generated}"
    out.append(f'<text x="{MARGIN}" y="{MARGIN + 6}" class="head">{escape(text)}</text>')
    out.append(f'<text x="{MARGIN}" y="{MARGIN + 24}" class="small">{escape(summary)}. '
               f'Arrows point from the PR to merge first.</text>')
    lx = MARGIN
    for verdict, (colour, dash) in list(VERDICT_STYLE.items()) + [("shares files",
                                                                  (OVERLAP_COLOUR, "1 3"))]:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<line x1="{lx}" y1="{MARGIN + 44}" x2="{lx + 22}" y2="{MARGIN + 44}" '
                   f'stroke="{colour}" stroke-width="2.5"{dash_attr}/>'
                   f'<text x="{lx + 27}" y="{MARGIN + 48}" class="small">{verdict}</text>')
        lx += 44 + 7 * len(verdict)
    if alone and groups:
        out.append(f'<text x="{MARGIN}" y="{alone_top - 8}" class="small">'
                   f'No relationship found:</text>')

    # Spread the arrows along the side of each box, in the order of the box at
    # the other end, so they do not all meet in one point.
    out_port: dict[int, float] = {}
    in_port: dict[int, float] = {}
    for side, ends, port in (("source", "target", out_port), ("target", "source", in_port)):
        by_node: dict[str, list[int]] = {}
        for k, e in enumerate(graph.edges):
            by_node.setdefault(getattr(e, side), []).append(k)
        for node, ks in by_node.items():
            ks.sort(key=lambda k: boxes[getattr(graph.edges[k], ends)][1])
            for slot, k in enumerate(ks):
                port[k] = boxes[node][1] + NODE_H * (slot + 1) / (len(ks) + 1)

    for a, b, what in graph.overlaps:
        (ax, ay), (bx, by) = boxes[a], boxes[b]
        if ax > bx:
            (ax, ay), (bx, by), a, b = (bx, by), (ax, ay), b, a
        if ax == bx:   # same column: a bracket on the right side
            right = ax + NODE_W
            d = (f"M{right},{ay + NODE_H / 2} C{right + 60},{ay + NODE_H / 2} "
                 f"{right + 60},{by + NODE_H / 2} {right},{by + NODE_H / 2}")
        else:
            d = _path(ax + NODE_W, ay + NODE_H / 2, bx, by + NODE_H / 2)
        out.append(f'<path d="{d}" fill="none" stroke="{OVERLAP_COLOUR}" stroke-width="2" '
                   f'stroke-dasharray="1 3"><title>{escape(a)} and {escape(b)}: '
                   f'{escape(what)}</title></path>')
    for k, e in sorted(enumerate(graph.edges), key=lambda ke: -_rank(ke[1].verdict)):
        # Strongest drawn last, on top.
        sx, tx = boxes[e.source][0], boxes[e.target][0]
        colour, dash = VERDICT_STYLE.get(e.verdict, VERDICT_STYLE["UNVERIFIED"])
        verdict = e.verdict if e.verdict in VERDICT_STYLE else "UNVERIFIED"
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        why = "\n".join([f"{e.source} before {e.target} - {e.verdict}"] + e.reasons)
        out.append(f'<path d="{_path(sx + NODE_W, out_port[k], tx, in_port[k])}" '
                   f'fill="none" stroke="{colour}" stroke-width="2.2"{dash_attr} '
                   f'marker-end="url(#arrow-{verdict})"><title>{escape(why)}</title></path>')
    for ref, node in graph.nodes.items():
        out.append(_node_svg(node, *boxes[ref]))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_svg(path: str, svg: str) -> str:
    """Write the file in one go, so a viewer never sees half of it."""
    path = os.path.abspath(path)
    fd, tmp = tempfile.mkstemp(prefix=".pr-graph-", suffix=".svg", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(svg)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)   # mkstemp makes it private; a plain file is not
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
