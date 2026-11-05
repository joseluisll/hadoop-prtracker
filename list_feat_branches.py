#!/usr/bin/env python3
"""Report every feat/ branch of the fork with its upstream branches and PRs.

For each feat/<name> branch, compares the generated <name> branch with it and
the <name>-upstream branch with the generated one, and names the pull request
into apache/hadoop. Two branches have the same code when they change the same
lines of the same files on top of their own trunk merge-bases, leaving .github/
out, so a branch on an older trunk can still match. Then lists the other
upstream branches of the fork by PR state, and what the comparison turned up:
staged tags, fork-only .github/ changes, local branches and PR heads out of
date.

Read-only, apart from 'git fetch'. The staged/<name> tags are fetched into
refs/prtracker/staged/ so the clone's own tags stay as they are.

Examples
--------
    python list_feat_branches.py
    python list_feat_branches.py --no-fetch > branches.md
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import analyze_pr as core
from list_stale_branches import DEFAULT_FORK_REMOTE as FORK
from list_stale_branches import DEFAULT_UPSTREAM_REMOTE as UPSTREAM
from list_stale_branches import Git, jira_key, search_prs
from list_upstream_prs import DEFAULT_FORK_OWNER, resolve_token

STAGED_REFS = "refs/prtracker/staged"
NO_GITHUB = ":(top,exclude).github"
# The fork's own paths, read from .github/FORK-OWNED: the fork keeps them, the
# staging workflow leaves them out of the upstream commit, so comparing two
# branches must leave them out too. Only .github/ when there is no list yet.
FORK_OWNED_REF = f"{FORK}/PREPARATION"


def fork_owned(git: Git) -> list[str]:
    """The pathspecs .github/FORK-OWNED of PREPARATION claims, as git excludes (only .github/ by default)."""
    status, text = core.git_run(git.cwd, "show", f"{FORK_OWNED_REF}:.github/FORK-OWNED")
    prefixes = [] if status else [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")]
    return [f":(top,exclude){prefix}" for prefix in prefixes] or [NO_GITHUB]


@dataclass
class Tip:
    """A branch as seen from trunk."""
    ref: str
    sha: str
    base: str
    behind: int  # commits of trunk its base is missing
    commits: int
    change: list[str]  # what it changes outside the fork-owned paths, sorted
    github_files: int


def read_tip(git: Git, ref: str, trunk: str, owned: list[str] | None = None) -> Tip:
    owned = owned or fork_owned(git)
    base = git.run("merge-base", trunk, ref).strip()
    diff = git.lines("diff", base, ref, "--", *owned)
    change = sorted(line for line in diff if line.startswith(("diff --git", "+", "-"))
                    and not line.startswith(("+++", "---")))
    return Tip(
        ref=ref,
        sha=git.run("rev-parse", ref).strip(),
        base=base,
        behind=git.count(f"{base}..{trunk}"),
        commits=git.count(f"{base}..{ref}"),
        change=change,
        github_files=len(git.lines("diff", "--name-only", base, ref, "--", ".github")),
    )


def code(a: Tip, b: Tip) -> str:
    return "Same code" if a.change == b.change else "Code differs"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def pr_state(pr: dict[str, Any]) -> str:
    if pr["state"] == "OPEN":
        return "open draft" if pr.get("isDraft") else "open"
    return pr["state"].lower()


def pr_link(pr: dict[str, Any], with_state: bool = True) -> str:
    link = f"[#{pr['number']}]({pr['url']})"
    return f"{link}, {pr_state(pr)}" if with_state else link


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [headers, ["---"] * len(headers), *rows]
    return "\n".join(f"| {' | '.join(line)} |" for line in lines)


def newest(prs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The open PR if there is one, else the latest."""
    return max(prs, key=lambda pr: (pr["state"] == "OPEN", pr["number"]), default=None)


