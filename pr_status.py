#!/usr/bin/env python3
"""Where one feature stands: its feat/ branch, the staged commit, the branch and its checks.

For <name> (a JIRA key such as HADOOP-19972, or a branch name such as HADOOP-19972):

  feat/       the branch's tip, how far upstream trunk moved, and whether it has PREPARATION
  staged/     what the last push to feat/ staged, against feat/
  branch      the branch the pull request is opened from, against feat/
  checks      the fork's newest Build and Yetus runs, and whether they tested the branch tip
  pr          the apache/hadoop pull request

Then one line: the next action, in the order the fork's workflows expect. The plan
is derived from what is on the fork and in the PR, so it needs no local state: run
it after each push, or --all for every feat/ branch.

Read-only. Fetches origin and upstream unless --no-fetch.

Examples
--------
    python pr_status.py HADOOP-19972
    python pr_status.py --all
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

import analyze_pr as core
from list_feat_branches import FORK, STAGED_REFS, UPSTREAM, fork_owned, newest, pr_link, read_tip
from list_my_jiras import fork_run, fork_verdict, prs_of
from list_stale_branches import Git, search_prs
from list_upstream_prs import DEFAULT_FORK_OWNER, resolve_token


def slug(git: Git, remote: str) -> str:
    """The owner/name of a git remote, for the GitHub API."""
    url = git.run("remote", "get-url", remote).strip()
    return re.sub(r"^(?:git@github\.com:|https://github\.com/)", "", url).removesuffix(".git")


def authored_prs(repo: str, owner: str, token: str | None) -> list[dict[str, Any]]:
    """Every pull request of the fork owner into the upstream repository."""
    prs = search_prs(f"repo:{repo} is:pr author:{owner}", token)
    return [pr for pr in prs
            if ((pr.get("headRepositoryOwner") or {}).get("login") or "").lower() == owner.lower()]


def find_pr(name: str, prs: list[dict[str, Any]], repo: str, token: str | None) -> dict[str, Any] | None:
    """The PR a feature opens: by head branch, else by the JIRA key in any title."""
    by_head = [pr for pr in prs if pr["headRefName"] in (name, f"{name}-upstream")]
    return newest(by_head) or newest(prs_of(name, repo, token))


def yetus_tested(fork: str, tip: str | None, run: dict[str, Any], token: str | None) -> bool:
    """Whether the tip's Yetus status points at this run."""
    if not tip:
        return False
    suffix = f"/runs/{run['id']}"
    statuses = core.rest_json(f"repos/{fork}/commits/{tip}/status", token).get("statuses") or []
    return any(s.get("context") == "Yetus" and (s.get("target_url") or "").endswith(suffix)
               for s in statuses)


def status(git: Git, name: str, prs: list[dict[str, Any]], repo: str, token: str | None,
           owned: list[str] | None = None) -> list[str]:
    """The lines describing one feature, the next action last."""
    fork = slug(git, FORK)  # owner/name, for the GitHub API
    trunk = f"{UPSTREAM}/{core.BASE}"
    feat_ref = f"{FORK}/feat/{name}"
    if not core.has_commit(git.cwd, feat_ref):
        return [f"feat/{name}: not on {fork}",
                f"NEXT: git push origin feat/{name}"]

    owned = owned or fork_owned(git)  # the fork's own paths, which no branch is behind on
    feat = read_tip(git, feat_ref, trunk, owned)
    prep = f"{FORK}/PREPARATION"
    ahead = git.count(f"{feat_ref}..{prep}")
    lines = [f"feat/{name}: {feat.sha[:12]}, base {feat.behind} behind {trunk}, "
             f"{feat.commits} commits, PREPARATION {ahead} ahead"
             + ("  <- merge PREPARATION" if ahead else "")]

    pr = find_pr(name, prs, repo, token)
    # <name> and <name>-upstream are what build/<name> publishes, and where the fork's
    # Build and Yetus runs of this feature are; any other head is pushed by hand.
    head = pr["headRefName"] if pr else name
    by_workflow = head in (name, f"{name}-upstream")
    steps: list[str] = []

    staged_ref = f"{STAGED_REFS}/{name}"
    staged = staged_same = None
    if core.has_commit(git.cwd, staged_ref):
        staged = read_tip(git, staged_ref, trunk, owned)
        staged_same = staged.change == feat.change
        lines.append(f"staged/{name}: {staged.sha[:12]}, "
                     f"{'same code as feat/' if staged_same else 'DIFFERS from feat/'}")
        # build/<head> publishes the staged tag, so a stale one must be re-staged first.
        if not staged_same:
            steps.append("restage")
    else:
        lines.append(f"staged/{name}: none")
        steps.append("restage")  # build/<head> fails without a staged commit

    head_ref = f"{FORK}/{head}"
    generated = (read_tip(git, head_ref, trunk, owned)
                 if core.has_commit(git.cwd, head_ref) else None)
    if generated is None:
        lines.append(f"{head}: not on {fork} yet")
        steps.append("publish")
    else:
        same = generated.change == feat.change
        lines.append(f"{head}: {generated.sha[:12]}, "
                     f"{'same code as feat/' if same else 'DIFFERS from feat/'}")
        if not same:
            steps.append("publish")
    tip = generated.sha if generated else None

    build = fork_run(fork, head, "Build", token) if tip else None
    # A Yetus run is listed under its request branch, yetus/<head>.
    yetus = fork_run(fork, f"yetus/{head}", "Yetus", token) if tip else None
    build_on_tip = bool(build and build.get("head_sha") == tip)
    yetus_on_tip = bool(yetus and yetus_tested(fork, tip, yetus, token))
    def date(run: dict[str, Any] | None) -> str:
        return f", {(run.get('created_at') or '')[:10]}" if run else ""

    lines.append(f"Build: {fork_verdict(build) if build else 'none'}{date(build)}"
                 + ("" if build is None or build_on_tip else "  <- not on the branch tip"))
    lines.append(f"Yetus: {fork_verdict(yetus, True) if yetus else 'none'}{date(yetus)}"
                 + ("" if yetus is None or yetus_on_tip else "  <- not on the branch tip"))

    if not by_workflow:
        # build/<name> cannot publish this head, so it is behind until pushed by hand.
        lines.append(f"  build/<name> does not publish {head}: push it by hand")
    # Only matters when feat/ would be pushed: a push runs its own workflow copy.
    if staging_is_broken(git, feat_ref) and (steps or not ahead):
        lines.append("  its staging workflow still reads fork-owned.sh, which PREPARATION dropped: "
                     "merge PREPARATION first")
        steps.append("staging-broken")
    if pr is None:
        lines.append("PR: none")
        steps.append("open-pr")
    else:
        lines.append(f"PR: {pr_link(pr)}, head {head}" + (", draft" if pr.get("isDraft") else ""))
        if pr["state"] != "OPEN":
            return lines + [f"NEXT: the PR is {pr['state'].lower()}"]
        if pr.get("headRefOid") and tip and pr["headRefOid"] != tip:
            lines.append("  the PR head is not the branch tip")
            steps.append("publish")
        if build_on_tip and build.get("status") == "completed" and build.get("conclusion") != "success":
            steps.append("build-red")
        if not yetus_on_tip:
            steps.append("yetus")

    return lines + [f"NEXT: {next_step(name, head, steps, by_workflow)}"]


