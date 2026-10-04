#!/usr/bin/env python3
"""List every open pull request into apache/hadoop per component, yours and
everybody else's, and recommend the ones where your review helps most.

Components are the Maven modules of the Hadoop clone (the nearest directory
with a pom.xml above each changed file, read from its trunk with git ls-tree);
a PR is filed under the module with most of its changed lines, and lists the
others it touches. Without a clone the module is the path before "src/".

*Read-only, always.* The script only runs GraphQL queries against GitHub and
git read commands against the clone; it has no write mode.

Priority
--------
Your own PRs and drafts are never recommended. Every other PR gets points,
all shown with the reason (--explain):

    size (changed lines)     <= 20  +20   <= 100 +15   <= 300 +10
                             <= 1000 +4   larger 0
    more than 20 files                         -5
    waiting: days since opened                 +1 per 3 days, at most 20
    untouched for 180 days (likely abandoned)  -15
    reviews: none yet                          +20
             comments only                     +12
             changes requested, new commits    +10   (author answered)
             changes requested, nothing new     0    (author's turn)
             approved                           +5   (second +1 / merge)
    you reviewed it and nothing new since      -20
    your review was requested                  +10
    a committer (write access) commented on it,
      reviewed it or was asked to review it    +10
    Yetus on the latest commit: +1             +15
                                -1              +5
             older than the latest commit, or none  +3
    merge conflict or Yetus "rebase required"  -10
    topic: security +10, bug +8, flaky/test fix +7, build +5,
           feature +4, docs +4, dependency bump +3
    changes main code and its tests            +3
    area: its main component is in your area   +10
          only another component it touches     +5
          (--focus replaces both: +10 when it matches)

Your area is the components where you have at least 2 changes: commits of
yours in the clone's trunk in the last --area-days (default 365) plus your
open PRs, each counted once per component. Only main code counts: files
under src/test/, pom.xml files and root files are left out, so a test-only
or build-only change adds nothing.

A committer is somebody who merged a PR into the repository in the last
--committer-days (default 365), or whom GitHub shows as OWNER, MEMBER or
COLLABORATOR on a comment or review, or a --committer LOGIN. The merges are
the reliable part: GitHub shows most Hadoop committers as CONTRIBUTOR,
because their membership of the apache organization is private. The PR's
author and you do not count.

The top N (default 10) is taken in score order with at most
--max-per-component PRs (default 3) from one component, so one busy module
does not fill the whole list.

Examples
--------
    python review_queue.py                     # per-component list + top 10
    python review_queue.py --view recommend --explain
    python review_queue.py --view components --component hadoop-hdfs-rbf
    python review_queue.py --view components --others --format markdown
    python review_queue.py --focus YARN --focus hadoop-common --top 15
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shutil
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from typing import Any

import analyze_pr as core
from list_upstream_prs import (DEFAULT_BOTS, days_since, graphql, parse_yetus_comment,
                               resolve_token, summarise_reviews)

ROOT = "(root)"
NON_HUMAN = {b.lower() for b in DEFAULT_BOTS} | {"dependabot", "github-actions"}

POINTS = {
    "size": ((20, 20), (100, 15), (300, 10), (1000, 4)),
    "many_files": -5,
    "waiting_days_per_point": 3,
    "waiting_max": 20,
    "abandoned_days": 180,
    "abandoned": -15,
    "no_review": 20,
    "commented": 12,
    "changes_answered": 10,
    "changes_pending": 0,
    "approved": 5,
    "seen_by_you": -20,
    "requested_from_you": 10,
    "yetus_pass": 15,
    "yetus_fail": 5,
    "yetus_unknown": 3,
    "conflict": -10,
    "topic": {"security": 10, "bug": 8, "test": 7, "build": 5, "feature": 4, "docs": 4,
              "dependency": 3},
    "with_tests": 3,
    "your_area": 10,
    "your_area_secondary": 5,
    "area_min_changes": 2,
    "committer_engaged": 10,
}

QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt baseRefName
        additions deletions changedFiles mergeable reviewDecision
        author { login }
        labels(first: 20) { nodes { name } }
        files(first: 100) { nodes { path additions deletions } }
        latestReviews(first: 20) {
          nodes { author { login } authorAssociation state submittedAt }
        }
        reviewRequests(first: 20) {
          nodes { requestedReviewer { __typename ... on User { login } } }
        }
        comments(last: 10) { nodes { author { login } createdAt body } }
        participants: comments(last: 100) { nodes { author { login } authorAssociation } }
        commits(last: 1) { nodes { commit { committedDate } } }
      }
    }
  }
}
"""

