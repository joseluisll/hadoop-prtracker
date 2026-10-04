#!/usr/bin/env python3
"""List the stale branches of a personal fork (default: joseluisll/hadoop).

Rules requested by the user:

* a branch is STALE when it is not associated with an open pull request, and
  it has no relation to another branch that *is* associated with an open PR;
* whenever the relation is uncertain the branch is reported as CANDIDATE
  instead, so that nothing is thrown away by mistake;
* branches that are the head of an open PR are ACTIVE (hidden unless --all).

"Relation" is established by three independent signals:

1. shared history - the branch contains, or is contained in, one of the
   commits an open-PR branch adds on top of the base branch;
2. shared JIRA key - ``HADOOP-19987-retry`` relates to ``HADOOP-19987``;
3. an earlier pull request for the same branch (merged -> stale, closed
   without merging -> candidate, because the work may still be revived).

Branches inherited from the upstream project when the fork was created (same
name and same tip as in apache/hadoop) are not personal work; they are skipped
by default and can be shown with --include-mirrors.

History analysis is done locally with git, so the clone must have a remote
pointing at the fork (default ``origin``) and one at the upstream project
(default ``upstream``). Pull request data comes from the GitHub API, reusing
the authentication helpers of ``list_upstream_prs.py`` (kept side by side).

Examples
--------
    python list_stale_branches.py
    python list_stale_branches.py --all --format markdown
    python list_stale_branches.py --no-fetch --format json | jq '.[].status'
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from analyze_pr import DEFAULT_REPO_PATH, JIRA_IN_TEXT_RE, has_commit, is_ancestor
from list_upstream_prs import graphql, resolve_token

DEFAULT_FORK = "joseluisll/hadoop"
DEFAULT_UPSTREAM = "apache/hadoop"
DEFAULT_FORK_REMOTE = "origin"
DEFAULT_UPSTREAM_REMOTE = "upstream"
DEFAULT_BASE_REF = "upstream/trunk"


PR_SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state isDraft
        headRefName headRefOid
        baseRefName
        mergedAt closedAt updatedAt
        headRepositoryOwner { login }
        baseRepository { nameWithOwner }
      }
    }
  }
}
"""

FORK_PR_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN], first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url state isDraft
        headRefName headRefOid
        baseRefName
        mergedAt closedAt updatedAt
        headRepositoryOwner { login }
        baseRepository { nameWithOwner }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
class Git:
    def __init__(self, cwd: str | None = None) -> None:
        if not shutil.which("git"):
            raise SystemExit("git is not on PATH.")
        self.cwd = cwd or os.getcwd()
        # Run from inside prtracker's own clone, the current directory is not the Hadoop one.
        own_clone = os.path.dirname(os.path.abspath(__file__))
        toplevel = self.run("rev-parse", "--show-toplevel", check=False).strip()
        if (self.run("rev-parse", "--is-inside-work-tree", check=False).strip() != "true"
                or (not cwd and toplevel and os.path.samefile(toplevel, own_clone))):
            if cwd or not os.path.isdir(DEFAULT_REPO_PATH):
                raise SystemExit(f"{self.cwd} is not a git clone; pass --repo-path.")
            self.cwd = DEFAULT_REPO_PATH
        self.root = self.run("rev-parse", "--show-toplevel").strip() or self.cwd

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            raise SystemExit(
                f"git {' '.join(args)} failed ({result.returncode}): "
                f"{result.stderr.strip()[:400]}"
            )
        return result.stdout

    def lines(self, *args: str, check: bool = True) -> list[str]:
        return [line for line in self.run(*args, check=check).splitlines() if line.strip()]

    def count(self, rev_range: str) -> int:
        out = self.run("rev-list", "--count", rev_range, check=False).strip()
        return int(out) if out.isdigit() else 0


@dataclass
class Branch:
    name: str
    ref: str
    sha: str
    committed: str
    subject: str
    author: str
    ahead: int = 0  # commits not in the base branch
    behind: int = 0
    merged_into_base: bool = False
    mirror_of_upstream: bool = False
    upstream_sha: str | None = None
    upstream_relation: str | None = None  # behind / ahead / diverged / unknown
    upstream_behind: int = 0
    related_prs: list[int] = field(default_factory=list)
    relation_kinds: list[str] = field(default_factory=list)


def collect_branches(git: Git, remote: str) -> dict[str, Branch]:
    prefix = f"refs/remotes/{remote}/"
    fmt = "%(refname)%09%(objectname)%09%(committerdate:short)%09%(authorname)%09%(subject)"
    branches: dict[str, Branch] = {}
    for line in git.lines("for-each-ref", f"--format={fmt}", prefix):
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        refname, sha, date, author, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        name = refname[len(prefix):]
        if name == "HEAD":
            continue
        branches[name] = Branch(
            name=name,
            ref=f"{remote}/{name}",
            sha=sha,
            committed=date,
            subject=subject,
            author=author,
        )
    return branches


