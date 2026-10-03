#!/usr/bin/env python3
"""Terminal UI over the pull request tools that sit next to this script.

One screen drives what the command-line scripts do separately:

* Pull requests - your open PRs into apache/hadoop:trunk, drafts included,
  and why each one is not merged yet (list_upstream_prs.py);
* Analysis     - the full report of one PR: CI, reviews, JIRA, dependencies,
  and what to do next (analyze_pr.py);
* Dependencies - the JIRA links (one per pair) and the 'Depends on' /
  'Required by' lists of your PR descriptions (both sides when both PRs are
  yours) the evidence asks for, for one PR or all of them, applied one change
  at a time (fix_dependencies.py); 'Plan all' also draws how your open PRs
  relate to each other in pr-graph.svg, in the working directory (pr_graph.py);
* Branches     - the stale branches of the fork (list_stale_branches.py),
  report only.

*Nothing is written without a yes.* Every JIRA or GitHub write is shown in
full in a dialog and needs its own confirmation; there is no 'apply all'.
Credentials are the ones the scripts already use: $GITHUB_TOKEN / $GH_TOKEN /
'gh auth token' for GitHub, and JIRA_TOKEN (or JIRA_PAT) from the environment
for JIRA. They are never shown.

Needs the 'textual' package. Run it from any directory:

    python pr_manager.py
    python pr_manager.py --author someone --no-ci-search
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import analyze_pr as core
    import fix_dependencies as fd
    import list_upstream_prs as lup
    import pr_graph
except ImportError as exc:  # pragma: no cover - misplaced file
    raise SystemExit(f"pr_manager.py must sit next to the other PR scripts ({exc}).")

try:
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label,
                                 Static, TabbedContent, TabPane)
    from textual.worker import get_current_worker
except ImportError:  # pragma: no cover
    raise SystemExit("pr_manager.py needs Textual: python -m pip install textual")

STATE_STYLE = {"pending": "", "applied": "bold green", "failed": "bold red", "skipped": "dim"}
BRANCH_STYLE = {"STALE": "red", "CANDIDATE": "yellow", "ACTIVE": "green"}
# In-memory caches of analyze_pr.py dropped by a refresh; the log caches on
# disk stay, a build log never changes.
CORE_CACHES = ("_MINI_CACHE", "_PEER_CACHE", "_JIRA_PR_CACHE", "_DIFF_CACHE", "_CI_CACHE",
               "_FIXER_SEARCH_CACHE", "_JIRA_SEARCH_CACHE")


@dataclass
class Settings:
    repo: str
    author: str
    base: str
    jira_base: str
    repo_path: str | None
    stale_days: int
    token: str | None


@dataclass
class Bundle:
    """Everything known about one PR: fetched once, shown in two tabs."""
    target: str
    pr: dict[str, Any] | None
    jira: core.Jira | None
    deps: dict[str, Any] | None
    report: core.Report
    search_external: bool


@dataclass
class PlanItem:
    change: fd.Change
    number: int | None
    state: str = "pending"
    message: str = ""


@dataclass
class PlanOptions:
    search_external: bool = True
    include_weak: bool = False
    jira_only: bool = False
    pr_only: bool = False


def text(value: Any, style: str = "") -> Text:
    """Literal text: PR titles and evidence are full of [brackets]."""
    return Text("" if value is None else str(value), style=style)


def jira_key_in(title: str) -> str:
    match = core.JIRA_IN_TEXT_RE.match((title or "").strip())
    return match.group(0).upper() if match else ""


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #
class ConfirmApply(ModalScreen[bool]):
    """The only way a change gets written: shown in full, answered one by one."""

    BINDINGS = [
        Binding("y", "answer(True)", "Apply"),
        Binding("n,escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, body: str, where: str) -> None:
        super().__init__()
        self.body = body
        self.where = where

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(text(f"Apply this change? It writes to {self.where}.", "bold"))
            with VerticalScroll(id="dialog-body"):
                yield Static(text(self.body))
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel (n)", id="cancel", variant="primary")
                yield Button("Apply (y)", id="apply", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "apply")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class AskTarget(ModalScreen[str]):
    """A PR number or a JIRA id, for a PR that is not in the list."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="small"):
            yield Label("PR number or JIRA id (e.g. 8704 or HADOOP-19972):")
            yield Input(id="target")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")


