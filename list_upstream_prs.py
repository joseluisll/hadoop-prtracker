#!/usr/bin/env python3
"""List the still-open pull requests that propose merging a branch of a personal
fork (default: joseluisll/hadoop) into an upstream repository (default:
apache/hadoop, branch trunk).

For every PR it prints:

    ID | Title | Branch | Status | Comments

where *Status* is a single derived verdict (why the PR is not merged yet) and
*Comments* spells out the evidence behind it: GitHub Actions result, Apache
Yetus / Jenkins result, review decision, missing reviewers, merge conflicts,
draft state, staleness, ...

Authentication: the script uses, in order of preference,
  1. ``$GITHUB_TOKEN`` or ``$GH_TOKEN``
  2. the token of the GitHub CLI (``gh auth token``)
A token is only needed for the (generous) authenticated rate limit; a public
repository can also be queried anonymously, but expect throttling.

Examples
--------
    python list_upstream_prs.py
    python list_upstream_prs.py --fork-owner joseluisll --base trunk
    python list_upstream_prs.py --format markdown > prs.md
    python list_upstream_prs.py --format json | jq '.[].status'
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
RETRIES = 3
RETRY_WAIT = 2.0  # seconds, multiplied by the attempt number

DEFAULT_UPSTREAM = "apache/hadoop"
DEFAULT_FORK_OWNER = "joseluisll"
DEFAULT_BASE_BRANCH = "trunk"
DEFAULT_BOTS = ("hadoop-yetus",)

# Contexts whose name matches this are the ASF Jenkins / Yetus precommit job
# rather than a GitHub Actions workflow.
YETUS_RE = re.compile(r"yetus|jenkins", re.IGNORECASE)

QUERY = """
query($q: String!, $after: String) {
  rateLimit { remaining resetAt }
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        createdAt
        updatedAt
        baseRefName
        headRefName
        mergeable
        author { login }
        headRepositoryOwner { login }
        headRepository { nameWithOwner }
        reviewDecision
        latestReviews(first: 20) {
          nodes { author { login } state submittedAt }
        }
        reviewRequests(first: 20) {
          nodes {
            requestedReviewer {
              __typename
              ... on User { login }
              ... on Team { name }
            }
          }
        }
        comments(last: 30) {
          nodes { author { login } createdAt body }
        }
        commits(last: 1) {
          nodes {
            commit {
              oid
              statusCheckRollup {
                state
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      status
                      conclusion
                      detailsUrl
                      completedAt
                    }
                    ... on StatusContext {
                      context
                      state
                      targetUrl
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- #
# GitHub access
# --------------------------------------------------------------------------- #
def resolve_token(explicit: str | None) -> str | None:
    """Return an API token from the CLI flag, the environment or `gh`."""
    if explicit:
        return explicit
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class GraphQLUnavailable(SystemExit):
    """GitHub refused the GraphQL endpoint itself (401/403, not a rate limit),
    as some proxied environments do; the REST API may still answer."""


def graphql(query: str, variables: dict[str, Any], token: str | None) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "User-Agent": "list-upstream-prs",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(GRAPHQL_URL, data=payload, headers=headers)
    # Long runs hit the odd dropped connection or TLS reset; a couple of
    # retries are cheaper than losing the whole report.
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode())
            break
        except urllib.error.HTTPError as exc:  # pragma: no cover - network failure
            if exc.code in (502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in (401, 403) and "rate limit" not in detail.lower():
                raise GraphQLUnavailable(f"GitHub API error {exc.code}: {detail}") from exc
            raise SystemExit(f"GitHub API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError) as exc:
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            reason = getattr(exc, "reason", exc)
            raise SystemExit(f"Cannot reach the GitHub API: {reason}") from exc
    if body.get("errors"):
        messages = "; ".join(e.get("message", str(e)) for e in body["errors"])
        raise SystemExit(f"GraphQL error: {messages}")
    return body["data"]


def rest(path: str, params: dict[str, Any] | None, token: str | None) -> Any:
    """GET a GitHub REST API path ('/repos/o/r/pulls/1/files') as JSON."""
    url = f"{REST_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "list-upstream-prs",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(RETRIES + 2):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:  # pragma: no cover - network failure
            detail = exc.read().decode(errors="replace")[:500]
            # The search API allows 30 requests a minute: wait for the window.
            limited = exc.code == 429 or (exc.code == 403 and "rate limit" in detail.lower())
            if limited and attempt < RETRIES + 1:
                reset = exc.headers.get("X-RateLimit-Reset") or ""
                wait = (int(reset) - time.time() + 1) if reset.isdigit() else 0
                time.sleep(min(max(wait, float(exc.headers.get("Retry-After") or 0), 5.0), 65.0))
                continue
            if exc.code in (502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            raise SystemExit(f"GitHub API error {exc.code} on {path}: {detail}") from exc
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError) as exc:
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            reason = getattr(exc, "reason", exc)
            raise SystemExit(f"Cannot reach the GitHub API: {reason}") from exc
    raise SystemExit(f"GitHub API rate limit on {path}")  # pragma: no cover


def search_prs_rest(query: str, token: str | None, limit: int = 1000,
                    comments: int = 25) -> list[dict[str, Any]]:
    """The PRs a search finds, shaped as the GraphQL PullRequest nodes the
    callers read: number title url body state mergedAt isDraft updatedAt
    author{login} files{nodes{path}} comments{nodes{author{login} createdAt body}}
    (the last `comments` of them). Two more requests per PR."""
    items: list[dict[str, Any]] = []
    page = 1
    while len(items) < limit:
        per_page = min(100, limit - len(items))
        data = rest("/search/issues", {"q": query, "per_page": per_page, "page": page}, token)
        batch = [i for i in data.get("items") or [] if i.get("pull_request")]
        items += batch
        if len(data.get("items") or []) < per_page or page * per_page >= 1000:
            break
        page += 1
    nodes = []
    for item in items[:limit]:
        repo = item["repository_url"].split("/repos/", 1)[1]
        number = item["number"]
        merged = (item.get("pull_request") or {}).get("merged_at")
        files, page = [], 1
        while page <= 30:  # GitHub lists at most 3000 files of a PR
            batch = rest(f"/repos/{repo}/pulls/{number}/files",
                         {"per_page": 100, "page": page}, token)
            files += [{"path": f["filename"]} for f in batch]
            if len(batch) < 100 or len(files) >= 100:
                break
            page += 1
        notes: list[dict[str, Any]] = []
        total = item.get("comments") or 0
        if total and comments:
            # The last `comments`: the last page, and the one before when it is short.
            last = -(-total // comments)
            for p in ([last - 1] if last > 1 and total % comments else []) + [last]:
                notes += rest(f"/repos/{repo}/issues/{number}/comments",
                              {"per_page": comments, "page": p}, token)
            notes = notes[-comments:]
        nodes.append({
            "number": number, "title": item.get("title") or "", "url": item.get("html_url") or "",
            "body": item.get("body") or "", "isDraft": bool(item.get("draft")),
            "updatedAt": item.get("updated_at") or "", "mergedAt": merged,
            "state": "MERGED" if merged else (item.get("state") or "").upper(),
            "author": {"login": (item.get("user") or {}).get("login", "")},
            "files": {"nodes": files[:100]},
            "comments": {"nodes": [{"author": {"login": (c.get("user") or {}).get("login", "")},
                                    "createdAt": c.get("created_at") or "",
                                    "body": c.get("body") or ""} for c in notes]},
        })
    return nodes


def fetch_pull_requests(
    upstream: str, author: str | None, token: str | None
) -> list[dict[str, Any]]:
    terms = [f"repo:{upstream}", "is:pr", "is:open"]
    if author:
        terms.append(f"author:{author}")
    search = " ".join(terms)

    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        data = graphql(QUERY, {"q": search, "after": cursor}, token)
        result = data["search"]
        nodes.extend(n for n in result["nodes"] if n)
        if not result["pageInfo"]["hasNextPage"]:
            break
        cursor = result["pageInfo"]["endCursor"]
    return nodes


# --------------------------------------------------------------------------- #
# Check / Yetus / review analysis
# --------------------------------------------------------------------------- #
PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILING_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}


@dataclass
class CheckGroup:
    """Aggregated result of one family of checks (Actions or Yetus/Jenkins)."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.pending)

    @property
    def verdict(self) -> str:
        if not self.total:
            return "none"
        if self.failed:
            return "fail"
        if self.pending:
            return "running"
        return "pass"


def classify_contexts(pr: dict[str, Any]) -> tuple[CheckGroup, CheckGroup]:
    """Split the head commit's checks into (GitHub Actions, Yetus/Jenkins)."""
    actions, yetus = CheckGroup(), CheckGroup()
    commits = (pr.get("commits") or {}).get("nodes") or []
    if not commits:
        return actions, yetus
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return actions, yetus

    seen: set[str] = set()
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "CheckRun":
            name = ctx.get("name") or "check"
            if ctx.get("status") != "COMPLETED":
                state = "pending"
            elif (ctx.get("conclusion") or "") in PASSING_CONCLUSIONS:
                state = "passed"
            elif (ctx.get("conclusion") or "") in FAILING_CONCLUSIONS:
                state = "failed"
            else:
                state = "pending"
        else:
            name = ctx.get("context") or "status"
            raw = (ctx.get("state") or "").upper()
            state = {
                "SUCCESS": "passed",
                "FAILURE": "failed",
                "ERROR": "failed",
                "PENDING": "pending",
                "EXPECTED": "pending",
            }.get(raw, "pending")

        # "Apache Yetus" (check run) and "Apache Yetus(jenkins)" (status) are
        # the same job reported twice; keep a single entry per normalised name.
        key = re.sub(r"\(jenkins\)$", "", name).strip().lower()
        if key in seen:
            continue
        seen.add(key)

        group = yetus if YETUS_RE.search(name) else actions
        getattr(group, state).append(name)
    return actions, yetus


@dataclass
class YetusReport:
    overall: str | None = None  # "+1" / "-1"
    failures: list[str] = field(default_factory=list)
    needs_rebase: bool = False
    posted_at: str | None = None


def parse_yetus_comment(pr: dict[str, Any], bots: Iterable[str]) -> YetusReport | None:
    """Parse the newest Apache Yetus precommit comment, if any."""
    bot_names = {b.lower() for b in bots}
    comments = (pr.get("comments") or {}).get("nodes") or []
    latest = None
    for comment in comments:
        login = ((comment.get("author") or {}).get("login") or "").lower()
        if login in bot_names and "overall" in (comment.get("body") or ""):
            latest = comment
    if latest is None:
        return None

    body = latest["body"]
    report = YetusReport(posted_at=latest.get("createdAt"))
    match = re.search(r"\*\*\s*([+-]\d+)\s+overall\*\*", body)
    if match:
        report.overall = match.group(1)

    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not cells[0].startswith("-1"):
            continue
        subsystem, note = cells[1], cells[-1]
        if subsystem:
            report.failures.append(subsystem)
        if "does not apply" in note or "Rebase required" in note:
            report.needs_rebase = True
    # Preserve order, drop duplicates.
    report.failures = list(dict.fromkeys(report.failures))
    return report


def summarise_reviews(pr: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return (approvers, change requesters, commenters) from the latest reviews."""
    approvers: list[str] = []
    requesters: list[str] = []
    commenters: list[str] = []
    for review in ((pr.get("latestReviews") or {}).get("nodes") or []):
        login = (review.get("author") or {}).get("login") or "?"
        state = review.get("state")
        if state == "APPROVED":
            approvers.append(login)
        elif state == "CHANGES_REQUESTED":
            requesters.append(login)
        elif state == "COMMENTED":
            commenters.append(login)
    return approvers, requesters, commenters


def requested_reviewers(pr: dict[str, Any]) -> list[str]:
    names = []
    for request in ((pr.get("reviewRequests") or {}).get("nodes") or []):
        reviewer = request.get("requestedReviewer") or {}
        names.append(reviewer.get("login") or reviewer.get("name") or "?")
    return names


def days_since(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    moment = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - moment).days


def join(names: list[str], limit: int = 3) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" (+{len(names) - limit})"


# --------------------------------------------------------------------------- #
# Status derivation
# --------------------------------------------------------------------------- #
@dataclass
class PullRequestRow:
    number: int
    title: str
    branch: str
    status: str
    comments: str
    url: str
    base: str
    head_repo: str
    author: str
    updated_at: str
    details: dict[str, Any]


def evaluate(pr: dict[str, Any], bots: Iterable[str], stale_days: int) -> PullRequestRow:
    actions, yetus_checks = classify_contexts(pr)
    yetus_report = parse_yetus_comment(pr, bots)
    approvers, requesters, commenters = summarise_reviews(pr)
    pending_reviewers = requested_reviewers(pr)
    mergeable = pr.get("mergeable")
    review_decision = pr.get("reviewDecision") or ""
    idle = days_since(pr.get("updatedAt"))

    notes: list[str] = []

    # --- continuous integration -------------------------------------------- #
    if actions.total:
        if actions.verdict == "pass":
            notes.append(f"CI Pass (GitHub Actions {len(actions.passed)}/{actions.total})")
        elif actions.verdict == "fail":
            notes.append(f"CI Not Pass (GitHub Actions: {join(actions.failed)})")
        else:
            notes.append(f"CI running (GitHub Actions: {join(actions.pending)})")
    else:
        notes.append("CI not reported")

    # --- Apache Yetus / Jenkins precommit ----------------------------------- #
    if yetus_checks.total or yetus_report:
        if yetus_report and yetus_report.overall == "-1":
            detail = join(yetus_report.failures) or "see report"
            notes.append(f"Yetus Not Pass (-1: {detail})")
        elif yetus_checks.verdict == "fail":
            notes.append(f"Yetus Not Pass ({join(yetus_checks.failed)})")
        elif yetus_checks.verdict == "running":
            notes.append("Yetus running")
        elif yetus_report and yetus_report.overall and yetus_report.overall.startswith("+"):
            notes.append(f"Yetus Pass ({yetus_report.overall} overall)")
        elif yetus_checks.verdict == "pass":
            notes.append("Yetus Pass")
        else:
            notes.append("Yetus result unknown")
        if yetus_report and yetus_report.needs_rebase:
            notes.append("patch no longer applies - rebase required")
    else:
        notes.append("Yetus not run yet")

    # --- merge conflicts ----------------------------------------------------- #
    if mergeable == "CONFLICTING":
        notes.append("merge conflicts with the base branch")
    elif mergeable == "UNKNOWN":
        notes.append("mergeability still being computed by GitHub")

    # --- review situation ---------------------------------------------------- #
    if requesters:
        notes.append(f"Not Approved - changes requested by {join(requesters)}")
    elif review_decision == "CHANGES_REQUESTED":
        pending = join(pending_reviewers)
        notes.append(
            "Not Approved - changes requested upstream"
            + (f", re-review pending from {pending}" if pending else "")
        )
    elif approvers:
        notes.append(f"Approved by {join(approvers)}")
    elif pending_reviewers:
        notes.append(f"Not Approved - review pending from {join(pending_reviewers)}")
    elif commenters:
        notes.append(f"Not Approved - only comments so far from {join(commenters)}")
    else:
        notes.append("Lacks Reviewers - nobody assigned or reviewing")

    if idle is not None and idle >= stale_days:
        notes.append(f"no activity for {idle} days")

    # --- single headline status --------------------------------------------- #
    if pr.get("isDraft"):
        status = "DRAFT"
    elif mergeable == "CONFLICTING":
        status = "CONFLICTS"
    elif requesters or review_decision == "CHANGES_REQUESTED":
        status = "CHANGES REQUESTED"
    elif yetus_report and yetus_report.overall == "-1":
        status = "YETUS FAILED"
    elif yetus_checks.verdict == "fail":
        status = "YETUS FAILED"
    elif actions.verdict == "fail":
        status = "CI FAILED"
    elif actions.verdict == "running" or yetus_checks.verdict == "running":
        status = "CI RUNNING"
    elif approvers or review_decision == "APPROVED":
        status = "READY TO MERGE"
    else:
        status = "WAITING FOR REVIEW"

    return PullRequestRow(
        number=pr["number"],
        title=pr["title"],
        branch=pr["headRefName"],
        status=status,
        comments="; ".join(notes),
        url=pr["url"],
        base=pr["baseRefName"],
        head_repo=(pr.get("headRepository") or {}).get("nameWithOwner") or "",
        author=(pr.get("author") or {}).get("login") or "",
        updated_at=pr.get("updatedAt") or "",
        details={
            "is_draft": bool(pr.get("isDraft")),
            "mergeable": mergeable,
            "review_decision": review_decision or None,
            "approvers": approvers,
            "changes_requested_by": requesters,
            "requested_reviewers": pending_reviewers,
            "days_since_update": idle,
            "actions": asdict(actions),
            "yetus_checks": asdict(yetus_checks),
            "yetus_report": asdict(yetus_report) if yetus_report else None,
        },
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_table(rows: list[PullRequestRow], width: int) -> str:
    headers = ("ID", "Title", "Branch", "Status", "Comments")
    id_w = max(len(headers[0]), max((len(str(r.number)) for r in rows), default=2))
    branch_w = min(28, max(len(headers[2]), max((len(r.branch) for r in rows), default=6)))
    status_w = max(len(headers[3]), max((len(r.status) for r in rows), default=6))
    fixed = id_w + branch_w + status_w + 4 * 3  # 3 spaces between 5 columns
    remaining = max(40, width - fixed)
    title_w = max(24, int(remaining * 0.42))
    comments_w = max(24, remaining - title_w)

    widths = (id_w, title_w, branch_w, status_w, comments_w)
    sep = "   "

    def emit(cells: tuple[str, ...]) -> list[str]:
        columns = [
            textwrap.wrap(text, w) or [""]
            for text, w in zip(cells, widths)
        ]
        height = max(len(c) for c in columns)
        lines = []
        for i in range(height):
            parts = [
                (col[i] if i < len(col) else "").ljust(w)
                for col, w in zip(columns, widths)
            ]
            lines.append(sep.join(parts).rstrip())
        return lines

    out = emit(headers)
    out.append(sep.join("-" * w for w in widths))
    for row in rows:
        out.extend(
            emit((str(row.number), row.title, row.branch, row.status, row.comments))
        )
    return "\n".join(out)


def render_markdown(rows: list[PullRequestRow]) -> str:
    lines = [
        "| ID | Title | Branch | Status | Comments |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        title = row.title.replace("|", "\\|")
        comments = row.comments.replace("|", "\\|")
        lines.append(
            f"| [#{row.number}]({row.url}) | {title} | `{row.branch}` | "
            f"{row.status} | {comments} |"
        )
    return "\n".join(lines)


def render_csv(rows: list[PullRequestRow], stream) -> None:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["ID", "Title", "Branch", "Status", "Comments", "URL"])
    for row in rows:
        writer.writerow(
            [row.number, row.title, row.branch, row.status, row.comments, row.url]
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM,
        help=f"upstream repository, owner/name (default: {DEFAULT_UPSTREAM})",
    )
    parser.add_argument(
        "--fork-owner",
        default=DEFAULT_FORK_OWNER,
        help=f"owner of the fork the branches live in (default: {DEFAULT_FORK_OWNER})",
    )
    parser.add_argument(
        "--author",
        default=None,
        help="PR author to search for (default: the fork owner)",
    )
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE_BRANCH,
        help=f"target branch upstream, '*' for any (default: {DEFAULT_BASE_BRANCH})",
    )
    parser.add_argument(
        "--include-drafts",
        action="store_true",
        help="also list draft pull requests",
    )
    parser.add_argument(
        "--any-head-repo",
        action="store_true",
        help="do not filter on the fork the branch comes from",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="flag PRs untouched for this many days (default: 14)",
    )
    parser.add_argument(
        "--bot",
        action="append",
        default=None,
        metavar="LOGIN",
        help=f"login of the precommit bot (default: {', '.join(DEFAULT_BOTS)})",
    )
    parser.add_argument(
        "--format",
        choices=("table", "markdown", "csv", "json"),
        default="table",
        help="output format (default: table)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="table width in columns (default: terminal width)",
    )
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # PR titles may carry characters the console codepage cannot encode.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args = parse_args(argv)
    author = args.author or args.fork_owner
    bots = args.bot or list(DEFAULT_BOTS)
    token = resolve_token(args.token)

    pulls = fetch_pull_requests(args.upstream, author, token)

    selected = []
    for pr in pulls:
        if args.base != "*" and pr.get("baseRefName") != args.base:
            continue
        if not args.any_head_repo:
            owner = (pr.get("headRepositoryOwner") or {}).get("login") or ""
            if owner.lower() != args.fork_owner.lower():
                continue
        if pr.get("isDraft") and not args.include_drafts:
            continue
        selected.append(pr)

    rows = [evaluate(pr, bots, args.stale_days) for pr in selected]
    rows.sort(key=lambda r: r.number, reverse=True)

    if args.format == "json":
        json.dump([asdict(r) for r in rows], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.format == "csv":
        render_csv(rows, sys.stdout)
        return 0

    if not rows:
        print(
            f"No open pull requests from {args.fork_owner}/* into "
            f"{args.upstream}:{args.base}."
        )
        return 0

    if args.format == "markdown":
        print(render_markdown(rows))
    else:
        width = args.width or shutil.get_terminal_size((120, 24)).columns
        print(
            f"Open pull requests from {args.fork_owner} into "
            f"{args.upstream}:{args.base} - {len(rows)} total\n"
        )
        print(render_table(rows, max(80, width)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