def upstream_heads(git: Git, remote: str, allow_network: bool) -> dict[str, str]:
    """Map upstream branch name -> tip sha, from the network or local refs."""
    heads: dict[str, str] = {}
    if allow_network:
        out = git.run("ls-remote", "--heads", remote, check=False)
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha.strip()
        if heads:
            return heads
    prefix = f"refs/remotes/{remote}/"
    for line in git.lines("for-each-ref", "--format=%(refname)\t%(objectname)", prefix, check=False):
        ref, _, sha = line.partition("\t")
        heads[ref[len(prefix):]] = sha
    return heads


# --------------------------------------------------------------------------- #
# GitHub pull requests
# --------------------------------------------------------------------------- #
def search_prs(query: str, token: str | None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    cursor = None
    while True:
        data = graphql(PR_SEARCH_QUERY, {"q": query, "after": cursor}, token)
        result = data["search"]
        nodes.extend(n for n in result["nodes"] if n)
        if not result["pageInfo"]["hasNextPage"]:
            break
        cursor = result["pageInfo"]["endCursor"]
    return nodes


def fork_open_prs(fork: str, token: str | None) -> list[dict[str, Any]]:
    owner, _, name = fork.partition("/")
    nodes: list[dict[str, Any]] = []
    cursor = None
    while True:
        data = graphql(
            FORK_PR_QUERY, {"owner": owner, "name": name, "after": cursor}, token
        )
        repo = data.get("repository") or {}
        result = repo.get("pullRequests") or {"nodes": [], "pageInfo": {"hasNextPage": False}}
        nodes.extend(n for n in result["nodes"] if n)
        if not result["pageInfo"]["hasNextPage"]:
            break
        cursor = result["pageInfo"]["endCursor"]
    return nodes


def gather_pull_requests(
    fork: str, upstream: str, token: str | None
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return (open PRs by head ref, historical PRs by head ref) for the fork."""
    fork_owner = fork.split("/")[0]
    everything = search_prs(f"repo:{upstream} is:pr author:{fork_owner}", token)
    everything += fork_open_prs(fork, token)

    open_by_ref: dict[str, list[dict]] = {}
    past_by_ref: dict[str, list[dict]] = {}
    seen: set[tuple[str, int]] = set()
    for pr in everything:
        owner = (pr.get("headRepositoryOwner") or {}).get("login") or ""
        if owner and owner.lower() != fork_owner.lower():
            continue
        key = ((pr.get("baseRepository") or {}).get("nameWithOwner") or "", pr["number"])
        if key in seen:
            continue
        seen.add(key)
        bucket = open_by_ref if pr.get("state") == "OPEN" else past_by_ref
        bucket.setdefault(pr["headRefName"], []).append(pr)
    return open_by_ref, past_by_ref


# --------------------------------------------------------------------------- #
# Relation analysis
# --------------------------------------------------------------------------- #
def jira_key(text: str) -> str | None:
    match = JIRA_IN_TEXT_RE.search(text or "")
    return match.group(0).upper() if match else None


def compute_relations(
    git: Git,
    branches: dict[str, Branch],
    open_prs: dict[str, list[dict]],
    base_ref: str,
    remote: str,
    max_commits: int,
) -> set[str]:
    """Mark branches sharing history with an open-PR branch. Returns truncated PR refs."""
    truncated: set[str] = set()
    prefix = f"{remote}/"
    for ref_name, prs in open_prs.items():
        anchor = branches.get(ref_name)
        tip = anchor.sha if anchor else (prs[0].get("headRefOid") or "")
        if not tip or not has_commit(git.cwd, tip):
            continue

        own = git.lines(
            "rev-list", f"--max-count={max_commits + 1}", tip, f"^{base_ref}", check=False
        )
        if len(own) > max_commits:
            truncated.add(ref_name)
            own = own[:max_commits]
        if not own:
            own = [tip]

        touched: set[str] = set()
        for commit in own:
            for line in git.lines(
                "branch", "-r", "--contains", commit, "--format=%(refname:short)", check=False
            ):
                if line.startswith(prefix):
                    touched.add(line[len(prefix):])

        numbers = sorted(pr["number"] for pr in prs)
        for name in touched:
            branch = branches.get(name)
            if branch is None or name == ref_name:
                continue
            for number in numbers:
                if number not in branch.related_prs:
                    branch.related_prs.append(number)
            if "shared history" not in branch.relation_kinds:
                branch.relation_kinds.append("shared history")
    return truncated


def apply_jira_relations(
    branches: dict[str, Branch], open_prs: dict[str, list[dict]]
) -> None:
    keys: dict[str, list[int]] = {}
    for ref_name, prs in open_prs.items():
        for pr in prs:
            key = jira_key(ref_name) or jira_key(pr.get("title", ""))
            if key:
                keys.setdefault(key, []).append(pr["number"])
    for branch in branches.values():
        if branch.name in open_prs:
            continue
        key = jira_key(branch.name)
        if key and key in keys:
            for number in keys[key]:
                if number not in branch.related_prs:
                    branch.related_prs.append(number)
            if "same JIRA key" not in branch.relation_kinds:
                branch.relation_kinds.append("same JIRA key")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    branch: str
    status: str
    last_commit: str
    age_days: int | None
    ahead: int
    behind: int
    comments: str
    details: dict[str, Any]


def age_in_days(date: str) -> int | None:
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        return None
    return (dt.date.today() - day).days


def describe_prs(numbers: Iterable[int]) -> str:
    numbers = sorted(set(numbers))
    return ", ".join(f"#{n}" for n in numbers)


def classify(
    branch: Branch,
    open_prs: dict[str, list[dict]],
    past_prs: dict[str, list[dict]],
    upstream_name: str,
    recent_days: int,
    truncated: set[str],
    protected: set[str],
    know_upstream: bool,
) -> Row:
    notes: list[str] = []
    age = age_in_days(branch.committed)

    own_pr = open_prs.get(branch.name)
    history = past_prs.get(branch.name) or []
    merged = [p for p in history if p.get("mergedAt")]
    closed = [p for p in history if not p.get("mergedAt")]

    if own_pr:
        numbers = describe_prs(p["number"] for p in own_pr)
        drafts = [p for p in own_pr if p.get("isDraft")]
        notes.append(f"head branch of open PR {numbers}" + (" (draft)" if drafts else ""))
        status = "ACTIVE"
    elif branch.name in protected:
        notes.append("protected/base branch of the fork, never treated as stale")
        status = "ACTIVE"
    elif branch.related_prs:
        kinds = " and ".join(branch.relation_kinds)
        notes.append(
            f"related to open PR {describe_prs(branch.related_prs)} by {kinds}"
        )
        status = "CANDIDATE"
    elif closed:
        numbers = describe_prs(p["number"] for p in closed)
        notes.append(f"PR {numbers} was closed without merging - work may be revived")
        status = "CANDIDATE"
    elif merged:
        numbers = describe_prs(p["number"] for p in merged)
        when = (merged[-1].get("mergedAt") or "")[:10]
        notes.append(f"PR {numbers} already merged upstream{' on ' + when if when else ''}")
        status = "STALE"
    elif branch.mirror_of_upstream:
        notes.append(
            f"identical to the {upstream_name} branch of the same name "
            "(inherited when the fork was created, no personal commits)"
        )
        status = "STALE"
    elif branch.upstream_sha:
        same = f"the {upstream_name} branch of the same name"
        if branch.upstream_relation == "behind":
            notes.append(
                f"outdated copy of {same}, {branch.upstream_behind} commits behind it "
                "and with no commits of its own"
            )
            status = "STALE"
        elif branch.upstream_relation == "ahead":
            notes.append(f"sits on top of {same} with commits that are not there")
            status = "CANDIDATE"
        elif branch.upstream_relation == "diverged":
            notes.append(f"diverges from {same}")
            status = "CANDIDATE"
        else:
            notes.append(
                f"differs from {same}, but the upstream tip is not in this clone, "
                "so the relation could not be verified"
            )
            status = "CANDIDATE"
    elif branch.merged_into_base or branch.ahead == 0:
        notes.append("no commits of its own - everything is already in the base branch")
        status = "STALE"
    elif age is not None and age <= recent_days:
        notes.append(
            f"{branch.ahead} own commits and last activity {age} days ago - "
            "possibly work in progress"
        )
        status = "CANDIDATE"
    else:
        notes.append(
            f"no open PR and no link to one; {branch.ahead} own commits, "
            f"last commit {branch.committed}"
        )
        status = "STALE"

    if status != "ACTIVE" and branch.name in truncated:
        notes.append("history comparison was truncated, relation not fully verified")
        status = "CANDIDATE"

    looks_upstream = bool(re.match(r"(branch-|rel/|gh-pages|feature-|trunk)", branch.name))
    if know_upstream and looks_upstream and not branch.upstream_sha:
        notes.append(f"no branch with this name in {upstream_name} any more")
    if branch.ahead and status != "ACTIVE":
        notes.append(f"ahead {branch.ahead} / behind {branch.behind} of the base branch")
    if age is not None and age > 365 and status == "STALE":
        notes.append(f"untouched for {age // 365} year(s)")

    return Row(
        branch=branch.name,
        status=status,
        last_commit=branch.committed,
        age_days=age,
        ahead=branch.ahead,
        behind=branch.behind,
        comments="; ".join(notes),
        details={
            "sha": branch.sha,
            "subject": branch.subject,
            "last_author": branch.author,
            "open_prs": [p["number"] for p in (own_pr or [])],
            "related_prs": sorted(set(branch.related_prs)),
            "relation_kinds": branch.relation_kinds,
            "merged_prs": [p["number"] for p in merged],
            "closed_prs": [p["number"] for p in closed],
            "mirror_of_upstream": branch.mirror_of_upstream,
            "upstream_sha": branch.upstream_sha,
            "merged_into_base": branch.merged_into_base,
        },
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_table(rows: list[Row], width: int) -> str:
    headers = ("Branch", "Status", "Last commit", "A/B", "Comments")
    branch_w = min(34, max(len(headers[0]), max(len(r.branch) for r in rows)))
    status_w = max(len(headers[1]), max(len(r.status) for r in rows))
    date_w = max(len(headers[2]), 10)
    ab = {r.branch: f"{r.ahead}/{r.behind}" for r in rows}
    ab_w = max(len(headers[3]), max(len(v) for v in ab.values()))
    fixed = branch_w + status_w + date_w + ab_w + 4 * 2
    comments_w = max(30, width - fixed)
    widths = (branch_w, status_w, date_w, ab_w, comments_w)
    sep = "  "

    def emit(cells: tuple[str, ...]) -> list[str]:
        columns = [textwrap.wrap(text, w) or [""] for text, w in zip(cells, widths)]
        height = max(len(c) for c in columns)
        out = []
        for i in range(height):
            out.append(
                sep.join(
                    (col[i] if i < len(col) else "").ljust(w)
                    for col, w in zip(columns, widths)
                ).rstrip()
            )
        return out

    lines = emit(headers)
    lines.append(sep.join("-" * w for w in widths))
    for row in rows:
        lines.extend(
            emit((row.branch, row.status, row.last_commit, ab[row.branch], row.comments))
        )
    return "\n".join(lines)


def render_markdown(rows: list[Row], fork: str) -> str:
    lines = [
        "| Branch | Status | Last commit | Ahead/Behind | Comments |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        url = f"https://github.com/{fork}/tree/{row.branch}"
        comments = row.comments.replace("|", "\\|")
        lines.append(
            f"| [`{row.branch}`]({url}) | {row.status} | {row.last_commit} | "
            f"{row.ahead}/{row.behind} | {comments} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fork", default=DEFAULT_FORK, help=f"fork, owner/name (default: {DEFAULT_FORK})")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help=f"upstream repository (default: {DEFAULT_UPSTREAM})")
    parser.add_argument("--fork-remote", default=DEFAULT_FORK_REMOTE, help="git remote for the fork (default: origin)")
    parser.add_argument("--upstream-remote", default=DEFAULT_UPSTREAM_REMOTE, help="git remote for upstream (default: upstream)")
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF, help=f"base branch to compare against (default: {DEFAULT_BASE_REF})")
    parser.add_argument("--no-fetch", action="store_true", help="skip 'git fetch' and use the refs already present")
    parser.add_argument("--offline", action="store_true", help="no git network access at all (implies --no-fetch); upstream branches are read from local refs")
    parser.add_argument("--fetch-upstream-branches", action="store_true", help="fetch every upstream branch (bigger download) so branches sharing a name with one can be compared exactly instead of being left in doubt")
    parser.add_argument("--include-mirrors", action="store_true", help="also list branches identical to an upstream branch")
    parser.add_argument("--all", action="store_true", help="also list ACTIVE branches")
    parser.add_argument("--only", default=None, help="comma separated statuses to keep, e.g. STALE or STALE,CANDIDATE")
    parser.add_argument("--recent-days", type=int, default=30, help="a branch touched within this many days is a CANDIDATE (default: 30)")
    parser.add_argument("--max-pr-commits", type=int, default=25, help="commits per open-PR branch inspected for shared history (default: 25)")
    parser.add_argument("--protect", action="append", default=None, metavar="BRANCH", help="branch never reported as stale (default: trunk, main, master)")
    parser.add_argument("--format", choices=("table", "markdown", "json"), default="table")
    parser.add_argument("--width", type=int, default=None, help="table width (default: terminal width)")
    parser.add_argument("--repo-path", default=None, help=f"path of the git clone (default: current directory, or {DEFAULT_REPO_PATH} when it is not one)")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args = parse_args(argv)
    protected = set(args.protect or ["trunk", "main", "master"])
    git = Git(args.repo_path)

    if not args.no_fetch and not args.offline:
        print("Fetching refs ...", file=sys.stderr)
        git.run("fetch", "--prune", args.fork_remote, check=False)
        if args.fetch_upstream_branches:
            git.run(
                "fetch", args.upstream_remote,
                f"+refs/heads/*:refs/remotes/{args.upstream_remote}/*",
                check=False,
            )
        else:
            git.run("fetch", args.upstream_remote, check=False)

    if not has_commit(git.cwd, args.base_ref):
        raise SystemExit(
            f"Base ref {args.base_ref} not found; pass --base-ref or fetch "
            f"the {args.upstream_remote} remote."
        )

    branches = collect_branches(git, args.fork_remote)
    if not branches:
        raise SystemExit(f"No remote-tracking branches under {args.fork_remote}/.")

    heads = upstream_heads(git, args.upstream_remote, allow_network=not args.offline)
    for branch in branches.values():
        branch.upstream_sha = heads.get(branch.name)
        branch.mirror_of_upstream = branch.upstream_sha == branch.sha

    merged_prefix = f"{args.fork_remote}/"
    for line in git.lines("branch", "-r", "--merged", args.base_ref, "--format=%(refname:short)", check=False):
        if line.startswith(merged_prefix):
            branch = branches.get(line[len(merged_prefix):])
            if branch:
                branch.merged_into_base = True

    token = resolve_token(args.token)
    open_prs, past_prs = gather_pull_requests(args.fork, args.upstream, token)

    truncated = compute_relations(
        git, branches, open_prs, args.base_ref, args.fork_remote, args.max_pr_commits
    )
    apply_jira_relations(branches, open_prs)

    # Ahead/behind counts are only needed for branches that will be judged;
    # mirrors of upstream branches are decided by their sha alone.
    interesting = [
        b for b in branches.values()
        if not b.mirror_of_upstream or args.include_mirrors
    ]
    for branch in interesting:
        if branch.mirror_of_upstream:
            continue
        out = git.run(
            "rev-list", "--left-right", "--count", f"{args.base_ref}...{branch.ref}", check=False
        ).split()
        if len(out) == 2:
            branch.behind, branch.ahead = int(out[0]), int(out[1])

        # How does it stand against the upstream branch carrying the same name?
        if branch.upstream_sha:
            if not has_commit(git.cwd, branch.upstream_sha):
                branch.upstream_relation = "unknown"
            elif is_ancestor(git.cwd, branch.sha, branch.upstream_sha):
                branch.upstream_relation = "behind"
                branch.upstream_behind = git.count(f"{branch.sha}..{branch.upstream_sha}")
            elif is_ancestor(git.cwd, branch.upstream_sha, branch.sha):
                branch.upstream_relation = "ahead"
            else:
                branch.upstream_relation = "diverged"

    rows = [
        classify(
            b, open_prs, past_prs, args.upstream, args.recent_days,
            truncated, protected, bool(heads),
        )
        for b in interesting
    ]

    wanted = {s.strip().upper() for s in args.only.split(",")} if args.only else None
    if wanted:
        rows = [r for r in rows if r.status in wanted]
    elif not args.all:
        rows = [r for r in rows if r.status != "ACTIVE"]

    order = {"STALE": 0, "CANDIDATE": 1, "ACTIVE": 2}
    rows.sort(key=lambda r: (order.get(r.status, 9), -(r.age_days or 0), r.branch))

    if args.format == "json":
        json.dump([asdict(r) for r in rows], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not rows:
        print("Nothing to report.")
        return 0
    if args.format == "markdown":
        print(render_markdown(rows, args.fork))
        return 0

    hidden = len(branches) - len(interesting)
    counts = {status: sum(1 for r in rows if r.status == status) for status in order}
    print(
        f"Branches of {args.fork} compared with {args.base_ref} - "
        f"{counts['STALE']} STALE, {counts['CANDIDATE']} CANDIDATE, "
        f"{counts['ACTIVE']} ACTIVE"
    )
    if hidden:
        print(
            f"({hidden} branches identical to {args.upstream} branches were skipped; "
            "use --include-mirrors to list them)"
        )
    print()
    width = args.width or shutil.get_terminal_size((120, 24)).columns
    print(render_table(rows, max(80, width)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
