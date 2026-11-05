#!/usr/bin/env python3
"""Rebuild each generated branch that has fallen behind from its feat/ branch.

Picks every feat/<name> branch of the fork whose generated <name> branch sits
on an older trunk than upstream's, and requests build_upstream.yml to publish
staged/<name> as <name> by pushing PREPARATION to build/<name>. Each request
starts a Build run in the fork.

Requests nothing for a branch the workflow would refuse or that would not help,
and says why: staged/<name> is missing, holds other code than feat/<name>, or is
no newer than <name>, or <name> holds work that feat/<name> does not contain
(the workflow only replaces a branch feat/ contains, or one commit on trunk).

A dry run unless --apply is passed, which pushes the requests. It stops at the
request: the publishing runs in GitHub Actions, so check its result there.

Examples
--------
    python build_generated.py
    python build_generated.py --apply
"""

from __future__ import annotations

import argparse
import sys

import analyze_pr as core
from analyze_pr import is_ancestor
from list_feat_branches import FORK, STAGED_REFS, UPSTREAM, fork_owned, read_tip
from list_stale_branches import Git
from merge_preparation import PREPARATION, behind_branches


def request_build(git: Git, name: str, behind: int, apply: bool,
                  owned: list[str] | None = None) -> tuple[str, bool]:
    """Request the publishing of staged/<name>; return what happened and whether it went well."""
    trunk = f"{UPSTREAM}/{core.BASE}"
    feat, generated, staged = f"{FORK}/feat/{name}", f"{FORK}/{name}", f"{STAGED_REFS}/{name}"
    if core.has_commit(git.cwd, f"{FORK}/build/{name}"):
        return "build/ request already pending", True
    if not core.has_commit(git.cwd, staged):
        return f"SKIPPED, no staged/{name}: push feat/{name} first", False
    tag = read_tip(git, staged, trunk, owned)
    if tag.change != read_tip(git, feat, trunk, owned).change:
        return f"SKIPPED, staged/{name} has other code than feat/{name}", False
    if tag.behind >= behind:
        return f"SKIPPED, staged/{name} is no newer (base {tag.behind} behind)", False
    if not is_ancestor(git.cwd, generated, feat) and git.count(f"{trunk}..{generated}") != 1:
        return f"SKIPPED, {name} holds work that is not in feat/{name}", False

    detail = f"staged base {tag.behind} behind"
    if not apply:
        return f"would request a build, {detail}", True
    status, _ = core.git_run(git.cwd, "push", FORK, f"{PREPARATION}:refs/heads/build/{name}")
    if status:
        return f"PUSH FAILED ({status})", False
    return f"build requested, {detail}", True


def build_all(git: Git, apply: bool) -> bool:
    """Report on each picked branch; return whether all went well."""
    picked = behind_branches(git)
    owned = fork_owned(git)  # the fork's own paths, as the staging workflow drops them
    fine = True
    for name, behind in picked:
        result, went_well = request_build(git, name, behind, apply, owned)
        fine = fine and went_well
        print(f"{name} (base {behind} behind): {result}")
    if not picked:
        print("No generated branch is behind.")
    return fine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)],
    )
    parser.add_argument("--apply", action="store_true", help="push the build requests (default: dry run)")
    parser.add_argument("--no-fetch", action="store_true", help="skip 'git fetch' and use the refs already present")
    parser.add_argument("--repo-path", default=None,
                        help=f"path of the git clone (default: current directory, or {core.DEFAULT_REPO_PATH} when it is not one)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    git = Git(args.repo_path)
    if not args.no_fetch:
        print("Fetching refs ...", file=sys.stderr)
        git.run("fetch", "--prune", FORK, f"+refs/tags/staged/*:{STAGED_REFS}/*",
                f"+refs/heads/*:refs/remotes/{FORK}/*")
        git.run("fetch", UPSTREAM)
    return 0 if build_all(git, args.apply) else 1


if __name__ == "__main__":
    sys.exit(main())