SECURITY_RE = re.compile(r"\bcve-\d|secur|vulnerab", re.I)
DEPENDENCY_RE = re.compile(r"^bump\b|\b(upgrade|bump)\b", re.I)
TEST_RE = re.compile(r"flak|deflake|intermittent|\btest", re.I)
BUG_RE = re.compile(r"\b(fix\w*|npe|nullpointer\w*|\w*exception|leak\w*|race|deadlock|incorrect\w*|"
                    r"wrong\w*|fail\w*|bug|broken|regression|errors?|crash\w*|hang\w*|never|"
                    r"missing|caus\w+|invalid|overflow|mismatch\w*|lost|loses?)\b", re.I)
DOC_RE = re.compile(r"\.(md|apt\.vm|vm|html|txt)$|/site/")


# --------------------------------------------------------------------------- #
# GitHub and the clone (read-only)
# --------------------------------------------------------------------------- #
def fetch_open_prs(repo: str, base: str, token: str | None) -> list[dict[str, Any]]:
    query = f"repo:{repo} is:pr is:open"
    if base != "*":
        query += f" base:{base}"
    prs, after = [], None
    while True:
        data = graphql(QUERY, {"q": query, "after": after}, token)["search"]
        prs += [n for n in data["nodes"] or [] if n]
        print(f"\rfetched {len(prs)} open PRs", end="", file=sys.stderr, flush=True)
        if not data["pageInfo"]["hasNextPage"]:
            print(file=sys.stderr)
            return prs
        after = data["pageInfo"]["endCursor"]