def staging_is_broken(git: Git, feat_ref: str) -> bool:
    """Whether the branch's own staging workflow still reads the removed fork-owned.sh.

    The workflow copies both scripts out of PREPARATION, which no longer carries
    fork-owned.sh, so its next push to feat/ fails before staging anything.
    """
    workflow = f"{feat_ref}:.github/workflows/upstream_branch.yml"
    return bool(core.git_run(git.cwd, "cat-file", "-e", workflow)[0] == 0
                and "fork-owned" in git.run("show", workflow))


def next_step(feat_name: str, head: str, steps: list[str], by_workflow: bool = True) -> str:
    """The one action the fork's workflows expect next.

    feat_name is pushed to; head is the branch build/<name> and yetus/<name> act on
    (<name> or <name>-upstream).
    """
    if feat_name.startswith("HARNESS"):
        return "nothing to publish: feat/HARNESS only carries the fork's files"
    if not by_workflow:
        if "publish" in steps:
            return f"{head} is behind feat/{feat_name}: push it by hand, then re-run the checks"
        return "green and published: ask for a review"
    if "staging-broken" in steps:
        return "git merge origin/PREPARATION   (its staging workflow reads a script PREPARATION no longer has)"

    if "restage" in steps:  # the staged tag, and so build/<head>, is missing or older
        return f"git push origin feat/{feat_name}   (stage it; then publish)"
    if "publish" in steps and "open-pr" in steps:
        return (f"git push origin PREPARATION:build/{head}, then gh pr create --repo "
                f"apache/hadoop --draft --base {core.BASE} --head {DEFAULT_FORK_OWNER}:{head}")
    if "publish" in steps:
        return f"git push origin PREPARATION:build/{head}   (publish + Build)"
    if "build-red" in steps:
        return "fix the Build failure, then push feat/ and publish again"
    if "yetus" in steps:
        return f"git push origin PREPARATION:yetus/{head}   (Yetus only)"
    if "open-pr" in steps:
        return (f"gh pr create --repo apache/hadoop --draft --base {core.BASE} "
                f"--head {DEFAULT_FORK_OWNER}:{head}")
    return "green and published: ask for a review"


def feat_names(git: Git) -> list[str]:
    """The names of the fork's feat/ branches, without the 'feat/' prefix."""
    return sorted(ref.removeprefix(f"{FORK}/feat/") for ref in
                  git.lines("for-each-ref", "--format=%(refname:short)", f"refs/remotes/{FORK}/feat"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)])
    parser.add_argument("name", nargs="?", help="JIRA key or branch name, e.g. HADOOP-19972")
    parser.add_argument("--all", action="store_true", help="every feat/ branch, next action only")
    parser.add_argument("--author", default=DEFAULT_FORK_OWNER,
                        help=f"owner of the fork and author of the PRs (default: {DEFAULT_FORK_OWNER})")
    parser.add_argument("--no-fetch", action="store_true", help="skip 'git fetch' and use the refs already present")
    parser.add_argument("--repo-path", default=None,
                        help="path of the git clone (default: current directory, or "
                             f"{core.DEFAULT_REPO_PATH})")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    if not args.name and not args.all:
        raise SystemExit("pass a JIRA key or branch name, or --all")
    git = Git(args.repo_path)
    if not args.no_fetch:
        print("Fetching refs ...", file=sys.stderr)
        git.run("fetch", "--prune", FORK, f"+refs/tags/staged/*:{STAGED_REFS}/*",
                f"+refs/heads/*:refs/remotes/{FORK}/*")
        git.run("fetch", UPSTREAM)
    token = resolve_token(args.token)
    prs = authored_prs(core.DEFAULT_REPO, args.author, token)
    names = feat_names(git) if args.all else [args.name.removeprefix("feat/")]
    owned = fork_owned(git)
    for name in names:
        lines = status(git, name, prs, core.DEFAULT_REPO, token, owned)
        print(f"{name}: {lines[-1].removeprefix('NEXT: ')}" if args.all else "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