class AskLink(ModalScreen[str]):
    """An 'is blocked by' link to propose by hand; it still needs confirming."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, blocked: str) -> None:
        super().__init__()
        self.blocked = blocked

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="small"):
            yield Label("Propose a JIRA link: BLOCKED 'is blocked by' BLOCKER")
            yield Input(value=self.blocked, placeholder="blocked, e.g. HADOOP-19972", id="blocked")
            yield Input(placeholder="blocker, e.g. HADOOP-19993", id="blocker")
            yield Label(text("It is added to the plan; applying it is asked for separately.",
                             "dim"))

    def on_mount(self) -> None:
        self.query_one("#blocker" if self.blocked else "#blocked", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "blocked":
            self.query_one("#blocker", Input).focus()
            return
        blocked = self.query_one("#blocked", Input).value.strip()
        blocker = self.query_one("#blocker", Input).value.strip()
        self.dismiss(f"{blocked}:{blocker}")

    def action_cancel(self) -> None:
        self.dismiss("")


# --------------------------------------------------------------------------- #
# The application
# --------------------------------------------------------------------------- #
class PRManager(App):
    TITLE = "PR manager"
    CSS = """
    #tabs { height: 1fr; }
    #pr-table, #branch-table { height: 1fr; }
    .detail { height: auto; max-height: 14; border: round $primary; padding: 0 1; }
    .status { height: 1; padding: 0 1; color: $text-muted; }
    #analysis-scroll { height: 1fr; padding: 0 1; }
    #options { height: auto; }
    #options Checkbox { width: auto; border: none; }
    #plan-top { height: 3fr; }
    #plan-table { width: 3fr; }
    #plan-detail-scroll { width: 2fr; border: round $primary; padding: 0 1; }
    #plan-notes-scroll { height: 2fr; border: round $secondary; padding: 0 1; }
    ConfirmApply, AskTarget, AskLink { align: center middle; }
    #dialog { width: 90%; height: 85%; border: thick $warning; background: $surface;
              padding: 1 2; }
    #dialog.small { height: auto; width: 70; border: thick $primary; }
    #dialog-body { height: 1fr; border: round $primary; padding: 0 1; margin: 1 0; }
    #dialog-buttons { height: auto; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 2; }
    """
    BINDINGS = [
        Binding("a", "analyse", "Analyse"),
        Binding("d", "plan", "Plan deps"),
        Binding("D", "plan_all", "Plan all"),
        Binding("l", "add_link", "Add link"),
        Binding("g", "goto", "Go to PR/JIRA"),
        Binding("o", "open_pr", "Open PR"),
        Binding("j", "open_jira", "Open JIRA"),
        Binding("r", "reload", "Reload"),
        Binding("f", "fetch_branches", "Fetch branches", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings, options: PlanOptions) -> None:
        super().__init__()
        self.settings = settings
        self.options = options
        self.rows: list[lup.PullRequestRow] = []
        self.bundles: dict[int, Bundle] = {}
        self.current: Bundle | None = None
        self.plan: list[PlanItem] = []
        self.plan_notes: list[str] = []
        self.plan_scope: list[int] = []
        self.plan_graph = False
        self.branches: list[dict[str, Any]] = []
        self.sub_title = f"{settings.author} -> {settings.repo}:{settings.base}"

    # ----- layout ----------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="prs", id="tabs"):
            with TabPane("Pull requests", id="prs"):
                yield Static(id="pr-status", classes="status")
                yield DataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
                yield Static(id="pr-detail", classes="detail")
            with TabPane("Analysis", id="analysis"):
                yield Static(id="analysis-status", classes="status")
                with VerticalScroll(id="analysis-scroll"):
                    yield Static(id="analysis-text")
            with TabPane("Dependencies", id="deps"):
                yield Static(id="plan-status", classes="status")
                with Horizontal(id="options"):
                    yield Checkbox("search others' PRs and JIRA", self.options.search_external,
                                   id="opt-search")
                    yield Checkbox("offer WEAK link deletions", self.options.include_weak,
                                   id="opt-weak")
                    yield Checkbox("JIRA only", self.options.jira_only, id="opt-jira")
                    yield Checkbox("PR descriptions only", self.options.pr_only, id="opt-pr")
                with Horizontal(id="plan-top"):
                    yield DataTable(id="plan-table", cursor_type="row", zebra_stripes=True)
                    with VerticalScroll(id="plan-detail-scroll"):
                        yield Static(id="plan-detail")
                with VerticalScroll(id="plan-notes-scroll"):
                    yield Static(id="plan-notes")
            with TabPane("Branches", id="branches"):
                yield Static(id="branch-status", classes="status")
                yield DataTable(id="branch-table", cursor_type="row", zebra_stripes=True)
                yield Static(id="branch-detail", classes="detail")
        yield Static(id="creds", classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pr-table", DataTable).add_columns("PR", "JIRA", "Status", "Updated",
                                                           "Title")
        self.query_one("#plan-table", DataTable).add_columns("#", "State", "Kind", "Change")
        self.query_one("#branch-table", DataTable).add_columns("Branch", "Status", "Last commit",
                                                               "Age", "Ahead", "Behind")
        self.show_credentials()
        self.set_status("analysis", "select a PR in 'Pull requests' and press a, or press g")
        self.set_status("deps", "d plans the selected PR, D all open PRs; enter applies the "
                                "highlighted change after asking")
        self.set_status("branches", "r lists the branches from the refs already fetched; "
                                    "f fetches first")
        self.load_prs()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # While a dialog is open, only the dialog's own keys work.
        return action == "quit" or not isinstance(self.screen, ModalScreen)

    # ----- small helpers ---------------------------------------------------- #
    def show_credentials(self) -> None:
        github = "found" if self.settings.token else "missing (set GITHUB_TOKEN or 'gh auth login')"
        jira = "set" if fd.jira_token() else "not set (needed only to write to JIRA)"
        self.query_one("#creds", Static).update(
            text(f"GitHub token: {github}   JIRA_TOKEN: {jira}   "
                 f"- every write is confirmed one at a time"))

    def set_status(self, tab: str, message: str) -> None:
        widget = {"prs": "#pr-status", "analysis": "#analysis-status", "deps": "#plan-status",
                  "branches": "#branch-status"}[tab]
        self.query_one(widget, Static).update(text(message))

    def status_from_thread(self, tab: str, message: str) -> None:
        self.call_from_thread(self.set_status, tab, message)

    def fail_from_thread(self, tab: str, what: str, exc: BaseException) -> None:
        message = f"{what} failed: {exc}"
        self.call_from_thread(self.set_status, tab, message)
        self.call_from_thread(self.notify, message, severity="error", timeout=10)

    @property
    def tab(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def show_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = name

    def selected_row(self) -> lup.PullRequestRow | None:
        table = self.query_one("#pr-table", DataTable)
        if not self.rows or table.cursor_row is None or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def selected_target(self) -> str | None:
        """The PR the current tab is about."""
        if self.tab in ("analysis", "deps") and self.current and self.current.pr:
            return str(self.current.pr["number"])
        row = self.selected_row()
        return str(row.number) if row else None

    def selected_item(self) -> PlanItem | None:
        table = self.query_one("#plan-table", DataTable)
        if not self.plan or table.cursor_row is None or table.cursor_row >= len(self.plan):
            return None
        return self.plan[table.cursor_row]

    # ----- pull requests ---------------------------------------------------- #
    @work(thread=True, exclusive=True, group="prs")
    def load_prs(self) -> None:
        s = self.settings
        self.status_from_thread("prs", f"loading the open PRs of {s.author} ...")
        try:
            pulls = lup.fetch_pull_requests(s.repo, s.author, s.token)
            bots = list(lup.DEFAULT_BOTS)
            # Drafts included: a stacked draft is where dependencies pile up.
            rows = [
                lup.evaluate(pr, bots, s.stale_days) for pr in pulls
                if s.base == "*" or pr.get("baseRefName") == s.base
            ]
        except (Exception, SystemExit) as exc:
            self.fail_from_thread("prs", "loading the PRs", exc)
            return
        rows.sort(key=lambda r: r.number, reverse=True)
        self.call_from_thread(self.fill_prs, rows)

    def fill_prs(self, rows: list[lup.PullRequestRow]) -> None:
        self.rows = rows
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        for row in rows:
            title = row.title
            key = jira_key_in(title)
            if key and title.upper().startswith(key):
                title = title[len(key):].lstrip(". :")
            table.add_row(text(f"#{row.number}"), text(key), text(row.status),
                          text((row.updated_at or "")[:10]), text(title), key=str(row.number))
        self.set_status("prs", f"{len(rows)} open PR(s) of {self.settings.author} into "
                               f"{self.settings.repo}:{self.settings.base} - enter/a analyses, "
                               f"d plans dependencies, o opens it")
        self.show_pr_detail()

    @on(DataTable.RowHighlighted, "#pr-table")
    def show_pr_detail(self) -> None:
        row = self.selected_row()
        detail = self.query_one("#pr-detail", Static)
        if row is None:
            detail.update("")
            return
        detail.update(text(f"#{row.number} {row.title}\nbranch {row.head_repo}:{row.branch} "
                           f"-> {row.base}   {row.url}\n\n{row.comments}"))

    @on(DataTable.RowSelected, "#pr-table")
    def pr_chosen(self) -> None:
        self.action_analyse()

    # ----- analysis --------------------------------------------------------- #
    def action_analyse(self) -> None:
        target = self.selected_target()
        if target:
            self.show_tab("analysis")
            self.analyse(target)

    def action_goto(self) -> None:
        def chosen(target: str | None) -> None:
            if target:
                self.show_tab("analysis")
                self.analyse(target)
        self.push_screen(AskTarget(), chosen)

    def bundle_for(self, target: str) -> Bundle:
        """Fetch a PR with its JIRA and dependencies, or reuse what was fetched."""
        s = self.settings
        search = self.options.search_external
        if target.isdigit() and int(target) in self.bundles \
                and self.bundles[int(target)].search_external == search:
            return self.bundles[int(target)]
        jira, pr, candidates = core.resolve_target(target, s.repo, s.jira_base, s.token)
        deps = None
        if pr is not None and pr.get("state") == "OPEN":
            deps = core.collect_dependencies(pr, jira, s.repo, s.token, s.repo_path,
                                             jira_base=s.jira_base, search_external=search)
        report = core.analyse(jira, pr, s.repo, s.jira_base, s.repo_path, s.stale_days,
                              bool(s.repo_path), deps)
        if len(candidates) > 1 and pr is not None:
            others = ", ".join(f"#{c['number']} ({c['state'].lower()})"
                               for c in candidates if c["number"] != pr["number"])
            report.comments += f"; other PRs mention this JIRA: {others}"
        bundle = Bundle(target, pr, jira, deps, report, search)
        if pr is not None:
            self.bundles[pr["number"]] = bundle
        return bundle

    @work(thread=True, exclusive=True, group="bundle")
    def analyse(self, target: str) -> None:
        self.status_from_thread("analysis", f"analysing {target} (CI logs, diffs and JIRA - "
                                            f"this can take a minute) ...")
        try:
            bundle = self.bundle_for(target)
        except (Exception, SystemExit) as exc:
            self.fail_from_thread("analysis", f"analysing {target}", exc)
            return
        self.call_from_thread(self.show_analysis, bundle)

    def show_analysis(self, bundle: Bundle) -> None:
        self.current = bundle
        width = max(70, self.query_one("#analysis-scroll").size.width - 2)
        self.query_one("#analysis-text", Static).update(
            text(core.render_report([bundle.report], width)))
        label = f"#{bundle.pr['number']}" if bundle.pr else bundle.target
        self.set_status("analysis", f"{label}: d plans its dependencies, o opens the PR, "
                                    f"j the JIRA, r analyses it again from scratch")

    # ----- dependencies ----------------------------------------------------- #
    def action_plan(self) -> None:
        target = self.selected_target()
        if target:
            self.show_tab("deps")
            self.make_plan([target])

    def action_plan_all(self) -> None:
        if not self.rows:
            self.notify("the PR list is not loaded yet", severity="warning")
            return
        self.show_tab("deps")
        self.make_plan([str(r.number) for r in self.rows], graph=True)

    @work(thread=True, exclusive=True, group="bundle")
    def make_plan(self, targets: list[str], graph: bool = False) -> None:
        worker = get_current_worker()
        s, o = self.settings, self.options
        items: list[PlanItem] = []
        notes: list[str] = []
        bundles: list[Bundle] = []
        for index, target in enumerate(targets, 1):
            if worker.is_cancelled:
                return
            self.status_from_thread("deps", f"planning {target} ({index}/{len(targets)}) ...")
            try:
                bundle = self.bundle_for(target)
            except (Exception, SystemExit) as exc:
                notes.append(f"{target}: {exc}")
                continue
            pr = bundle.pr
            if pr is None or pr.get("state") != "OPEN" or bundle.deps is None:
                notes.append(f"{target}: no open pull request, skipped")
                continue
            if all(b.pr["number"] != pr["number"] for b in bundles):
                bundles.append(bundle)
            if len(targets) == 1:
                self.call_from_thread(setattr, self, "current", bundle)
        # All together: a dependency seen from one PR is written on the other
        # PR too when it is yours, and each JIRA link is proposed only once.
        try:
            me = fd.viewer_login(s.token) or s.author
            plans = fd.plan_all([fd.Target(b.pr, b.jira, b.deps) for b in bundles],
                                s.repo, s.jira_base, s.token, o.include_weak, o.jira_only,
                                o.pr_only, me)
        except (Exception, SystemExit) as exc:
            notes.append(f"planning failed: {exc}")
            plans = []
        for bundle, (number, changes, found) in zip(bundles, plans):
            label = bundle.jira.key if bundle.jira and bundle.jira.found else "no JIRA"
            notes.append(f"#{number} {label}: {len(changes)} change(s) proposed")
            notes += [f"  note: {n}" for n in found + fd.prose_notes(bundle.pr, bundle.deps)]
            notes += [f"  {n}" for n in fd.unexplained_lines(bundle.deps.get("unexplained", []),
                                                             bundle.jira)]
            for change in changes:
                # A description of another PR: that is the one to fetch again.
                touched = change.target[1:] if change.target.startswith("#") else ""
                items.append(PlanItem(change, int(touched) if touched.isdigit() else number))
        if graph:
            notes.insert(0, self.write_graph(bundles))
        self.call_from_thread(self.fill_plan, items, notes, targets, graph)

    def write_graph(self, bundles: list[Bundle]) -> str:
        """Draw the open PRs and the dependencies just found; returns a note."""
        s = self.settings
        prs = [{"number": r.number, "title": r.title, "status": r.status, "url": r.url,
                "jira": jira_key_in(r.title)} for r in self.rows]
        try:  # a bug here must not take the plan down with it
            graph = pr_graph.build_graph(prs, {b.pr["number"]: b.deps for b in bundles})
            svg = pr_graph.render_svg(graph, f"Open PRs of {s.author} into {s.repo}:{s.base}",
                                      datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
            path = pr_graph.write_svg(pr_graph.DEFAULT_GRAPH_FILE, svg)
        except Exception as exc:
            return f"graph not written: {exc}"
        return (f"graph written to {path}: {len(graph.edges)} dependency(ies), "
                f"{len(graph.overlaps)} overlap(s)")

    def fill_plan(self, items: list[PlanItem], notes: list[str], targets: list[str],
                  graph: bool) -> None:
        self.plan, self.plan_notes, self.plan_scope = items, notes, targets
        self.plan_graph = graph
        self.refresh_plan_table()
        self.query_one("#plan-notes", Static).update(text("\n".join(notes) or "no notes"))
        scope = f"#{targets[0]}" if len(targets) == 1 else f"{len(targets)} PRs"
        self.set_status("deps", f"{len(items)} change(s) proposed for {scope} - enter applies "
                                f"the highlighted one after asking, l proposes a link by hand")

    def refresh_plan_table(self, keep: int | None = None) -> None:
        table = self.query_one("#plan-table", DataTable)
        row = table.cursor_row if keep is None else keep
        table.clear()
        for index, item in enumerate(self.plan, 1):
            table.add_row(text(index), text(item.state, STATE_STYLE.get(item.state, "")),
                          text(item.change.kind), text(item.change.headline))
        if self.plan:
            table.move_cursor(row=min(row or 0, len(self.plan) - 1))
        self.show_plan_detail()

    @on(DataTable.RowHighlighted, "#plan-table")
    def show_plan_detail(self) -> None:
        item = self.selected_item()
        detail = self.query_one("#plan-detail", Static)
        if item is None:
            detail.update(text("nothing proposed" if self.plan_scope else ""))
            return
        index = self.plan.index(item) + 1
        body = fd.change_text(item.change, index, len(self.plan))
        if item.state != "pending":
            body += f"\n\n{item.state}: {item.message}"
        detail.update(text(body))

    @on(DataTable.RowSelected, "#plan-table")
    def apply_selected(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        if item.state == "applied":
            self.notify("already applied", severity="warning")
            return
        change = item.change
        if change.needs == "jira" and not fd.jira_token():
            self.notify("JIRA_TOKEN is not set: create a Personal Access Token at "
                        f"{self.settings.jira_base}/secure/ViewProfile.jspa and export it "
                        "before starting pr_manager", severity="error", timeout=12)
            return
        if change.needs == "github" and not self.settings.token:
            self.notify("no GitHub token: set GITHUB_TOKEN or run 'gh auth login'",
                        severity="error", timeout=12)
            return
        where = "JIRA" if change.needs == "jira" else "GitHub (the PR description)"
        body = fd.change_text(change, self.plan.index(item) + 1, len(self.plan))

        def answered(yes: bool | None) -> None:
            if yes:
                self.apply(item)
            else:
                item.state, item.message = "skipped", "not applied - you said no"
                self.refresh_plan_table()
        self.push_screen(ConfirmApply(body, where), answered)

    @work(thread=True, group="apply")
    def apply(self, item: PlanItem) -> None:
        self.status_from_thread("deps", f"writing: {item.change.headline} ...")
        try:
            ok, message = item.change.run()
        except (Exception, SystemExit) as exc:
            ok, message = False, str(exc)
        self.call_from_thread(self.applied, item, ok, message)

    def applied(self, item: PlanItem, ok: bool, message: str) -> None:
        item.state, item.message = ("applied" if ok else "failed"), message
        # What JIRA or the PR says now differs from what was fetched.
        if item.number is not None:
            self.bundles.pop(item.number, None)
        self.refresh_plan_table()
        self.notify(message, severity="information" if ok else "error", timeout=8)
        self.set_status("deps", f"{'done' if ok else 'FAILED'}: {message}")

    def action_add_link(self) -> None:
        blocked = ""
        if self.current and self.current.jira and self.current.jira.found:
            blocked = self.current.jira.key
        elif (row := self.selected_row()) is not None:
            blocked = jira_key_in(row.title)

        def chosen(pair: str | None) -> None:
            if not pair:
                return
            try:
                change = fd.link_change(self.settings.jira_base, pair, "proposed by hand in pr_manager")
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return
            self.plan.append(PlanItem(change, None))
            self.show_tab("deps")
            self.refresh_plan_table(keep=len(self.plan) - 1)
            self.set_status("deps", "link added to the plan - press enter on it to apply it")
        self.push_screen(AskLink(blocked), chosen)

    @on(Checkbox.Changed)
    def option_changed(self, event: Checkbox.Changed) -> None:
        name = {"opt-search": "search_external", "opt-weak": "include_weak",
                "opt-jira": "jira_only", "opt-pr": "pr_only"}[event.checkbox.id]
        setattr(self.options, name, event.value)
        if self.plan_scope:
            self.set_status("deps", "options changed - press d (or D) to plan again")

    # ----- branches --------------------------------------------------------- #
    @work(thread=True, exclusive=True, group="branches")
    def load_branches(self, fetch: bool) -> None:
        # Without a Hadoop clone list_stale_branches.py would run in this repository instead.
        if not self.settings.repo_path:
            self.fail_from_thread("branches", "listing the branches", RuntimeError(
                f"no Hadoop clone at {core.DEFAULT_REPO_PATH}; "
                "pass --repo-path or set HADOOP_REPO_PATH"))
            return
        self.status_from_thread("branches", "fetching and comparing branches ..." if fetch
                                else "comparing branches ...")
        command = [sys.executable, os.path.join(HERE, "list_stale_branches.py"),
                   "--format", "json", "--all", "--repo-path", self.settings.repo_path]
        if not fetch:
            command.append("--no-fetch")
        try:
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=900, cwd=HERE)
            if done.returncode != 0:
                raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1])
            rows = json.loads(done.stdout or "[]")
        except (Exception, SystemExit) as exc:
            self.fail_from_thread("branches", "listing the branches", exc)
            return
        self.call_from_thread(self.fill_branches, rows)

    def fill_branches(self, rows: list[dict[str, Any]]) -> None:
        self.branches = rows
        table = self.query_one("#branch-table", DataTable)
        table.clear()
        for row in rows:
            age = row.get("age_days")
            table.add_row(text(row["branch"]),
                          text(row["status"], BRANCH_STYLE.get(row["status"], "")),
                          text(row.get("last_commit")), text("" if age is None else f"{age}d"),
                          text(row.get("ahead")), text(row.get("behind")))
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in BRANCH_STYLE}
        self.set_status("branches", ", ".join(f"{n} {s}" for s, n in counts.items())
                        + " - report only, nothing here deletes a branch")
        self.show_branch_detail()

    @on(DataTable.RowHighlighted, "#branch-table")
    def show_branch_detail(self) -> None:
        table = self.query_one("#branch-table", DataTable)
        detail = self.query_one("#branch-detail", Static)
        if not self.branches or table.cursor_row is None or table.cursor_row >= len(self.branches):
            detail.update("")
            return
        row = self.branches[table.cursor_row]
        detail.update(text(f"{row['branch']} - {row['status']}\n{row.get('comments') or ''}"))

    def action_fetch_branches(self) -> None:
        if self.tab == "branches":
            self.load_branches(True)

    @on(TabbedContent.TabActivated)
    def tab_shown(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "branches" and not self.branches:
            self.load_branches(False)

    # ----- everywhere ------------------------------------------------------- #
    def action_reload(self) -> None:
        tab = self.tab
        if tab == "branches":
            self.load_branches(False)
            return
        # Fetch everything again: CI may have run, JIRA may have changed.
        self.bundles.clear()
        for name in CORE_CACHES:
            cache = getattr(core, name, None)
            if isinstance(cache, dict):
                cache.clear()
        if tab == "prs":
            self.load_prs()
        elif tab == "analysis" and self.current and self.current.pr:
            self.analyse(str(self.current.pr["number"]))
        elif tab == "deps" and self.plan_scope:
            self.make_plan(self.plan_scope, self.plan_graph)
        self.show_credentials()

    def action_open_pr(self) -> None:
        url = ""
        if self.tab in ("analysis", "deps") and self.current and self.current.pr:
            url = self.current.pr.get("url") or ""
        elif (row := self.selected_row()) is not None:
            url = row.url
        if url:
            webbrowser.open(url)

    def action_open_jira(self) -> None:
        key = ""
        if self.tab in ("analysis", "deps") and self.current and self.current.jira:
            key = self.current.jira.key
        elif (row := self.selected_row()) is not None:
            key = jira_key_in(row.title)
        if key:
            webbrowser.open(f"{self.settings.jira_base.rstrip('/')}/browse/{key}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--author", default=lup.DEFAULT_FORK_OWNER,
                        help=f"whose open PRs to list (default: {lup.DEFAULT_FORK_OWNER})")
    parser.add_argument("--repo", default=core.DEFAULT_REPO)
    parser.add_argument("--base", default=lup.DEFAULT_BASE_BRANCH,
                        help="target branch upstream, '*' for any (default: trunk)")
    parser.add_argument("--jira-base", default=core.DEFAULT_JIRA)
    parser.add_argument("--repo-path", default=None,
                        help=f"the Hadoop clone (default: {core.DEFAULT_REPO_PATH})")
    parser.add_argument("--stale-days", type=int, default=14)
    parser.add_argument("--no-ci-search", action="store_true",
                        help="start with the search of others' PRs and JIRA switched off")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_path = args.repo_path or (
        core.DEFAULT_REPO_PATH if os.path.isdir(core.DEFAULT_REPO_PATH) else None
    )
    settings = Settings(repo=args.repo, author=args.author, base=args.base,
                        jira_base=args.jira_base, repo_path=repo_path,
                        stale_days=args.stale_days, token=lup.resolve_token(args.token))
    PRManager(settings, PlanOptions(search_external=not args.no_ci_search)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