MERGERS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: 100, after: $after,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { updatedAt mergedAt mergedBy { login } }
    }
  }
}
"""
WRITE_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def fetch_mergers(repo: str, days: int, token: str | None) -> list[str]:
    """Logins that merged a PR in the last `days` days: they have write access.
    Cached on disk for the day."""
    if days <= 0:
        return []
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def compute() -> list[str]:
        owner, name = repo.split("/", 1)
        found: set[str] = set()
        after = None
        while True:
            data = graphql(MERGERS_QUERY, {"owner": owner, "name": name, "after": after},
                           token)["repository"]["pullRequests"]
            for node in data["nodes"]:
                login = (node.get("mergedBy") or {}).get("login")
                if login and (node.get("mergedAt") or "") >= since:
                    found.add(login)
            oldest = min((n.get("updatedAt") or "" for n in data["nodes"]), default="")
            if not data["pageInfo"]["hasNextPage"] or oldest < since:
                return sorted(found)
            after = data["pageInfo"]["endCursor"]

    today = datetime.date.today().isoformat()
    return core.disk_cached(f"review_queue_mergers_{repo}_{days}_{today}", compute)


def associated_committers(prs: list[dict[str, Any]]) -> set[str]:
    """Logins GitHub shows with write access on a comment or review of these PRs."""
    found = set()
    for pr in prs:
        for key in ("latestReviews", "participants"):
            for node in (pr.get(key) or {}).get("nodes") or []:
                login = (node.get("author") or {}).get("login")
                if login and node.get("authorAssociation") in WRITE_ASSOCIATIONS:
                    found.add(login)
    return found


def committers_engaged(pr: dict[str, Any], user: str, committers: set[str]) -> list[str]:
    """Committers other than the author and you who commented on, reviewed or
    were asked to review the PR."""
    skip = {user.lower(), ((pr.get("author") or {}).get("login") or "").lower()}
    lower = {c.lower() for c in committers}
    logins = [(n.get("author") or {}).get("login") or ""
              for key in ("latestReviews", "participants")
              for n in (pr.get(key) or {}).get("nodes") or []]
    logins += [(n.get("requestedReviewer") or {}).get("login") or ""
               for n in (pr.get("reviewRequests") or {}).get("nodes") or []]
    return list(dict.fromkeys(l for l in logins
                              if l and l.lower() in lower and l.lower() not in skip))


def maven_modules(repo_path: str | None) -> set[str]:
    """Directories with a pom.xml in the trunk of the clone ('' for the root)."""
    ref = core.resolve_base_ref(repo_path)
    if not ref:
        return set()
    code, out = core.git_run(repo_path, "ls-tree", "-r", "--name-only", ref)
    if code != 0:
        return set()
    return {os.path.dirname(p).replace("\\", "/") for p in out.splitlines()
            if p == "pom.xml" or p.endswith("/pom.xml")}


def module_of(path: str, modules: set[str]) -> str:
    parts = path.split("/")[:-1]
    if modules:
        for i in range(len(parts), 0, -1):
            if "/".join(parts[:i]) in modules:
                return "/".join(parts[:i])
        return ""
    if "src" in parts:
        return "/".join(parts[:parts.index("src")])
    return "/".join(parts[:2])


def component_name(module: str) -> str:
    return module.rsplit("/", 1)[-1] if module else ROOT


def project_of(title: str, modules: list[str]) -> str:
    match = core.JIRA_IN_TEXT_RE.search(title)
    if match:
        return match.group(1).upper()
    for module in modules:
        project = core.JIRA_PROJECT_OF_TREE.get(module.split("/", 1)[0])
        if project:
            return project
    return "HADOOP"


def main_components(paths: list[str], modules: set[str]) -> set[str]:
    """The components whose code the paths change: tests, pom.xml files and
    files outside every module (the root) are left out."""
    found = {component_name(module_of(p, modules)) for p in paths
             if p and "/src/test/" not in p and os.path.basename(p) != "pom.xml"}
    return found - {ROOT}


def your_history(repo_path: str | None, modules: set[str], days: int) -> collections.Counter:
    """Per component, how many of your commits in the clone's trunk in the last
    `days` days changed its main code."""
    counts: collections.Counter = collections.Counter()
    ref = core.resolve_base_ref(repo_path)
    if not ref or days <= 0:
        return counts
    code, email = core.git_run(repo_path, "config", "user.email")
    if code != 0 or not email:
        return counts
    code, out = core.git_run(repo_path, "log", ref, f"--author={email}", f"--since={days}.days",
                             "--name-only", "--format=tformat:@@")
    if code != 0:
        return counts
    for commit in out.split("@@"):
        counts.update(main_components(commit.splitlines(), modules))
    return counts


def your_area(history: collections.Counter, own_prs: list[dict[str, Any]],
              modules: set[str]) -> set[str]:
    """Components with at least POINTS['area_min_changes'] changes of yours:
    commits from `history` plus open PRs, main code only."""
    counts = collections.Counter(history)
    for pr in own_prs:
        counts.update(main_components([f["path"] for f in (pr.get("files") or {})
                                       .get("nodes") or []], modules))
    return {c for c, n in counts.items() if n >= POINTS["area_min_changes"]}


# --------------------------------------------------------------------------- #
# Classification and scoring
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    number: int
    title: str
    url: str
    author: str
    mine: bool
    draft: bool
    base: str
    project: str
    component: str
    components: list[str]
    topic: str
    files: int
    additions: int
    deletions: int
    age_days: int
    idle_days: int
    reviews: str
    yetus: str
    conflict: bool
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    committers: list[str] = field(default_factory=list)

    @property
    def lines(self) -> int:
        return self.additions + self.deletions


def topic_of(pr: dict[str, Any], paths: list[str]) -> str:
    title = pr.get("title") or ""
    labels = {(l.get("name") or "").lower() for l in (pr.get("labels") or {}).get("nodes") or []}
    login = ((pr.get("author") or {}).get("login") or "").lower()
    main = any("/src/main/" in p for p in paths)
    if SECURITY_RE.search(title):
        return "security"
    if login == "dependabot" or "dependencies" in labels or DEPENDENCY_RE.search(title):
        return "dependency"
    if paths and all(DOC_RE.search(p) for p in paths):
        return "docs"
    if paths and all("/src/test/" in p for p in paths) or TEST_RE.search(title) and not main:
        return "test"
    if BUG_RE.search(title):
        return "bug"
    if paths and all(os.path.basename(p) == "pom.xml" or p.startswith(("dev-support/", ".github/"))
                     or "Dockerfile" in p for p in paths):
        return "build"
    return "feature"


def review_state(pr: dict[str, Any]) -> str:
    if pr.get("isDraft"):
        return "draft"
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "changes"
    approvers, requesters, commenters = summarise_reviews(pr)
    humans = [r for r in approvers + requesters + commenters if r.lower() not in NON_HUMAN]
    if approvers:
        return "approved"
    if requesters:
        return "changes"
    return "commented" if humans else "none"


def last_commit(pr: dict[str, Any]) -> str:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    return (nodes[0].get("commit") or {}).get("committedDate") or "" if nodes else ""


def yetus_state(pr: dict[str, Any]) -> tuple[str, bool]:
    """('+1' | '-1' | 'stale' | 'none', needs rebase) for the latest Yetus report."""
    report = parse_yetus_comment(pr, DEFAULT_BOTS)
    if report is None or not report.overall:
        return "none", False
    if (report.posted_at or "") < last_commit(pr):
        return "stale", report.needs_rebase
    return report.overall, report.needs_rebase


def classify(pr: dict[str, Any], user: str, modules: set[str]) -> Entry:
    files = (pr.get("files") or {}).get("nodes") or []
    weight: dict[str, int] = {}
    for f in files:
        module = module_of(f["path"], modules)
        weight[module] = weight.get(module, 0) + f.get("additions", 0) + f.get("deletions", 0) + 1
    ordered = sorted(weight, key=lambda m: (-weight[m], m)) or [""]
    yetus, rebase = yetus_state(pr)
    author = (pr.get("author") or {}).get("login") or "ghost"
    return Entry(
        number=pr["number"], title=pr.get("title") or "", url=pr.get("url") or "",
        author=author, mine=author.lower() == user.lower(), draft=bool(pr.get("isDraft")),
        base=pr.get("baseRefName") or "", project=project_of(pr.get("title") or "", ordered),
        component=component_name(ordered[0]),
        components=list(dict.fromkeys(component_name(m) for m in ordered)),
        topic=topic_of(pr, [f["path"] for f in files]),
        files=pr.get("changedFiles") or len(files),
        additions=pr.get("additions") or 0, deletions=pr.get("deletions") or 0,
        age_days=days_since(pr.get("createdAt")) or 0,
        idle_days=days_since(pr.get("updatedAt")) or 0,
        reviews=review_state(pr), yetus=yetus,
        conflict=pr.get("mergeable") == "CONFLICTING" or rebase,
    )


def score(entry: Entry, pr: dict[str, Any], user: str, focus: set[str],
          your_components: set[str], committers: set[str] = frozenset()) -> None:
    reasons: list[tuple[int, str]] = []

    def add(points: int, why: str) -> None:
        if points:
            reasons.append((points, why))

    for limit, points in POINTS["size"]:
        if entry.lines <= limit:
            add(points, f"small: {entry.lines} changed lines (<= {limit})")
            break
    if entry.files > 20:
        add(POINTS["many_files"], f"{entry.files} files")
    add(min(POINTS["waiting_max"], entry.age_days // POINTS["waiting_days_per_point"]),
        f"open for {entry.age_days} days")
    if entry.idle_days > POINTS["abandoned_days"]:
        add(POINTS["abandoned"], f"untouched for {entry.idle_days} days, likely abandoned")

    commit = last_commit(pr)
    if entry.reviews == "none":
        add(POINTS["no_review"], "nobody has reviewed it")
    elif entry.reviews == "commented":
        add(POINTS["commented"], "only review comments so far")
    elif entry.reviews == "approved":
        add(POINTS["approved"], "approved: a second +1 or a merge is what it needs")
    elif entry.reviews == "changes":
        asked = max((r.get("submittedAt") or "" for r in (pr.get("latestReviews") or {})
                     .get("nodes") or [] if r.get("state") == "CHANGES_REQUESTED"), default="")
        if commit > asked:
            add(POINTS["changes_answered"], "changes requested, new commits since")
        else:
            reasons.append((POINTS["changes_pending"], "changes requested, waiting for the author"))

    yours = [r.get("submittedAt") or "" for r in (pr.get("latestReviews") or {}).get("nodes") or []
             if ((r.get("author") or {}).get("login") or "").lower() == user.lower()]
    if yours and commit <= max(yours):
        add(POINTS["seen_by_you"], "you reviewed it and nothing changed since")
    requested = [((n.get("requestedReviewer") or {}).get("login") or "").lower()
                 for n in (pr.get("reviewRequests") or {}).get("nodes") or []]
    if user.lower() in requested:
        add(POINTS["requested_from_you"], "your review was requested")

    if entry.yetus == "+1":
        add(POINTS["yetus_pass"], "Yetus +1 on the latest commit")
    elif entry.yetus == "-1":
        add(POINTS["yetus_fail"], "Yetus -1 on the latest commit")
    else:
        add(POINTS["yetus_unknown"], "no Yetus report on the latest commit")
    if entry.conflict:
        add(POINTS["conflict"], "merge conflict / rebase required")

    add(POINTS["topic"].get(entry.topic, 4), f"topic: {entry.topic}")
    paths = [f["path"] for f in (pr.get("files") or {}).get("nodes") or []]
    if any("/src/main/" in p for p in paths) and any("/src/test/" in p for p in paths):
        add(POINTS["with_tests"], "changes main code together with its tests")

    engaged = entry.committers = committers_engaged(pr, user, committers)
    if engaged:
        add(POINTS["committer_engaged"], f"committer involved: {', '.join(engaged[:3])}"
            + (f" (+{len(engaged) - 3})" if len(engaged) > 3 else ""))

    touched = set(entry.components)
    if focus:
        matched = (touched | {entry.project}) & focus
        if matched:
            add(POINTS["your_area"], f"in your focus: {', '.join(sorted(matched))}")
    elif entry.component in your_components:
        add(POINTS["your_area"], f"your area: {entry.component}")
    elif touched & your_components:
        add(POINTS["your_area_secondary"],
            f"also touches your area: {', '.join(sorted(touched & your_components))}")

    entry.score = sum(p for p, _ in reasons)
    entry.reasons = [f"{p:+d} {why}" for p, why in reasons]


def recommend(entries: list[Entry], top: int, per_component: int,
              include_drafts: bool) -> list[Entry]:
    candidates = sorted((e for e in entries if not e.mine and (include_drafts or not e.draft)),
                        key=lambda e: (-e.score, e.number))
    picked: list[Entry] = []
    taken: dict[str, int] = {}
    for entry in candidates:
        if taken.get(entry.component, 0) >= per_component:
            continue
        taken[entry.component] = taken.get(entry.component, 0) + 1
        picked.append(entry)
        if len(picked) == top:
            break
    return picked


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def grouped(entries: list[Entry]) -> list[tuple[str, list[Entry]]]:
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(entry.component, []).append(entry)
    return [(name, sorted(groups[name], key=lambda e: -e.number))
            for name in sorted(groups, key=lambda g: (-len(groups[g]), g))]


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(1, width - 3)] + "..."


def render_components(entries: list[Entry], width: int) -> str:
    mine = sum(e.mine for e in entries)
    groups = grouped(entries)
    out = [f"Open PRs: {len(entries)} (yours {mine}, others {len(entries) - mine}) "
           f"in {len(groups)} components", ""]
    head = f"  {'PR':>6}  {'topic':<10} {'author':<16} {'files':>5} {'lines':>6} {'age':>4} " \
           f"{'reviews':<9} {'yetus':<5} "
    for name, items in groups:
        own = sum(e.mine for e in items)
        out.append(f"== {name}: {len(items)} open, {own} yours")
        out.append(head + "title")
        for e in items:
            row = f"{'*' if e.mine else ' '} {e.number:>6}  {e.topic:<10} {short(e.author, 16):<16} " \
                  f"{e.files:>5} {e.lines:>6} {e.age_days:>3}d " \
                  f"{('draft' if e.draft else e.reviews):<9} {e.yetus:<5} "
            out.append(row + short(e.title, max(20, width - len(row))))
        out.append("")
    out.append("* = yours; lines = additions + deletions; age = days since opened; "
               "yetus 'stale' = older than the latest commit")
    return "\n".join(out)


def render_recommendations(picked: list[Entry], explain: bool, width: int) -> str:
    out = [f"Top {len(picked)} PRs to review", ""]
    for rank, e in enumerate(picked, 1):
        out.append(f"{rank:>2}. [{e.score:>3}] #{e.number} {short(e.title, width - 16)}")
        out.append(f"    {e.component} | {e.project} | {e.topic} | {e.author} | {e.files} files "
                   f"+{e.additions}/-{e.deletions} | {e.age_days}d old | reviews: {e.reviews} | "
                   f"yetus: {e.yetus}{' | CONFLICT' if e.conflict else ''}"
                   + (f" | committers: {', '.join(e.committers)}" if e.committers else ""))
        out.append(f"    {e.url}")
        if explain:
            out.extend(textwrap.wrap("; ".join(e.reasons), width - 4,
                                     initial_indent="    ", subsequent_indent="    "))
        out.append("")
    return "\n".join(out).rstrip()


def md(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(entries: list[Entry] | None, picked: list[Entry] | None,
                    explain: bool) -> str:
    out: list[str] = []
    if picked is not None:
        out += ["## Top PRs to review", "", "| # | Score | PR | Component | Topic | Author | "
                "Size | Age | Reviews | Yetus |", "|---|---|---|---|---|---|---|---|---|---|"]
        for rank, e in enumerate(picked, 1):
            out.append(f"| {rank} | {e.score} | [#{e.number}]({e.url}) {md(e.title)} | "
                       f"{e.component} | {e.topic} | {e.author} | {e.files}f "
                       f"+{e.additions}/-{e.deletions} | {e.age_days}d | {e.reviews} | {e.yetus} |")
        if explain:
            out += [""] + [f"- **#{e.number}** ({e.score}): {md('; '.join(e.reasons))}"
                           for e in picked]
        out.append("")
    if entries is not None:
        out += ["## Open PRs per component", ""]
        for name, items in grouped(entries):
            out += [f"### {name} ({len(items)} open, {sum(e.mine for e in items)} yours)", "",
                    "| PR | Mine | Topic | Author | Files | Lines | Age | Reviews | Yetus |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for e in items:
                out.append(f"| [#{e.number}]({e.url}) {md(e.title)} | {'yes' if e.mine else ''} | "
                           f"{e.topic} | {e.author} | {e.files} | {e.lines} | {e.age_days}d | "
                           f"{'draft' if e.draft else e.reviews} | {e.yetus} |")
            out.append("")
    return "\n".join(out).rstrip()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=core.DEFAULT_REPO,
                        help=f"repository, owner/name (default: {core.DEFAULT_REPO})")
    parser.add_argument("--base", default="trunk",
                        help="target branch, '*' for any (default: trunk)")
    parser.add_argument("--user", default=core.DEFAULT_AUTHOR,
                        help=f"your GitHub login (default: {core.DEFAULT_AUTHOR})")
    parser.add_argument("--repo-path", default=core.DEFAULT_REPO_PATH,
                        help="Hadoop clone, for the modules and your history "
                             f"(default: {core.DEFAULT_REPO_PATH})")
    parser.add_argument("--view", choices=("all", "components", "recommend"), default="all",
                        help="what to print (default: all)")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--mine", action="store_true", help="list only your PRs")
    who.add_argument("--others", action="store_true", help="list only other people's PRs")
    parser.add_argument("--component", action="append", default=[], metavar="NAME",
                        help="only PRs touching this component or JIRA project (repeatable)")
    parser.add_argument("--top", type=int, default=10, help="PRs to recommend (default: 10)")
    parser.add_argument("--max-per-component", type=int, default=3,
                        help="recommendations from one component at most (default: 3)")
    parser.add_argument("--focus", action="append", default=[], metavar="NAME",
                        help="component or JIRA project you want to review; replaces the "
                             "area learned from your history (repeatable)")
    parser.add_argument("--area-days", type=int, default=365,
                        help="your commits of this many days make up your area, with your "
                             "open PRs (default: 365)")
    parser.add_argument("--committer", action="append", default=[], metavar="LOGIN",
                        help="GitHub login with write access, besides those found (repeatable)")
    parser.add_argument("--committer-days", type=int, default=365,
                        help="who merged a PR in this many days counts as a committer; "
                             "0 skips the lookup (default: 365)")
    parser.add_argument("--include-drafts", action="store_true",
                        help="recommend draft PRs as well")
    parser.add_argument("--explain", action="store_true",
                        help="print the points behind every recommendation")
    parser.add_argument("--format", choices=("table", "markdown", "json"), default="table",
                        help="output format (default: table)")
    parser.add_argument("--width", type=int, default=None,
                        help="table width in columns (default: terminal width)")
    parser.add_argument("--from-json", metavar="FILE",
                        help="read the PRs from a file (a JSON list of GraphQL nodes) "
                             "instead of GitHub; offline, so committers come only from "
                             "--committer and the associations in the file")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    token = None if args.from_json else resolve_token(args.token)
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            prs = json.load(f)
        mergers: list[str] = []
    else:
        prs = fetch_open_prs(args.repo, args.base, token)
        mergers = fetch_mergers(args.repo, args.committer_days, token)
    committers = set(mergers) | associated_committers(prs) | set(args.committer)

    repo_path = args.repo_path if os.path.isdir(args.repo_path or "") else None
    modules = maven_modules(repo_path)
    entries = [classify(pr, args.user, modules) for pr in prs]
    by_number = {pr["number"]: pr for pr in prs}

    your_components = your_area(your_history(repo_path, modules, args.area_days),
                                [by_number[e.number] for e in entries if e.mine], modules)
    focus = set(args.focus)
    for e in entries:
        score(e, by_number[e.number], args.user, focus, your_components, committers)

    if args.component:
        wanted = set(args.component)
        entries = [e for e in entries if wanted & (set(e.components) | {e.project})]
    picked = recommend(entries, args.top, args.max_per_component, args.include_drafts) \
        if args.view in ("all", "recommend") else None
    listed = entries
    if args.mine:
        listed = [e for e in entries if e.mine]
    elif args.others:
        listed = [e for e in entries if not e.mine]
    listed = listed if args.view in ("all", "components") else None

    if args.format == "json":
        payload: dict[str, Any] = {}
        if listed is not None:
            payload["components"] = {name: [asdict(e) for e in items]
                                     for name, items in grouped(listed)}
        if picked is not None:
            payload["recommendations"] = [asdict(e) for e in picked]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.format == "markdown":
        print(render_markdown(listed, picked, args.explain))
        return 0
    width = max(80, args.width or shutil.get_terminal_size((140, 24)).columns)
    if listed is not None:
        print(render_components(listed, width))
    if picked is not None:
        if listed is not None:
            print()
        print(render_recommendations(picked, args.explain, width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
