#!/usr/bin/env python3
"""Rebuild a stacked pull request branch by replaying its commits onto current trunk.

A stacked PR carries other issues' commits so CI tests the chain together. Those
commits rot: when a prerequisite lands on trunk, the stack still holds its old copy.
Rebuilding means cherry-picking the stack's own commits onto current trunk, in the
order the PR description gives, and pushing the result back to the branch.

That is what the `jetty-phase-*` PRs state in their bodies, and replaying commits -
not squashing each prerequisite's net change - is what keeps intra-stack dependencies
intact: HADOOP-20004, 20006 and 20007 all edit TestHttpServer, and each one's change
must apply on top of the previous one's.

Order and membership come from `--stack` (a file, or repeated flags):

    jetty-phase-a = 3bf3610bebcc..origin/jetty-phase-a
    jetty-phase-b = 3bf3610bebcc..origin/jetty-phase-b

Each line is `branch: commit-range`, where the range is what the PR carries: its own
commits on top of the trunk point it was last built on. `--stack` ranges are replayed
in the order given. A merge commit inside a range is dropped, since its content is
already in the commits it merged.

Dry run by default: prints each replayed commit. `--apply` pushes the result to the
branch with `--force-with-lease`. A conflict stops that stack and leaves the branch
alone. Read-only unless --apply.

Examples
--------
    python stack_pr.py --stack stacks.txt
    python stack_pr.py --stack stacks.txt --apply jetty-phase-a
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

import analyze_pr as core
from list_stale_branches import DEFAULT_FORK_REMOTE as FORK
from list_stale_branches import DEFAULT_UPSTREAM_REMOTE as UPSTREAM
from list_stale_branches import Git


def read_stacks(path: str | None, spec: list[str]) -> dict[str, list[str]]:
    """The stacks, as branch name -> commit ranges in replay order."""
    text = ""
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    text += "\n".join(spec)
    stacks: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        branch, _, ranges = line.partition(":")
        stacks[branch.strip()] = ranges.split()
    return stacks


def git(cwd: str, *args: str) -> tuple[int, str]:
    """Run git; return exit status and stdout, with stderr's first line on failure."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    out = (result.stdout or "").strip()
    if result.returncode and not out:
        out = (result.stderr or "").strip().splitlines()[-1:] or [""]
        out = out[0]
    return result.returncode, out


def replay(git_dir: str, commits: list[str]) -> tuple[str, bool, list[str]]:
    """Cherry-pick the commits onto trunk in order in a scratch worktree.

    Returns the tip, whether it worked, and one line per commit or the conflict.
    """
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "work")
        status, _ = git(git_dir, "worktree", "add", "-q", "--detach", work,
                        f"{UPSTREAM}/{core.BASE}")
        if status:
            return "", False, ["could not create a scratch worktree"]
        try:
            if not commits:
                return "", False, ["the stack has no commits of its own"]
            for sha in commits:
                title = git(work, "log", "-1", "--format=%s", sha)[1]
                body = git(work, "log", "-1", "--format=%B", sha)[1]
                status, out = git(work, "cherry-pick", "-x", "--no-commit", sha)
                if status:
                    conflict = git(work, "diff", "--name-only", "--diff-filter=U")[1]
                    git(work, "cherry-pick", "--abort")
                    return "", False, [*lines,
                                       f"CONFLICT replaying {sha[:12]} ({title}): "
                                       f"{conflict or out}",
                                       "  the stack was not pushed; resolve it in a"
                                       " worktree or rebuild the member's feat/ branch"]
                new = git(work, "commit", "-q", "--allow-empty", "-m",
                          f"{body}\n\n(cherry picked from commit {sha})")[0]
                if new:
                    return "", False, [*lines, f"could not commit {sha[:12]}"]
                lines.append(f"  {sha[:12]}  {title[:70]}")
            tip = git(work, "rev-parse", "HEAD")[1]
        finally:
            git(git_dir, "worktree", "remove", "--force", work)
    return tip, True, lines