def report(git: Git, prs: list[dict[str, Any]], owner: str) -> str:
    trunk = f"{UPSTREAM}/{core.BASE}"
    owned = fork_owned(git)
    prs = [pr for pr in prs
           if ((pr.get("headRepositoryOwner") or {}).get("login") or "").lower() == owner.lower()]
    by_head: dict[str, list[dict[str, Any]]] = {}
    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for pr in prs:
        by_head.setdefault(pr["headRefName"], []).append(pr)
        if pr["state"] == "OPEN":
            by_ticket.setdefault(jira_key(pr["title"]) or "", []).append(pr)

    prefix = f"refs/remotes/{FORK}/"
    shas = {}
    for line in git.lines("for-each-ref", "--format=%(refname) %(objectname)", prefix):
        ref, sha = line.split()
        shas[ref[len(prefix):]] = sha

    staged = {}
    staged_prefix = f"{STAGED_REFS}/"
    for line in git.lines("for-each-ref", "--format=%(refname) %(objectname)", STAGED_REFS):
        ref, sha = line.split()
        staged[ref[len(staged_prefix):]] = sha

    rows: list[list[str]] = []
    shown = set()  # branches the feat/ table accounts for
    with_pr = 0
    behind_tags, tag_differs, github_files = [], [], []
    for name in sorted(n[len("feat/"):] for n in shas if n.startswith("feat/")):
        feat = read_tip(git, f"{FORK}/feat/{name}", trunk, owned)
        shown.add(f"feat/{name}")
        generated = upstream = None
        vs_feat, gen_commits = "No generated branch", "–"
        if name in shas:
            shown.add(name)
            generated = read_tip(git, f"{FORK}/{name}", trunk, owned)
            base = "same base" if generated.base == feat.base else f"base {generated.behind} behind"
            vs_feat, gen_commits = f"{code(generated, feat)}, {base}", str(generated.commits)
            if generated.github_files:
                github_files.append((name, generated.github_files))
            if name in staged:
                if staged[name] != generated.sha:
                    behind_tags.append(name)
                tag = read_tip(git, f"{staged_prefix}{name}", trunk, owned)
                if tag.change != feat.change:
                    tag_differs.append(name)

        vs_generated = "no `-upstream`"
        if f"{name}-upstream" in shas:
            shown.add(f"{name}-upstream")
            upstream = read_tip(git, f"{FORK}/{name}-upstream", trunk, owned)
            base = "base current" if not upstream.behind else f"base {upstream.behind} behind"
            against = generated or feat
            vs_generated = (f"{code(upstream, against)}, {base}, "
                            f"{plural(upstream.commits, 'commit')}")

        pr = newest(by_head.get(f"{name}-upstream", []) + by_head.get(name, [])
                    or by_ticket.get(jira_key(name) or "?", []))
        head = "–"
        if pr:
            with_pr += 1
            shown.add(pr["headRefName"])
            head = {f"{name}-upstream": "`-upstream`", name: "generated"}.get(
                pr["headRefName"], f"`{pr['headRefName']}`")
        rows.append([f"`feat/{name}`", vs_feat, gen_commits, vs_generated,
                     pr_link(pr) if pr else "none", head])

    out = [f"{len(rows)} `feat/` branches on {FORK}: {with_pr} with a PR, {len(rows) - with_pr} without. "
           f"\"Behind\" counts commits behind `{trunk}` "
           f"(`{git.run('rev-parse', '--short', trunk).strip()}`).", ""]
    out += ["## `feat/` branches", "",
            table(["`feat/` branch", "Generated vs `feat/`", "Commits on generated",
                   "`-upstream` vs generated", "PR", "PR head"], rows), ""]

    groups: dict[str, list[list[str]]] = {"OPEN": [], "MERGED": [], "CLOSED": []}
    no_pr = []
    for name in sorted(n for n in shas if n not in shown):
        pr = newest(by_head.get(name, []))
        if pr:
            ticket = jira_key(pr["title"])
            detail = f" ({ticket})" if ticket and ticket not in name.upper() else ""
            draft = ", draft" if pr.get("isDraft") else ""
            groups[pr["state"]].append([f"`{name}`", f"{pr_link(pr, False)}{draft}{detail}"])
        elif jira_key(name):
            no_pr.append(name)

    out += ["## Other upstream branches, no `feat/` branch", ""]
    for state, title in (("OPEN", "Open PRs"), ("MERGED", "Merged PRs, branch still on origin"),
                         ("CLOSED", "Closed without merging")):
        if groups[state]:
            out += [f"**{title}**", "", table(["Branch", "PR"], groups[state]), ""]
    if no_pr:
        out += ["**No PR**", "", *(f"- `{name}`" for name in no_pr), ""]

    notes = []
    if behind_tags:
        notes.append(f"**Staged tags:** {len(behind_tags)} generated branches "
                     f"not at `staged/<name>`: {', '.join(f'`{n}`' for n in behind_tags)}.")
    if tag_differs:
        notes.append(f"**Tags with other code than `feat/`:** {', '.join(f'`{n}`' for n in tag_differs)}.")
    if github_files:
        listed = ", ".join(f"`{n}` ({count})" for n, count in github_files)
        notes.append(f"**Fork-only `.github/` changes on generated branches, by file count:** {listed}.")
    stale_heads = [f"[#{pr['number']}]({pr['url']})" for pr in prs
                   if pr["state"] == "OPEN" and shas.get(pr["headRefName"], pr["headRefOid"]) != pr["headRefOid"]]
    notes.append(f"**PR heads out of date:** {', '.join(stale_heads)}."
                 if stale_heads else "**PR heads:** every open PR matches its branch tip.")

    behind_local, ahead_local, local_only = [], [], []
    for name in git.lines("for-each-ref", "--format=%(refname:lstrip=2)",
                          "refs/heads/feat", "refs/heads/*-upstream"):
        if name not in shas:
            local_only.append(f"`{name}`")
            continue
        ahead, behind = git.run("rev-list", "--left-right", "--count",
                                f"{name}...{FORK}/{name}").split()
        if int(behind):
            behind_local.append(f"`{name}` ({behind})")
        if int(ahead):
            ahead_local.append(f"`{name}` ({ahead})")
    if behind_local:
        notes.append(f"**Local branches behind origin, by commits:** {', '.join(behind_local)}.")
    if ahead_local:
        notes.append(f"**Local branches ahead of origin, by commits:** {', '.join(ahead_local)}.")
    if local_only:
        notes.append(f"**Local only:** {', '.join(local_only)}.")
    gone = sum(pr["state"] != "OPEN" and pr["headRefName"] not in shas for pr in prs)
    notes.append(f"**Left out:** {plural(gone, 'merged or closed PR')} whose branch is no longer on origin.")

    out += ["## Notes", "", *(f"- {note}" for note in notes)]
    return "\n".join(out)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)],
    )
    parser.add_argument("--author", default=DEFAULT_FORK_OWNER,
                        help=f"owner of the fork and author of the PRs (default: {DEFAULT_FORK_OWNER})")
    parser.add_argument("--upstream", default=core.DEFAULT_REPO,
                        help=f"upstream repository (default: {core.DEFAULT_REPO})")
    parser.add_argument("--no-fetch", action="store_true",
                        help="skip 'git fetch' and use the refs already present")
    parser.add_argument("--repo-path", default=None,
                        help=f"path of the git clone (default: current directory, or {core.DEFAULT_REPO_PATH} when it is not one)")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args = parse_args(argv)
    git = Git(args.repo_path)
    if not args.no_fetch:
        print("Fetching refs ...", file=sys.stderr)
        git.run("fetch", "--prune", FORK, f"+refs/tags/staged/*:{STAGED_REFS}/*",
                f"+refs/heads/*:refs/remotes/{FORK}/*")
        git.run("fetch", UPSTREAM)
    prs = search_prs(f"repo:{args.upstream} is:pr author:{args.author}", resolve_token(args.token))
    print(report(git, prs, args.author))
    return 0


if __name__ == "__main__":
    sys.exit(main())
