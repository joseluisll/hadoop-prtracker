#!/usr/bin/env python3
"""List the unresolved JIRA issues you reported, each with its pull request.

For every issue it prints the status, the summary, and the Yetus, Build and
approval state of its PR. A PR belongs to the issue whose key comes first in
its title, whoever wrote it. The open PR is shown, else the latest one, so an
issue left open after its PR merged shows up too.

Read-only.

Examples
--------
    python list_my_jiras.py
    python list_my_jiras.py --reporter somebody --profile hbase
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import analyze_pr as core
from list_feat_branches import newest, pr_link, table
from list_stale_branches import jira_key
from list_upstream_prs import (DEFAULT_FORK_OWNER, QUERY, classify_contexts, graphql,
                               parse_yetus_comment, resolve_token)
from review_queue import last_commit

PAGE = 100


def my_issues(reporter: str) -> list[dict[str, Any]]:
    projects = ", ".join(core.PROFILE["jira_search"])
    jql = (f'reporter = "{reporter}" AND resolution = Unresolved '
           f"AND project in ({projects}) ORDER BY key")
    issues: list[dict[str, Any]] = []
    while True:
        data = core.jira_get(core.DEFAULT_JIRA, "search", {
            "jql": jql, "fields": "summary,status", "startAt": str(len(issues)),
            "maxResults": str(PAGE)})
        if data is None:
            raise SystemExit("JIRA search failed")
        issues.extend(data["issues"])
        if len(issues) >= data["total"]:
            return issues


def prs_of(key: str, repo: str, token: str | None) -> list[dict[str, Any]]:
    data = graphql(QUERY, {"q": f"repo:{repo} is:pr {key} in:title", "after": None}, token)
    return [pr for pr in data["search"]["nodes"] if pr and jira_key(pr.get("title") or "") == key]


def yetus_cell(pr: dict[str, Any]) -> str:
    report = parse_yetus_comment(pr, core.DEFAULT_BOTS)
    if report is None or not report.overall:
        return "None"
    date = (report.posted_at or "")[:10]
    stale = ", older than head" if (report.posted_at or "") < last_commit(pr) else ""
    return f"{report.overall}, {date}{stale}"


def build_cell(pr: dict[str, Any]) -> str:
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
    contexts = ((rollup or {}).get("contexts") or {}).get("nodes") or []
    check = next((c for c in contexts if c.get("name") == "Build"), None)
    if check is None:  # a profile whose workflows have other names
        return classify_contexts(pr)[0].verdict.capitalize()
    if check.get("status") != "COMPLETED":
        return "Running"
    return {"SUCCESS": "Pass", "ACTION_REQUIRED": "Awaiting approval to run"}.get(
        check.get("conclusion"), "Fail")


def approval_cell(pr: dict[str, Any]) -> str:
    cells = []
    for review in (pr.get("latestReviews") or {}).get("nodes") or []:
        login = (review.get("author") or {}).get("login") or "?"
        date = (review.get("submittedAt") or "")[:10]
        if review.get("state") == "APPROVED":
            cells.append(f"{login}, {date}")
        elif review.get("state") == "CHANGES_REQUESTED":
            cells.append(f"changes by {login}, {date}")
    return "; ".join(cells) or "None"


def row(issue: dict[str, Any], pr: dict[str, Any] | None) -> list[str]:
    key, fields = issue["key"], issue["fields"]
    cells = [f"[{key}]({core.DEFAULT_JIRA}/browse/{key})", fields["status"]["name"],
             fields["summary"].replace("|", "\\|")]
    if pr is None:
        return [*cells, "No PR", "-", "-", "-"]
    return [*cells, pr_link(pr), yetus_cell(pr), build_cell(pr), approval_cell(pr)]


def report(issues: list[dict[str, Any]], repo: str, token: str | None) -> str:
    rows = []
    for issue in issues:
        pr = newest(prs_of(issue["key"], repo, token))
        rows.append(row(issue, core.add_actions_yetus(pr, repo, token) if pr else None))
    headers = ["Ticket", "Status", "Summary", "PR", "Yetus", "Build", "Approval"]
    return f"{table(headers, rows)}\n\n{len(rows)} unresolved issues"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[core.profile_parser(argv)])
    parser.add_argument("--reporter", default=DEFAULT_FORK_OWNER,
                        help=f"JIRA user name (default: {DEFAULT_FORK_OWNER})")
    parser.add_argument("--upstream", default=core.DEFAULT_REPO,
                        help=f"upstream repository, owner/name (default: {core.DEFAULT_REPO})")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    print(report(my_issues(args.reporter), args.upstream, resolve_token(args.token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