def own_commits(git_obj: Git, ranges: list[str]) -> list[str]:
    """The stack's own commits: each range minus what its base already had from trunk.

    A range like `origin/jetty-phase-a` spans apache trunk commits too once the stack
    has been rebuilt on a newer trunk. Those are not the stack's to replay, so the base
    is the fork point against upstream, and anything reachable from upstream is dropped.
    """
    shas: list[str] = []
    for spec in ranges:
        tip = spec.split("..")[-1]
        base = git_obj.run("merge-base", tip, f"{UPSTREAM}/{core.BASE}").strip()
        shas += git_obj.run("rev-list", "--reverse", "--no-merges",
                            f"{base}..{tip}").split()
    # Drop anything upstream already has, keeping order and dropping duplicates.
    landed = set(git_obj.run("rev-list", f"{UPSTREAM}/{core.BASE}").split())
    seen: set[str] = set()
    return [sha for sha in shas
            if sha not in landed and not (sha in seen or seen.add(sha))]


def build(git_obj: Git, branch: str, ranges: list[str], apply: bool) -> tuple[str, bool]:
    """Rebuild one stack; return what happened and whether it went well."""
    if not ranges:
        return "no commit ranges listed", False
    commits = own_commits(git_obj, ranges)
    tip, ok, lines = replay(git_obj.cwd, commits)
    if not ok:
        return "\n".join(lines), False
    count = git_obj.count(f"{UPSTREAM}/{core.BASE}..{tip}")
    detail = f"{count} commits"
    if not apply:
        return f"would rebuild {detail}\n" + "\n".join(lines), True
    # The lease names what the fork holds; a stale local branch must not be the yardstick.
    tracked = f"{FORK}/{branch}"
    ref = tracked if core.has_commit(git_obj.cwd, tracked) else branch
    current = git_obj.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    status, _ = core.git_run(git_obj.cwd, "push", FORK,
                             f"--force-with-lease=refs/heads/{branch}:{current}",
                             f"{tip}:refs/heads/{branch}")
    if status:
        return f"PUSH FAILED ({status}), {detail}\n" + "\n".join(lines), False
    return f"rebuilt {detail}\n" + "\n".join(lines), True


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)])
    parser.add_argument("branches", nargs="*", help="stack branches to rebuild (default: all)")
    parser.add_argument("--stack", default=None,
                        help="file of `branch: commit-range...` lines, in replay order")
    parser.add_argument("--add-stack", action="append", default=[], metavar="BRANCH: RANGE...",
                        help="a stack definition, instead of or on top of the file")
    parser.add_argument("--apply", action="store_true",
                        help="push each rebuilt stack to its branch (default: dry run)")
    parser.add_argument("--no-fetch", action="store_true", help="skip 'git fetch'")
    parser.add_argument("--repo-path", default=None, help="path of the git clone")
    args = parser.parse_args(argv)

    git_obj = Git(args.repo_path)
    if not args.no_fetch:
        print("Fetching refs ...", file=sys.stderr)
        git_obj.run("fetch", "--prune", FORK, f"+refs/heads/*:refs/remotes/{FORK}/*")
        git_obj.run("fetch", UPSTREAM)
    stacks = read_stacks(args.stack, args.add_stack)
    if not stacks:
        raise SystemExit("no stack defined: pass --stack or --add-stack")
    wanted: list[str] = args.branches or list(stacks)
    fine = True
    for branch in wanted:
        if branch not in stacks:
            print(f"{branch}: not a stack in --stack or --add-stack")
            fine = False
            continue
        result, went_well = build(git_obj, branch, stacks[branch], args.apply)
        fine = fine and went_well
        print(f"{branch}: {result}")
    return 0 if fine else 1


if __name__ == "__main__":
    sys.exit(main())
