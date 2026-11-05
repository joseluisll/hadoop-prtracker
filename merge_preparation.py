#!/usr/bin/env python3
"""Merge PREPARATION into each feat/ branch whose generated branch has fallen behind.

Picks every feat/<name> branch of the fork whose generated <name> branch sits
on an older trunk than upstream's, and merges origin/PREPARATION into it. The
merge is made with 'git merge-tree' and 'git commit-tree', so no worktree is
touched and local branches stay as they are. A branch that conflicts is
reported and left alone.

A dry run unless --apply is passed, which pushes each merge to the fork; a push
that cannot fast-forward is refused, never forced. A merge that only brings
.github/ changes starts no workflow, so it does not stage a new commit
(see upstream_branch.yml), and the generated branch itself only moves through
build/<name>.

Examples
--------
    python merge_preparation.py
    python merge_preparation.py --apply
"""

from __future__ import annotations

import argparse
import sys

import analyze_pr as core
from list_feat_branches import FORK, UPSTREAM, fork_owned
from list_stale_branches import Git

PREPARATION = f"{FORK}/PREPARATION"


def merge_branch(git: Git, name: str, apply: bool, owned: list[str] | None = None) -> tuple[str, bool]:
    """Merge PREPARATION into feat/<name>; return what happened and whether it went well."""
    feat = f"{FORK}/feat/{name}"
    missing = git.count(f"{feat}..{PREPARATION}")
    if not missing:
        return "already has PREPARATION", True

    status, out = core.git_run(git.cwd, "merge-tree", "--write-tree", "--name-only", feat, PREPARATION)
    lines = out.splitlines()
    if status == 1:
        files = lines[1:lines.index("")] if "" in lines else lines[1:]
        return f"CONFLICT in {', '.join(files)}", False
    if status:
        return f"merge failed ({status}): {out[:200]}", False

    tree = lines[0]
    # The fork's own paths only: such a merge starts no staging workflow.
    owned = owned or fork_owned(git)
    fork_only = not git.lines("diff", "--name-only", feat, tree, "--", *owned)
    detail = f"{missing} commits" + (", fork-owned files only: starts no workflow" if fork_only else "")
    if not apply:
        return f"would merge {detail}", True
    commit = git.run("commit-tree", tree, "-p", feat, "-p", PREPARATION,
                     "-m", f"Merge PREPARATION into feat/{name}").strip()
    status, _ = core.git_run(git.cwd, "push", FORK, f"{commit}:refs/heads/feat/{name}")
    if status:
        return f"PUSH FAILED ({status}), {detail}", False
    return f"merged {detail}", True


def behind_branches(git: Git) -> list[tuple[str, int]]:
    """The feat/ names whose generated branch is on an older trunk, with how far behind."""
    trunk = f"{UPSTREAM}/{core.BASE}"
    prefix = f"{FORK}/feat/"
    names = [ref[len(prefix):] for ref in git.lines("for-each-ref", "--format=%(refname:short)",
                                                    f"refs/remotes/{prefix}")]
    picked = []
    for name in sorted(names):
        generated = f"{FORK}/{name}"
        if not core.has_commit(git.cwd, generated):
            continue
        base = git.run("merge-base", trunk, generated).strip()
        behind = git.count(f"{base}..{trunk}")
        if behind:
            picked.append((name, behind))
    return picked


def merge_all(git: Git, apply: bool) -> bool:
    """Report on each picked branch; return whether all went well."""
    picked = behind_branches(git)
    owned = fork_owned(git)
    fine = True
    for name, behind in picked:
        result, went_well = merge_branch(git, name, apply, owned)
        fine = fine and went_well
        print(f"feat/{name} (generated base {behind} behind): {result}")
    if not picked:
        print("No generated branch is behind.")
    return fine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)],
    )
    parser.add_argument("--apply", action="store_true", help="make the merges and push them (default: dry run)")
    parser.add_argument("--no-fetch", action="store_true", help="skip 'git fetch' and use the refs already present")
    parser.add_argument("--repo-path", default=None,
                        help=f"path of the git clone (default: current directory, or {core.DEFAULT_REPO_PATH} when it is not one)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    git = Git(args.repo_path)
    if not args.no_fetch:
        print("Fetching refs ...", file=sys.stderr)
        git.run("fetch", "--prune", FORK)
        git.run("fetch", UPSTREAM)
    return 0 if merge_all(git, args.apply) else 1


if __name__ == "__main__":
    sys.exit(main())
