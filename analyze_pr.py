#!/usr/bin/env python3
"""Analyse one Apache Hadoop contribution and say what it needs to get merged.

Given either a pull request number or an ASF JIRA id (HADOOP-19987,
MAPREDUCE-7546, YARN-11993, HDFS-17951, HDDS-..., OZONE-...), the script reads:

* the JIRA issue - summary, status, resolution, assignee, labels and the
  description, plus the most recent JIRA comments;
* the pull request - title, body, review state and every discussion comment;
* the checks - GitHub Actions and the Apache Yetus / Jenkins precommit report;
* the dependencies on other pull requests - branches stacked on top of each
  other, diffs that use a class, constant, maven property or configuration key
  another open PR introduces, a precommit -1 (a trunk spotbugs warning, a
  failing test) that another open PR fixes, wording such as 'depends on
  #8699' and JIRA 'is blocked by' links.

Declarations made by hand (PR text, JIRA links) are checked against the diffs
and the precommit history, so each dependency carries a verdict: CONFIRMED,
CI-FIX, LIKELY, WEAK, UNSUPPORTED or UNVERIFIED, plus DISCOVERED for the ones
the code or the CI shows and nobody declared, and STALE for a CI fix whose
failure is gone: not seen for STALE_DAYS days, with a green run since.

and prints

    JIRA-ID and title | PR ID and title | PR Status | PR Comments |
    Depends on | Suggestion

where *PR Comments* explains why that status was assigned, *Depends on* lists
the pull requests that must land first (and the ones waiting on this one) and
*Suggestion* is an ordered course of action towards the MERGED state.

The JIRA REST API is read anonymously; GitHub uses $GITHUB_TOKEN, $GH_TOKEN or
the ``gh`` CLI, exactly like the sibling scripts. ``list_upstream_prs.py`` must
sit in the same directory: its check/Yetus/review analysis is reused here.

Examples
--------
    python analyze_pr.py 8757
    python analyze_pr.py HADOOP-19987
    python analyze_pr.py MAPREDUCE-7546 --format json
    python analyze_pr.py 8717 8725 8754 --format markdown
"""

from __future__ import annotations

import argparse
import csv
import datetime
import html as html_lib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from list_upstream_prs import (
        DEFAULT_BOTS,
        classify_contexts,
        days_since,
        evaluate,
        graphql,
        join,
        parse_yetus_comment,
        requested_reviewers,
        resolve_token,
        summarise_reviews,
        RETRIES,
        RETRY_WAIT,
    )
except ImportError:  # pragma: no cover - misplaced file
    raise SystemExit(
        "list_upstream_prs.py must sit next to this script "
        "(it provides the GitHub and check-analysis helpers)."
    )

DEFAULT_REPO = "apache/hadoop"
DEFAULT_AUTHOR = "joseluisll"
DEFAULT_JIRA = "https://issues.apache.org/jira"
DEFAULT_REPO_PATH = r"C:\dev\hadoop"

JIRA_ID_RE = re.compile(
    r"^(HADOOP|HDFS|YARN|MAPREDUCE|HDDS|OZONE|SUBMARINE)-(\d+)$", re.I
)
JIRA_IN_TEXT_RE = re.compile(
    r"\b(HADOOP|HDFS|YARN|MAPREDUCE|HDDS|OZONE|SUBMARINE)-(\d+)\b", re.I
)

JIRA_FIELDS = (
    "summary,status,resolution,assignee,reporter,priority,issuetype,components,"
    "labels,fixVersions,description,created,updated,comment,issuelinks,parent"
)

# JIRA link labels, read from the point of view of the issue being analysed.
JIRA_BLOCKING_LABELS = ("is blocked by", "depends upon", "depends on", "is dependent")
JIRA_BLOCKED_LABELS = ("blocks", "is depended upon by", "is required by", "is a prerequisite")

# Wording that turns a bare reference into a real 'merge that one first'.
DEP_PHRASE_RE = re.compile(
    r"(depends?\s+(?:on|upon)|depending\s+on|blocked\s+by|blocker\s*:|"
    r"requires?\b|needs?\b|based\s+(?:on|upon)|builds?\s+(?:on|upon)|"
    r"stacked\s+(?:on|upon)|on\s+top\s+of|prerequisite|pre-?requisite|"
    r"after\s+(?:merging|#)|once\s+\S+\s+is\s+merged|"
    r"should\s+(?:be\s+)?(?:merged|committed)\s+after|"
    r"merged?\s+(?:\w+\s+)?first|"
    r"(?:fix(?:es|ed)?|clears?|resolves?)\s+(?:the\s+|this\s+)?"
    r"(?:precommit|yetus|ci\b|build|spotbugs|findbugs|javadoc|checkstyle|[-−]1))",
    re.I,
)
# A list introduced by 'merge first:' carries its references on the next lines.
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
REF_IN_TEXT_RE = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)"
    r"|(?<![\w/])#(\d+)\b"
    r"|\b(?:HADOOP|HDFS|YARN|MAPREDUCE|HDDS|OZONE|SUBMARINE)-\d+\b",
    re.I,
)
# 'nothing in HADOOP-19972 depends on it' is not a dependency.
NEGATION_RE = re.compile(
    r"\b(?:no|not|nothing|none|never|neither|nor|without|n't|does\s+not|do\s+not|"
    r"doesn't|don't|no\s+longer|independent(?:ly)?\s+of|unrelated)\b",
    re.I,
)

# What a -1 from a given Yetus subsystem means in practice.
YETUS_ADVICE = {
    "patch": "the patch no longer applies: rebase onto the latest trunk and force-push",
    "mvninstall": "the build fails: reproduce with 'mvn -DskipTests install' on the touched modules",
    "compile": "compilation fails on one of the supported JDKs: check both JDK 17 and JDK 21",
    "javac": "new javac warnings were introduced: remove them or justify each one",
    "checkstyle": "fix the checkstyle issues listed in the Yetus report (line length, imports, spacing)",
    "spotbugs": "fix the SpotBugs warning, or add a justified exclusion in the module's spotbugs file",
    "javadoc": "fix the javadoc warnings (malformed tags, missing @param/@return)",
    "unit": "inspect the failing tests in the Yetus report; fix the real ones and, for known flakies, say so in a PR comment and push a rebase so the job runs again",
    "test4tests": "add a unit test, or explain in the PR description why no new test is possible",
    "asflicense": "add the ASF license header to the new files",
    "blanks": "remove the trailing whitespace / blank-line issues the report lists",
    "whitespace": "remove the trailing whitespace the report lists",
    "shadedclient": "the shaded client build broke: check dependency changes in hadoop-client-modules",
    "xmllint": "fix the malformed XML the report points at",
    "pylint": "fix the python lint issues listed in the report",
    "shellcheck": "fix the shellcheck issues listed in the report",
    "hadolint": "fix the Dockerfile lint issues listed in the report",
}

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title url body state isDraft
      createdAt updatedAt mergedAt closedAt
      baseRefName headRefName
      mergeable
      additions deletions changedFiles
      author { login ... on User { name } }
      mergedBy { login }
      headRepositoryOwner { login }
      headRepository { nameWithOwner }
      reviewDecision
      latestReviews(first: 20) { nodes { author { login } state submittedAt body } }
      reviewRequests(first: 20) {
        nodes { requestedReviewer { __typename ... on User { login } ... on Team { name } } }
      }
      comments(last: 40) { nodes { author { login } createdAt body url } }
      reviewThreads(first: 50) {
        nodes {
          isResolved isOutdated
          comments(first: 1) { nodes { author { login } body path } }
        }
      }
      files(first: 100) { nodes { path } }
      commits(last: 1) {
        nodes {
          commit {
            oid committedDate
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun { name status conclusion detailsUrl completedAt }
                  ... on StatusContext { context state targetUrl createdAt }
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

SEARCH_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 30) {
    nodes {
      ... on PullRequest {
        number title state isDraft updatedAt mergedAt
      }
    }
  }
}
"""

# The other open PRs of the same author, used to spot stacked branches.
PEERS_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        number title url body state isDraft updatedAt author { login }
        baseRefName headRefName headRefOid
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""

MINI_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title url body state isDraft mergedAt headRefName headRefOid author { login }
      files(first: 100) { nodes { path } }
    }
  }
}
"""

# Declarations that another change can depend on, read from added diff lines.
SYMBOL_PATTERNS = (
    re.compile(r"\b(?:class|interface|enum|record)\s+([A-Z]\w{3,})"),
    re.compile(r"^\s*(?:public|protected)\s+(?:static\s+)?(?:final\s+)?"
               r"(?:synchronized\s+)?[\w<>\[\],.?\s]+?\s(\w{4,})\s*\("),
    re.compile(r"\bstatic\s+final\s+[\w<>\[\].]+\s+([A-Z][A-Z0-9_]{4,})\s*="),
    re.compile(r"<([a-z][\w.-]*?\.(?:version|artifactId))>"),
    re.compile(r'"([a-z][\w-]*(?:\.[\w-]+){2,})"'),
)
# Identifiers too common to prove anything.
SYMBOL_STOPWORDS = {
    "getinstance", "tostring", "hashcode", "equals", "builder", "create", "close",
    "value", "getname", "setname", "start", "stop", "getconf", "setconf", "run",
    "test", "setup", "teardown", "initialize", "main", "apache", "hadoop",
}


# --------------------------------------------------------------------------- #
# JIRA
# --------------------------------------------------------------------------- #
def jira_get(base: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    url = f"{base.rstrip('/')}/rest/api/2/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "analyze-pr"}
    )
    # ASF JIRA drops connections under a burst of requests, and a TLS reset
    # must not cost the whole report: retry, then carry on without the issue.
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return None
            if exc.code in (429, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            print(f"warning: JIRA error {exc.code} for {path}", file=sys.stderr)
            return None
        except (urllib.error.URLError, ssl.SSLError, ConnectionError,
                TimeoutError, json.JSONDecodeError) as exc:
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            print(f"warning: cannot reach JIRA for {path}: "
                  f"{getattr(exc, 'reason', exc)}", file=sys.stderr)
            return None
    return None


@dataclass
class Jira:
    key: str
    title: str = ""
    status: str = ""
    resolution: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    priority: str | None = None
    issue_type: str | None = None
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    description: str = ""
    updated: str | None = None
    last_comments: list[dict[str, str]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    parent: str | None = None
    found: bool = False


def fetch_jira(base: str, key: str) -> Jira:
    data = jira_get(base, f"issue/{key}", {"fields": JIRA_FIELDS})
    if not data:
        return Jira(key=key)
    f = data.get("fields") or {}
    comments = ((f.get("comment") or {}).get("comments") or [])[-5:]
    links = []
    for link in f.get("issuelinks") or []:
        kind = link.get("type") or {}
        for direction, issue_field in (("inward", "inwardIssue"), ("outward", "outwardIssue")):
            issue = link.get(issue_field)
            if not issue:
                continue
            fields = issue.get("fields") or {}
            links.append(
                {
                    "id": link.get("id", ""),
                    "type": (kind.get("name") or ""),
                    "direction": direction,
                    "label": (kind.get(direction) or "").lower(),
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary") or "",
                    "status": ((fields.get("status") or {}) or {}).get("name", ""),
                    "resolution": ((fields.get("resolution") or {}) or {}).get("name", "") or "",
                }
            )
    return Jira(
        key=data.get("key", key),
        title=f.get("summary") or "",
        status=((f.get("status") or {}).get("name")) or "",
        resolution=((f.get("resolution") or {}) or {}).get("name"),
        assignee=((f.get("assignee") or {}) or {}).get("displayName"),
        reporter=((f.get("reporter") or {}) or {}).get("displayName"),
        priority=((f.get("priority") or {}) or {}).get("name"),
        issue_type=((f.get("issuetype") or {}) or {}).get("name"),
        components=[c.get("name", "") for c in (f.get("components") or [])],
        labels=list(f.get("labels") or []),
        fix_versions=[v.get("name", "") for v in (f.get("fixVersions") or [])],
        description=f.get("description") or "",
        updated=f.get("updated"),
        last_comments=[
            {
                "author": ((c.get("author") or {}) or {}).get("displayName", "?"),
                "created": c.get("created", "")[:10],
                "body": (c.get("body") or "").strip(),
            }
            for c in comments
        ],
        links=links,
        parent=((f.get("parent") or {}) or {}).get("key"),
        found=True,
    )


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def fetch_pr(repo: str, number: int, token: str | None) -> dict[str, Any] | None:
    owner, _, name = repo.partition("/")
    data = graphql(PR_QUERY, {"owner": owner, "name": name, "number": number}, token)
    return ((data.get("repository") or {}) or {}).get("pullRequest")


def find_pr_for_jira(repo: str, key: str, token: str | None) -> tuple[int | None, list[dict]]:
    """Return (chosen PR number, all candidates) for a JIRA id."""
    query = f'repo:{repo} is:pr in:title "{key}"'
    data = graphql(SEARCH_QUERY, {"q": query}, token)
    nodes = [n for n in (data["search"]["nodes"] or []) if n]
    exact = [n for n in nodes if JIRA_IN_TEXT_RE.match(n["title"].strip() or "")
             and (JIRA_IN_TEXT_RE.match(n["title"].strip()).group(0).upper() == key.upper())]
    candidates = exact or nodes
    if not candidates:
        return None, []
    open_prs = [n for n in candidates if n["state"] == "OPEN"]
    merged = [n for n in candidates if n["state"] == "MERGED"]
    pool = open_prs or merged or candidates
    pool.sort(key=lambda n: n.get("updatedAt") or "", reverse=True)
    return pool[0]["number"], candidates


_MINI_CACHE: dict[tuple[str, int], dict[str, Any] | None] = {}
_PEER_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_JIRA_PR_CACHE: dict[tuple[str, str], int | None] = {}


def fetch_pr_summary(repo: str, number: int, token: str | None) -> dict[str, Any] | None:
    """Cheap lookup of another pull request (state, title, head commit)."""
    key = (repo, number)
    if key not in _MINI_CACHE:
        owner, _, name = repo.partition("/")
        try:
            data = graphql(MINI_PR_QUERY, {"owner": owner, "name": name, "number": number}, token)
            _MINI_CACHE[key] = ((data.get("repository") or {}) or {}).get("pullRequest")
        except SystemExit:  # a reference to a PR that does not exist here
            _MINI_CACHE[key] = None
    return _MINI_CACHE[key]


def fetch_peer_prs(repo: str, author: str, token: str | None) -> list[dict[str, Any]]:
    """Every other open PR of the same author, with its files and head commit."""
    key = (repo, author.lower())
    if key not in _PEER_CACHE:
        query = f"repo:{repo} is:pr is:open author:{author}"
        data = graphql(PEERS_QUERY, {"q": query}, token)
        _PEER_CACHE[key] = [n for n in (data["search"]["nodes"] or []) if n]
    return _PEER_CACHE[key]


def pr_for_jira_cached(repo: str, key: str, token: str | None) -> int | None:
    cache_key = (repo, key.upper())
    if cache_key not in _JIRA_PR_CACHE:
        number, _ = find_pr_for_jira(repo, key, token)
        _JIRA_PR_CACHE[cache_key] = number
    return _JIRA_PR_CACHE[cache_key]


# --------------------------------------------------------------------------- #
# The diff of a pull request, and what it declares
# --------------------------------------------------------------------------- #
_DIFF_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def fetch_diff(repo: str, number: int, token: str | None, max_bytes: int = 4_000_000) -> str:
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "analyze-pr",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read(max_bytes).decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError,
                ConnectionError, TimeoutError):
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            return ""  # the dependency is then reported as UNVERIFIED
    return ""


def parse_diff(text: str) -> dict[str, dict[str, Any]]:
    """path -> {'added': [(line, text)], 'removed': [...], 'ranges': [(from, to)],
    'trunk': [(from, to)]}: 'ranges' in the new file, 'trunk' in the old one."""
    files: dict[str, dict[str, Any]] = {}
    path = None
    new_line = 0
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            files.setdefault(path, {"added": [], "removed": [], "ranges": [], "trunk": []})
            continue
        if line.startswith("@@") and path:
            hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if hunk:
                old, old_length = int(hunk.group(1)), int(hunk.group(2) or 1)
                start = int(hunk.group(3))
                length = int(hunk.group(4) or 1)
                new_line = start
                files[path]["ranges"].append((start, start + max(length, 1) - 1))
                files[path]["trunk"].append((old, old + max(old_length, 1) - 1))
            continue
        if path is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            files[path]["added"].append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            files[path]["removed"].append(line[1:])
        elif line.startswith(" "):
            new_line += 1
    return files


def pr_diff(repo: str, number: int, token: str | None) -> dict[str, dict[str, Any]]:
    key = (repo, number)
    if key not in _DIFF_CACHE:
        _DIFF_CACHE[key] = parse_diff(fetch_diff(repo, number, token))
    return _DIFF_CACHE[key]


def declared_symbols(diff: dict[str, dict[str, Any]], limit: int = 60) -> dict[str, str]:
    """Identifiers a diff introduces -> the file that introduces them."""
    symbols: dict[str, str] = {}
    for path, data in diff.items():
        if "/test/" in path or path.endswith("Test.java"):
            continue
        for _, text in data["added"]:
            for pattern in SYMBOL_PATTERNS:
                for match in pattern.finditer(text):
                    name = match.group(1)
                    if len(name) < 5 or name.lower() in SYMBOL_STOPWORDS:
                        continue
                    # Something that is only moved around is not a new symbol.
                    if any(name in old for old in data["removed"]):
                        continue
                    symbols.setdefault(name, path)
                    if len(symbols) >= limit:
                        return symbols
    return symbols


def uses_symbols(diff: dict[str, dict[str, Any]], symbols: dict[str, str],
                 limit: int = 5) -> list[tuple[str, str]]:
    """(symbol, file) for every introduced symbol this diff starts using."""
    hits: list[tuple[str, str]] = []
    for path, data in diff.items():
        blob = "\n".join(text for _, text in data["added"])
        if not blob:
            continue
        for name in symbols:
            if re.search(rf"\b{re.escape(name)}\b", blob) and (name, path) not in hits:
                hits.append((name, path))
                if len(hits) >= limit:
                    return hits
    return hits


def overlapping_hunks(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]],
                      slack: int = 3, limit: int = 4) -> list[str]:
    """Files where both diffs edit the same lines - a certain rebase conflict."""
    clashes: list[str] = []
    for path in set(a) & set(b):
        for a_from, a_to in a[path]["ranges"]:
            for b_from, b_to in b[path]["ranges"]:
                if a_from - slack <= b_to and b_from - slack <= a_to:
                    clashes.append(f"{path}:{max(a_from, b_from)}")
                    break
            else:
                continue
            break
        if len(clashes) >= limit:
            break
    return clashes


def symbol_in_base(repo_path: str | None, base_ref: str | None, symbol: str,
                   paths: list[str]) -> bool:
    """True when the identifier already exists on trunk, so it proves nothing."""
    if not repo_path or not base_ref or not paths:
        return False
    code, _ = git_run(repo_path, "grep", "-F", "-q", "-e", symbol, base_ref, "--", *paths[:20])
    return code == 0


def resolve_base_ref(repo_path: str | None) -> str | None:
    if not repo_path:
        return None
    for ref in ("upstream/trunk", "origin/trunk", "trunk"):
        if git_run(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0:
            return ref
    return None


def code_evidence(
    repo: str, token: str | None, self_diff: dict[str, dict[str, Any]],
    other_number: int, repo_path: str | None, base_ref: str | None,
    self_paths: list[str], other_paths: list[str],
) -> dict[str, Any]:
    """Compare two diffs: who uses what the other one introduces, and clashes."""
    other_diff = pr_diff(repo, other_number, token)
    if not other_diff or not self_diff:
        return {"available": False, "uses": [], "provides": [], "clashes": [],
                "shared_files": sorted(set(self_paths) & set(other_paths))[:5]}

    uses = [
        (name, path) for name, path in uses_symbols(self_diff, declared_symbols(other_diff))
        if not symbol_in_base(repo_path, base_ref, name, self_paths)
    ]
    provides = [
        (name, path) for name, path in uses_symbols(other_diff, declared_symbols(self_diff))
        if not symbol_in_base(repo_path, base_ref, name, other_paths)
    ]
    return {
        "available": True,
        "uses": uses,
        "provides": provides,
        "clashes": overlapping_hunks(self_diff, other_diff),
        "shared_files": sorted(set(self_diff) & set(other_diff))[:5],
    }


# --------------------------------------------------------------------------- #
# Precommit failures another pull request can clear
# --------------------------------------------------------------------------- #
# A PR whose precommit is red because of trunk (an 'extant' spotbugs warning,
# a flaky test it never touches) cannot go green on its own: it waits for the
# PR that fixes that. Those relations share no code, so the diffs cannot see
# them; the Yetus history can.
CI_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title headRefName
      headRepository { nameWithOwner }
      files(first: 100) { nodes { path } }
      comments(last: 100) { nodes { author { login } createdAt body } }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name conclusion
                    annotations(first: 30) { nodes { path message } }
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

YETUS_ROW_RE = re.compile(r"^\|\s*-1\b")
YETUS_MODULE_RE = re.compile(r"^\s*([\w.-]+(?:/[\w.-]+)*)\s+in\s+(?:trunk|the\s+patch)\b")
YETUS_TESTS_RE = re.compile(r"^\|\s*(Failed junit tests|Timed out junit tests|"
                            r"Failed junit5 tests)?\s*\|\s*([\w.$]+)\s*\|\s*$")
CHECK_WORDS = {
    "spotbugs": ("spotbugs", "findbugs", "se_", "np_", "dm_", "is2_", "bad_field"),
    "javadoc": ("javadoc",),
    "javac": ("javac", "compil", "deprecat", "warning"),
    "checkstyle": ("checkstyle",),
}
CI_REPORTS_PER_PR = 3
FOCUSED_FILES = 12
FIX_WORDS_RE = re.compile(r"flak|deflake|intermittent|\brace\b|re-?enable|stabili[sz]|"
                          r"fix\w*\s+(?:\w+\s+){0,3}tests?\b|spotbugs|findbugs|javadoc|"
                          r"checkstyle|warning", re.I)

# What a Maven log says failed: a test class, or a plugin goal on a module.
# Surefire's own goal failure only repeats the tests, so it is left out.
TEST_FAILURE_RE = re.compile(r"<<< (?:FAILURE|ERROR)! -- in ([\w.$]+)")
# One failed test method, and the test sources its stack trace runs through:
# a test can fail in code it inherits ('TestFederationWebApp' fails inside
# 'TestRouterWebServicesREST.java'), so its own file name is not enough.
METHOD_FAILURE_RE = re.compile(r"\[ERROR\] ([\w.$]+)\.\w+ -- Time elapsed.*<<< (?:FAILURE|ERROR)!")
TEST_FRAME_RE = re.compile(r"^\s*at [\w.$<>]+\(((?:Test\w*|\w+Test)\.java:\d+)\)")
GOAL_FAILURE_RE = re.compile(r"Failed to execute goal ([\w.-]+):([\w.-]+):[\w.-]+:[\w-]+ "
                             r"\([\w.-]+\) on project ([\w.-]+)")
TEST_PLUGINS = ("maven-surefire-plugin", "maven-failsafe-plugin")
ACTIONS_FAILED_RUNS = 6
CACHE_DIR = os.path.join(tempfile.gettempdir(), "analyze_pr_cache")

_CI_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
# (head repo, branch) -> the completed Actions runs: when, workflow, and
# 'clean' when the run is green or its failures were all read.
_ACTIONS_RUNS: dict[tuple[str, str], list[dict[str, Any]]] = {}
# A Yetus table row, whatever the vote: '| +1 :green_heart: | unit | ...'.
YETUS_ANY_ROW_RE = re.compile(r"^\|\s*[+-]?\d\b")
# A CI fix whose failure has not been seen for this long, with at least
# one green run of the same check since, is STALE.
STALE_DAYS = 30
_REPORT_CACHE: dict[str, dict[str, list[str]]] = {}


def http_get(url: str, token: str | None = None, limit: int = 80_000_000,
             accept: str | None = None) -> bytes | None:
    """GET with retries; None once the resource is gone (Jenkins prunes old builds)."""
    headers = {"User-Agent": "analyze-pr"}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    if token:
        # Actions logs redirect to a signed storage URL that must not get it.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read(limit)
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            return None
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError):
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            return None
    return None


def rest_json(path: str, token: str | None) -> dict[str, Any]:
    data = http_get(f"https://api.github.com/{path}", token,
                    accept="application/vnd.github+json")
    try:
        return json.loads(data) if data else {}
    except ValueError:
        return {}


def disk_cached(name: str, compute) -> Any:
    """A JSON value kept on disk: a finished build log never changes."""
    path = os.path.join(CACHE_DIR, re.sub(r"[^\w.-]+", "_", name)[-150:] + ".json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        pass
    value = compute()
    if value is not None:  # a log that could not be read is tried again next time
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
        except OSError:
            pass
    return value


def log_failures(text: str) -> dict[str, Any]:
    """Failed test classes, the test lines their stack traces run through
    ('TestRouterWebServicesREST.java:712'), and failed non-test plugin goals
    in a Maven log."""
    def name(test: str) -> str:
        return re.sub(r"^org\.apache\.", "", test.split("$")[0])
    tests = {name(t) for t in TEST_FAILURE_RE.findall(text)}
    frames: dict[str, set[str]] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        method = METHOD_FAILURE_RE.search(line)
        if not method:
            continue
        files = frames.setdefault(name(method.group(1)), set())
        for after in lines[i + 1: i + 80]:
            if "[ERROR]" in after or "[INFO]" in after or "[WARNING]" in after:
                break
            if frame := TEST_FRAME_RE.match(after):
                files.add(frame.group(1))
    goals = {(plugin, project) for _, plugin, project in GOAL_FAILURE_RE.findall(text)
             if plugin not in TEST_PLUGINS}
    return {"tests": sorted(tests | set(frames)),
            "frames": {test: sorted(files) for test, files in sorted(frames.items())},
            "goals": [list(g) for g in sorted(goals)]}


def build_log_failures(url: str, token: str | None = None) -> dict[str, Any] | None:
    def compute() -> dict[str, Any] | None:
        data = http_get(url, token)
        return log_failures(data.decode("utf-8", "replace")) if data else None
    return disk_cached(f"log-{url}-frames2", compute)  # the end of the name is the key


def actions_failures(head_repo: str, branch: str, token: str | None) -> list[dict[str, Any]]:
    """What failed in the latest failed GitHub Actions runs of a branch.

    A fork runs the Build workflow on every push, so its history shows the
    tests that went red on this branch even when the last run is green.
    """
    if not token:
        return []  # job logs need a token, even on a public repository
    listing = rest_json(f"repos/{head_repo}/actions/runs?branch="
                        f"{urllib.parse.quote(branch)}&per_page=50", token)
    runs = [r for r in (listing.get("workflow_runs") or []) if r.get("status") == "completed"]
    newest: dict[str, int] = {}
    for run in runs:  # newest first
        newest.setdefault(run.get("name") or "", run["id"])
    results = []
    read: set[int] = set()
    for run in [r for r in runs if r.get("conclusion") == "failure"][:ACTIONS_FAILED_RUNS]:
        jobs = rest_json(f"repos/{head_repo}/actions/runs/{run['id']}/jobs?per_page=100",
                         token).get("jobs") or []
        complete = bool(jobs)
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            found = build_log_failures(
                f"https://api.github.com/repos/{head_repo}/actions/jobs/{job['id']}/logs", token)
            complete = complete and found is not None
            if found:
                results.append({"when": run.get("created_at") or "", "job": job.get("name") or "",
                                "current": newest.get(run.get("name") or "") == run["id"],
                                "workflow": run.get("name") or "", **found})
        if complete:
            read.add(run["id"])
    _ACTIONS_RUNS[(head_repo, branch)] = [
        {"when": r.get("created_at") or "", "workflow": r.get("name") or "",
         "clean": r.get("conclusion") == "success" or r["id"] in read}
        for r in runs
    ]
    return results


def spotbugs_report(url: str) -> dict[str, list[str]]:
    """class -> bug types, as named by a Jenkins spotbugs report still kept."""
    if url in _REPORT_CACHE:
        return _REPORT_CACHE[url]
    classes: dict[str, list[str]] = {}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "analyze-pr"})
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read(2_000_000).decode("utf-8", "replace")
        flat = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).split())
        # Each warning reads 'Bug type X (click for details) In class Y ...'.
        for kind, cls in re.findall(r"Bug type ([A-Z0-9_]+).{0,80}?In class ([\w.$]+)", flat):
            types = classes.setdefault(cls, [])
            if kind not in types:
                types.append(kind)
    except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError, ValueError):
        pass  # Jenkins only keeps the last builds; the module still tells a lot
    _REPORT_CACHE[url] = classes
    return classes


def parse_ci_failures(pr: dict[str, Any], token: str | None = None) -> list[dict[str, Any]]:
    """Every CI failure of a PR that some other change could clear.

    Kept: warnings Yetus attributes to trunk ('... in trunk has 1 extant
    spotbugs warnings', any '/branch-' report), tests that failed although the
    PR does not touch them (nor the test files their stack traces run
    through, read from the latest Jenkins unit logs), plugin goals that
    failed on a module the PR does
    not touch (read from the newest Jenkins unit log), the tests that failed in
    the GitHub Actions runs of the head branch, and check annotations on files
    the PR does not change. Dropped: what the patch itself introduced, and
    'does not apply' (that is a rebase, not a dependency).
    """
    own = {f["path"] for f in ((pr.get("files") or {}).get("nodes") or [])}
    own_names = {os.path.basename(p) for p in own}
    bots = {b.lower() for b in DEFAULT_BOTS}
    found: dict[tuple[str, str], dict[str, Any]] = {}

    def record(key: tuple[str, str], when: str, current: bool, place: str, **data: Any) -> None:
        entry = found.get(key)
        if entry is None:
            entry = found[key] = {"first_seen": when, "last_seen": when, "runs": 0,
                                  "current": False, "seen_in": [], **data}
        entry["runs"] += 1
        entry["current"] = entry["current"] or current
        if place not in entry["seen_in"]:
            entry["seen_in"].append(place)
        if when:
            entry["first_seen"] = min(entry["first_seen"] or when, when)
        if data.get("report") and when >= entry["last_seen"]:
            entry["report"] = data["report"]
        entry["last_seen"] = max(entry["last_seen"], when)

    def note_workflow(key: tuple[str, str], workflow: str) -> None:
        workflows = found[key].setdefault("workflows", [])
        if workflow and workflow not in workflows:
            workflows.append(workflow)

    def touched(project: str) -> bool:
        return any(f"/{project}/" in f"/{p}" for p in own)

    yetus = [c for c in ((pr.get("comments") or {}).get("nodes") or [])
             if (((c.get("author") or {}) or {}).get("login") or "").lower() in bots
             and "overall" in (c.get("body") or "")]
    newest = max((c.get("createdAt") or "" for c in yetus), default="")
    unit_logs: list[tuple[str, str]] = []   # (Yetus comment time, its unit log)
    table_tests: dict[str, set[str]] = {}
    for comment in yetus:
        when = comment.get("createdAt") or ""
        current = when == newest
        in_tests = False
        for line in (comment.get("body") or "").splitlines():
            tests = YETUS_TESTS_RE.match(line)
            if tests and (tests.group(1) or in_tests):
                in_tests = True
                test = tests.group(2)
                table_tests.setdefault(when, set()).add(test)
                if f"{test.rsplit('.', 1)[-1]}.java" not in own_names:
                    record(("unit", test), when, current, "precommit", subsystem="unit",
                           test=test, detail=f"{test.rsplit('.', 1)[-1]} failed")
                continue
            in_tests = False
            if not YETUS_ROW_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            subsystem, note = cells[1].lower(), cells[-1]
            report = re.search(r"\((https?://[^)\s]+)\)", cells[3])
            report_url = report.group(1) if report else ""
            if subsystem == "unit" and report_url:
                unit_logs.append((when, report_url))
            if subsystem in ("patch", "unit") or "does not apply" in note:
                continue  # unit failures are recorded test by test above
            from_trunk = "/branch-" in report_url or " in trunk" in note
            if not from_trunk:
                continue  # introduced by this patch: the PR has to fix it itself
            module = YETUS_MODULE_RE.match(note)
            module_path = module.group(1) if module else ""
            if module_path == "root":
                module_path = ""  # 'root has 94 extant warnings' points nowhere
            record((subsystem, module_path or note), when, current, "precommit",
                   subsystem=subsystem, module=module_path, report=report_url,
                   detail=f"{subsystem} on {module_path or 'the build'} (trunk)")

    # The unit logs of the latest runs: the test files each failure's stack
    # trace runs through, and failed tests the Yetus table left out. A log
    # Jenkins no longer keeps leaves the table as it is.
    own_failures = set()
    for when, url in sorted(set(unit_logs), reverse=True)[:CI_REPORTS_PER_PR]:
        log = build_log_failures(url) or {}
        for test in log.get("tests", []):
            frames = log.get("frames", {}).get(test, [])
            simple = test.rsplit(".", 1)[-1]
            if own_names & {f"{simple}.java", *(f.split(":")[0] for f in frames)}:
                own_failures.add(("unit", test))  # it fails in code the PR edits
                continue
            if test not in table_tests.get(when, ()):
                record(("unit", test), when, when == newest, "precommit", subsystem="unit",
                       test=test, detail=f"{simple} failed")
            entry = found[("unit", test)]
            entry["frames"] = sorted(set(entry.get("frames", [])) | set(frames))
        # 'root in the patch failed' can hide a plugin that broke on some module
        # (a Jasmine run, an enforcer rule): the newest unit log names it.
        for plugin, project in log.get("goals", []) if when == newest else []:
            if not touched(project):
                record(("build", project), newest, True, "precommit", subsystem="build",
                       project=project, plugin=plugin, detail=f"{plugin} fails on {project}")
    for key in own_failures:
        found.pop(key, None)

    # The GitHub Actions history of the head branch.
    head_repo = ((pr.get("headRepository") or {}) or {}).get("nameWithOwner") or ""
    branch = pr.get("headRefName") or ""
    if head_repo and branch:
        for run in actions_failures(head_repo, branch, token):
            for test in run.get("tests", []):
                simple = test.rsplit(".", 1)[-1]
                if f"{simple}.java" not in own_names:
                    record(("unit", test), run["when"], run["current"], "GitHub Actions",
                           subsystem="unit", test=test,
                           detail=f"{simple} failed ({run['job']})")
                    note_workflow(("unit", test), run.get("workflow", ""))
            for plugin, project in run.get("goals", []):
                if not touched(project):
                    record(("build", project), run["when"], run["current"], "GitHub Actions",
                           subsystem="build", project=project, plugin=plugin,
                           detail=f"{plugin} fails on {project}")
                    note_workflow(("build", project), run.get("workflow", ""))

    # GitHub checks: an annotation on a file this PR does not change.
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
    for context in ((rollup.get("contexts") or {}).get("nodes") or []):
        if context.get("__typename") != "CheckRun":
            continue
        if (context.get("conclusion") or "") not in ("FAILURE", "TIMED_OUT"):
            continue
        for note in ((context.get("annotations") or {}).get("nodes") or []):
            path = note.get("path") or ""
            if path and path not in own and not path.startswith("."):
                record(("check", path), "", True, "GitHub checks",
                       subsystem=context.get("name") or "check",
                       files=[path], detail=f"{context.get('name')} fails on {path}")

    # Green runs since the last sighting: runs of the same check, newer than
    # it, that did not report it. They tell a failure that is gone from one
    # nobody ran again.
    yetus_runs = []
    for comment in yetus:
        ran = set()
        for line in (comment.get("body") or "").splitlines():
            if YETUS_ANY_ROW_RE.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) >= 2:
                    ran.add(cells[1].lower())
        yetus_runs.append((comment.get("createdAt") or "", ran))
    actions_runs = _ACTIONS_RUNS.get((head_repo, branch), []) if head_repo and branch else []
    for failure in found.values():
        seen, green = failure["last_seen"], 0
        if seen and "precommit" in failure["seen_in"]:
            check = "unit" if failure["subsystem"] in ("unit", "build") else failure["subsystem"]
            green += sum(1 for when, ran in yetus_runs if when > seen and check in ran)
        if seen and failure.get("workflows"):
            green += sum(1 for r in actions_runs if r["when"] > seen and r["clean"]
                         and r["workflow"] in failure["workflows"])
            # The latest run of such a workflow failed, but its logs could
            # not all be read: whether it still shows this failure is unknown.
            for workflow in failure["workflows"]:
                latest = next((r for r in actions_runs if r["workflow"] == workflow), None)
                if latest and not latest["clean"]:
                    failure["latest_unread"] = True
        failure["green_runs"] = green

    failures = list(found.values())
    for failure in failures:
        failure["last_seen"] = failure["last_seen"][:10]
        failure["first_seen"] = failure["first_seen"][:10]
    # The class names behind the newest spotbugs reports, while Jenkins keeps them.
    fetched = 0
    for failure in sorted(failures, key=lambda f: f["last_seen"], reverse=True):
        if failure["subsystem"] == "spotbugs" and failure.get("report") \
                and fetched < CI_REPORTS_PER_PR:
            failure["classes"] = spotbugs_report(failure["report"])
            fetched += 1
    return failures


def fetch_ci_failures(repo: str, number: int, token: str | None) -> list[dict[str, Any]]:
    key = (repo, number)
    if key not in _CI_CACHE:
        owner, _, name = repo.partition("/")
        try:
            data = graphql(CI_QUERY, {"owner": owner, "name": name, "number": number}, token)
            pr = ((data.get("repository") or {}) or {}).get("pullRequest") or {}
        except SystemExit:
            pr = {}
        _CI_CACHE[key] = {"failures": parse_ci_failures(pr, token) if pr else []}
    return _CI_CACHE[key]["failures"]


def _main_code(path: str) -> bool:
    return "/src/test/" not in path and not os.path.basename(path).startswith("Test")


def _project_of_test(test: str) -> str:
    """Top-level source tree of a test, from its package."""
    package = test.replace("org.apache.", "")
    for prefix, project in (("hadoop.hdfs", "hadoop-hdfs-project"),
                            ("hadoop.yarn", "hadoop-yarn-project"),
                            ("hadoop.mapred", "hadoop-mapreduce-project")):
        if package.startswith(prefix):
            return project
    return ""


def _plugin_word(plugin: str) -> str:
    """'jasmine-maven-plugin' -> 'jasmine', 'maven-enforcer-plugin' -> 'enforcer'."""
    return re.sub(r"^maven-|-maven-plugin$|-plugin$", "", plugin)


def _frame_hit(frames: list[str], paths: list[str],
               diff: Callable[[], dict[str, dict[str, Any]]] | None) -> str | None:
    """A hunk of this change over a trunk line a failing stack trace runs through."""
    lines: dict[str, list[int]] = {}
    for frame in frames:
        name, _, line = frame.partition(":")
        lines.setdefault(name, []).append(int(line))
    touched = [p for p in paths if os.path.basename(p) in lines]
    if not touched or diff is None:
        return None
    hunks = diff()  # fetched only now: most failures have no frame in these files
    for path in touched:
        name = os.path.basename(path)
        for start, end in (hunks.get(path) or {}).get("trunk", []):
            for line in lines[name]:
                if start <= line <= end:
                    return f"it edits {name}:{start}-{end}, where %s fails at line {line}"
    return None


def ci_fix_match(failures: list[dict[str, Any]], paths: list[str],
                 title: str, body: str = "",
                 diff: Callable[[], dict[str, dict[str, Any]]] | None = None
                 ) -> dict[str, Any] | None:
    """Does a change with these files, title and description clear one of the failures?

    Returns {'strength': 'strong'|'medium'|'weak', 'reason': ...} for the best
    match. Strong: it edits the failing test, or (given `diff`, called only
    when needed) the trunk lines its stack trace runs through in another test
    file (a parent class), the class spotbugs names, a file
    a check annotated, or the module whose plugin run fails while naming that
    plugin in its title; or it names the
    check and edits the failing module; or its description names the failing
    test and it edits code of the same source tree. Medium: it edits the class
    under a failing test, the module's findbugs-exclude file, or only names the
    failing test. Weak: it only edits main code of that module.
    """
    # The dependency block quotes the failures other PRs fix: reading it back
    # would make every PR "name" the tests of its neighbours.
    body = split_managed_block(body)[0]
    rank = {"strong": 3, "medium": 2, "weak": 1}
    best: dict[str, Any] | None = None
    lowered = (title or "").lower()
    # A 118-file upgrade touches failing tests on its way; that is not a fix.
    # A small change, or one whose title is about fixing tests, is.
    focused = len(paths) <= FOCUSED_FILES or bool(FIX_WORDS_RE.search(lowered))

    def offer(strength: str, reason: str, named: bool = False) -> None:
        nonlocal best
        if strength != "weak" and not (focused or named):
            strength, reason = "weak", f"{reason} (but as part of a {len(paths)}-file change)"
        offered = {"strength": strength, "reason": reason, "current": current,
                   "last_seen": seen, "green_runs": failure.get("green_runs", 0),
                   "latest_unread": failure.get("latest_unread", False)}
        # A failure that is gone ranks below any that is not; among equals,
        # prefer a failure that is still red today.
        def key(m: dict[str, Any]) -> tuple[bool, int, bool]:
            return (not ci_stale(m), rank[m["strength"]], bool(m["current"]))
        if best is None or key(offered) > key(best):
            best = offered

    for failure in failures:
        seen = failure.get("last_seen") or ""
        current = failure.get("current", True)
        when = ("" if not seen else ", still red in the latest run" if current
                else f", last seen {seen}")
        places = " and ".join(failure.get("seen_in") or ["precommit"])
        via = failure.get("via")
        # Failures of a PR this branch is stacked on are this PR's failures too.
        where = f"the {places} of {via}, whose commits this branch carries" if via \
            else f"this PR's {places}"
        red = f"{via} (whose commits this branch carries)" if via else "this PR"
        subsystem = failure["subsystem"]
        if failure.get("test"):
            test = failure["test"]
            simple = test.rsplit(".", 1)[-1]
            package = "/".join(test.split(".")[:-1])
            in_title = simple.lower() in lowered
            in_body = bool(body) and re.search(rf"\b{re.escape(simple)}\b", body) is not None
            project = _project_of_test(test)
            # A change that edits a file the stack trace runs through is judged
            # by its lines: editing (or naming) the test elsewhere is no fix.
            frame_files = {f.split(":")[0] for f in failure.get("frames") or []}
            by_line = diff is not None and any(os.path.basename(p) in frame_files for p in paths)
            if not by_line and (in_title or any(p.endswith(f"/{simple}.java") for p in paths)):
                offer("strong", f"it fixes {simple}, which fails in {where}{when}", in_title)
            elif hit := _frame_hit(failure.get("frames") or [], paths, diff):
                offer("strong", f"{hit % simple} in {where}{when}")
            elif simple.startswith("Test") and any(
                    p.endswith(f"{package}/{simple[4:]}.java") for p in paths):
                offer("medium", f"it changes {simple[4:]}, the class {simple} tests; "
                                f"{simple} fails in {where}{when}")
            elif in_body and project and any(p.startswith(project + "/") and _main_code(p)
                                             for p in paths):
                offer("strong", f"its description names {simple}, which fails in {where}"
                                f"{when}, and it changes {project} code")
            elif in_body:
                offer("medium", f"its description names {simple}, which fails in {where}{when}")
            continue
        if failure.get("project"):
            project, plugin = failure["project"], failure.get("plugin") or "build"
            word = _plugin_word(plugin).lower()
            changed = [p for p in paths if f"/{project}/" in f"/{p}"]
            # Editing the module's pom for some other reason (a surefire bump,
            # say) does not fix its jasmine run: the title must name the plugin.
            # Descriptions don't count; PRs the failure blocks mention it there too.
            named = re.search(rf"\b{re.escape(word)}\b", title or "", re.I) is not None
            if changed:
                offer("strong" if named else "weak",
                      f"it changes {os.path.basename(changed[0])} in {project}, "
                      f"whose {plugin} run fails in {where}{when}", named)
            continue
        for path in failure.get("files") or []:
            if path in paths:
                offer("strong", f"it changes {path}, where '{subsystem}' fails in {where}{when}")
        for cls, types in (failure.get("classes") or {}).items():
            source = cls.split("$")[0].replace(".", "/") + ".java"
            if any(p.endswith(source) for p in paths):
                short = cls.split("$")[0].rsplit(".", 1)[-1]
                kinds = join(types, limit=2)
                offer("strong", f"it changes {short}, which carries the trunk {subsystem} "
                                f"warning" + (f" {kinds}" if kinds else "")
                                + f" that turns {red} red{when}",
                      short.lower() in lowered or subsystem in lowered)
        module = failure.get("module") or ""
        if not module:
            continue
        in_module = [p for p in paths if (p.startswith(module + "/") or f"/{module}/" in p
                                          or p.startswith(module.rsplit("/", 1)[-1] + "/"))
                     and _main_code(p)]
        leaf = module.rsplit("/", 1)[-1]
        words = CHECK_WORDS.get(subsystem, (subsystem,))
        if in_module and any(word in lowered for word in words):
            offer("strong", f"it names {subsystem} and changes {leaf}, where trunk's "
                            f"{subsystem} -1 turns {red} red{when}", named=True)
        if subsystem == "spotbugs":
            for p in paths:
                if os.path.basename(p) == "findbugs-exclude.xml":
                    root = p.split("/dev-support/")[0]
                    if module.startswith(root + "/") or module == root:
                        offer("medium", f"it edits {p}, which covers {leaf}, where trunk's "
                                        f"spotbugs -1 turns {red} red{when}")
        if in_module:
            offer("weak", f"it changes main code of {leaf}, where trunk's {subsystem} -1 "
                          f"turns {red} red{when}")
    return best


def days_since(day: str) -> int:
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(day[:10])).days
    except ValueError:
        return 0


def ci_stale(ci: dict[str, Any]) -> bool:
    """The failure a CI fix clears is gone.

    All three hold: the latest run of each check that reported it (the newest
    Yetus comment, the newest run of each Actions workflow) does not show it;
    it was last seen STALE_DAYS or more ago; and at least one run of that same
    check since then did not report it.
    """
    if not ci or ci.get("current", True) or ci.get("latest_unread") or not ci.get("last_seen"):
        return False
    return days_since(ci["last_seen"]) >= STALE_DAYS and ci.get("green_runs", 0) >= 1


def verdict_for(entry: dict[str, Any]) -> tuple[str, str]:
    """(verdict, why) for one candidate dependency, from the evidence gathered.

    A CI fix whose failure is gone (see `ci_stale`) no longer counts; when
    nothing else supports the dependency either, it is STALE.
    """
    ci = entry.get("ci") or {}
    if not ci_stale(ci):
        verdict, why = _verdict_from(entry, ci)
        if verdict == "CI-FIX" and not ci.get("current", True) and ci.get("last_seen"):
            why += (" - not stale yet: latest run unread" if ci.get("latest_unread") else
                    f" - not stale yet: {days_since(ci['last_seen'])} of {STALE_DAYS} days, "
                    f"{ci.get('green_runs', 0)} green run(s) since")
        return verdict, why
    verdict, why = _verdict_from(entry, {})
    if verdict in ("WEAK", "UNSUPPORTED", "UNVERIFIED"):
        return "STALE", (f"the CI failure it cleared is gone: {ci['reason']}; absent from the "
                         f"latest run, {days_since(ci['last_seen'])} days ago, and "
                         f"{ci.get('green_runs', 0)} green run(s) since")
    return verdict, why


def _verdict_from(entry: dict[str, Any], ci: dict[str, Any]) -> tuple[str, str]:
    evidence = entry.get("evidence") or {}
    if not entry.get("number"):
        return "UNVERIFIED", f"no open pull request carries {entry.get('jira') or entry['ref']}"
    if entry.get("ancestry"):
        return "CONFIRMED", "this branch is built on top of that one"
    if evidence.get("uses"):
        names = join([f"{name} ({os.path.basename(path)})" for name, path in evidence["uses"][:3]])
        return "CONFIRMED", f"this diff uses {names}, which that PR introduces"
    if ci.get("strength") == "strong":
        return "CI-FIX", ci["reason"]
    if evidence.get("clashes"):
        return "LIKELY", f"both diffs edit the same lines ({join(evidence['clashes'][:2])})"
    if ci.get("strength") == "medium":
        return "LIKELY", ci["reason"]
    if ci.get("strength") == "weak":
        return "WEAK", ci["reason"]
    if evidence.get("shared_files"):
        return "WEAK", (
            f"only a shared file ({join([os.path.basename(p) for p in evidence['shared_files'][:2]])}), "
            "no code of one inside the other"
        )
    if evidence.get("available"):
        return "UNSUPPORTED", ("the two diffs have no file, line or symbol in common, and "
                               "that PR fixes none of the precommit failures")
    return "UNVERIFIED", "the diff of that PR could not be read"


# --------------------------------------------------------------------------- #
# A failure none of the author's PRs clears: somebody else's PR, a JIRA, or nothing
# --------------------------------------------------------------------------- #
FIXER_SEARCH_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 10) {
    nodes {
      ... on PullRequest {
        number title url body state mergedAt author { login }
        files(first: 100) { nodes { path } }
      }
    }
  }
}
"""
UNEXPLAINED_LIMIT = 10
JIRA_PROJECT_OF_TREE = {"hadoop-hdfs-project": "HDFS", "hadoop-yarn-project": "YARN",
                        "hadoop-mapreduce-project": "MAPREDUCE"}
MODULE_NOISE = {"hadoop", "project", "server", "client", "applications", "webapp", "common",
                "yarn", "hdfs", "mapreduce"}
_FIXER_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_JIRA_SEARCH_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def failure_words(failure: dict[str, Any]) -> list[list[str]]:
    """Search terms for a failure: each inner list is one query, all words required."""
    if failure.get("test"):
        return [[failure["test"].rsplit(".", 1)[-1]]]
    if failure.get("project"):
        distinctive = [w for w in failure["project"].split("-") if w not in MODULE_NOISE]
        return [[_plugin_word(failure.get("plugin") or "")] + distinctive[:1]]
    classes = [c.split("$")[0].rsplit(".", 1)[-1] for c in (failure.get("classes") or {})]
    if classes:
        return [[c, failure["subsystem"]] for c in dict.fromkeys(classes)][:2]
    if failure.get("module"):
        return [[failure["subsystem"], failure["module"].rsplit("/", 1)[-1]]]
    return [[os.path.splitext(os.path.basename(p))[0]] for p in (failure.get("files") or [])[:1]]


def new_jira_summary(failure: dict[str, Any]) -> str:
    """What a JIRA for a failure nobody tracks could be called."""
    if failure.get("test"):
        simple = failure["test"].rsplit(".", 1)[-1]
        project = JIRA_PROJECT_OF_TREE.get(_project_of_test(failure["test"]), "HADOOP")
        return f"{project}: {simple} fails on trunk"
    if failure.get("project"):
        name = failure["project"]
        project = next((v for k, v in JIRA_PROJECT_OF_TREE.items()
                        if name.startswith(k.replace("-project", ""))), "HADOOP")
        return f"{project}: {failure.get('plugin')} fails on {name}"
    module = failure.get("module") or ""
    project = JIRA_PROJECT_OF_TREE.get(module.split("/", 1)[0], "HADOOP")
    classes = failure.get("classes") or {}
    if len(classes) == 1:
        cls, types = next(iter(classes.items()))
        short = cls.split("$")[0].rsplit(".", 1)[-1]
        return f"{project}: Fix SpotBugs {join(types, limit=2)} in {short}"
    if classes and module:
        return f"{project}: Fix the trunk SpotBugs warnings in {module.rsplit('/', 1)[-1]}"
    if module:
        return f"{project}: Fix the trunk {failure['subsystem']} warnings in {module.rsplit('/', 1)[-1]}"
    return f"{project}: {failure.get('detail')}"


def search_fixer_prs(repo: str, words: list[str], token: str | None) -> list[dict[str, Any]]:
    query = f"repo:{repo} is:pr " + " ".join(f'"{w}"' for w in words)
    if query not in _FIXER_SEARCH_CACHE:
        try:
            data = graphql(FIXER_SEARCH_QUERY, {"q": query}, token)
            _FIXER_SEARCH_CACHE[query] = [n for n in (data["search"]["nodes"] or []) if n]
        except SystemExit:
            _FIXER_SEARCH_CACHE[query] = []
    return _FIXER_SEARCH_CACHE[query]


def search_jira_issues(jira_base: str, words: list[str], since: str = "") -> list[dict[str, Any]]:
    text = " ".join(w.replace('"', "") for w in words)
    # Only issues still open, or resolved since the failure showed up: a
    # well-known test has pages of old fixes that would crowd them out.
    live = (f' AND (resolution = Unresolved OR resolved >= "{since[:10]}")'
            if since[:10] else "")
    if (text, live) not in _JIRA_SEARCH_CACHE:
        # Summary and description only: every issue whose precommit a test
        # broke quotes it in a Yetus comment, and 'text ~' would match those.
        phrase = f'"\\"{text}\\""' if len(words) == 1 else f'"{text}"'
        jql = (f"project in (HADOOP, HDFS, YARN, MAPREDUCE) AND "
               f"(summary ~ {phrase} OR description ~ {phrase}){live} ORDER BY updated DESC")
        data = jira_get(jira_base, "search", {
            "jql": jql, "maxResults": "8",
            "fields": "summary,status,resolution,resolutiondate,description",
        })
        issues = []
        for issue in (data or {}).get("issues") or []:
            fields = issue.get("fields") or {}
            haystack = f"{fields.get('summary') or ''} {fields.get('description') or ''}".lower()
            if all(w.lower() in haystack for w in words):  # JIRA's text search is loose
                issues.append({
                    "key": issue.get("key", ""),
                    "summary": fields.get("summary") or "",
                    "status": ((fields.get("status") or {}) or {}).get("name", ""),
                    "resolution": ((fields.get("resolution") or {}) or {}).get("name", ""),
                    "resolved": (fields.get("resolutiondate") or "")[:10],
                })
        _JIRA_SEARCH_CACHE[(text, live)] = issues
    return _JIRA_SEARCH_CACHE[(text, live)]


def existing_fixes(failure: dict[str, Any], repo: str, token: str | None, jira_base: str,
                   skip_numbers: set[int], skip_keys: set[str]) -> dict[str, Any]:
    """Pull requests of anybody, and JIRA issues, that address a failure.

    A PR counts when the same matching as for the author's own PRs says it
    clears the failure, and it is open or was merged after the failure first
    showed up (then a rebase clears it). A JIRA counts when it is unresolved,
    or was resolved after the failure first showed up.
    """
    since = failure.get("first_seen") or ""
    prs: dict[int, dict[str, Any]] = {}
    jiras: dict[str, dict[str, Any]] = {}
    for words in failure_words(failure):
        if not all(words):
            continue
        for node in search_fixer_prs(repo, words, token):
            number = node.get("number")
            if number in skip_numbers or number in prs:
                continue
            merged = (node.get("mergedAt") or "")[:10]
            if node.get("state") == "CLOSED" or (node.get("state") == "MERGED" and merged < since):
                continue
            paths = [f["path"] for f in ((node.get("files") or {}).get("nodes") or [])]
            match = ci_fix_match([failure], paths, node.get("title") or "", node.get("body") or "",
                                 lambda n=number: pr_diff(repo, n, token))
            if match and match["strength"] in ("strong", "medium"):
                key = JIRA_IN_TEXT_RE.match((node.get("title") or "").strip())
                prs[number] = {
                    "number": number, "title": node.get("title") or "",
                    "url": node.get("url") or "", "state": node.get("state") or "",
                    "merged": merged, "strength": match["strength"],
                    "author": ((node.get("author") or {}) or {}).get("login", ""),
                    "jira": key.group(0).upper() if key else None,
                    "reason": match["reason"],
                }
        for issue in search_jira_issues(jira_base, words, since):
            if issue["key"] in skip_keys or issue["key"] in jiras:
                continue
            if issue["resolution"] and issue["resolved"] < since:
                continue  # an old fix of the same test, not today's
            jiras[issue["key"]] = issue
    covered = {p["jira"] for p in prs.values() if p.get("jira")}
    return {
        "prs": sorted(prs.values(), key=lambda p: (p["state"] != "OPEN", p["strength"] != "strong")),
        "jiras": [j for k, j in jiras.items() if k not in covered],
    }


# --------------------------------------------------------------------------- #
# Dependencies between pull requests
# --------------------------------------------------------------------------- #
def git_run(repo_path: str, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], cwd=repo_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.returncode, (result.stdout or "").strip()


def has_commit(repo_path: str, sha: str | None) -> bool:
    # The ^{commit} suffix matters: 'rev-parse --verify <sha>' echoes back a
    # full sha even when the object itself is missing from this clone.
    if not sha:
        return False
    return git_run(repo_path, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")[0] == 0


def is_ancestor(repo_path: str, ancestor: str, descendant: str) -> bool:
    return git_run(repo_path, "merge-base", "--is-ancestor", ancestor, descendant)[0] == 0


# The block fix_dependencies.py keeps in a PR description: the PRs this one
# needs merged first, and the PRs that wait for this one. Both halves of a
# dependency between two PRs are written by hand on GitHub, one per PR.
BLOCK_START = "<!-- dependencies: maintained by analyze_pr.py -->"
BLOCK_END = "<!-- /dependencies -->"
DEPENDS_HEADER = "**Depends on** (has to be merged first):"
REQUIRED_HEADER = "**Required by** (waits for this one to be merged):"


def split_managed_block(text: str) -> tuple[str, dict[str, list[str]]]:
    """(the text around the block, {'depends': [...], 'required': [...]} references)."""
    sections: dict[str, list[str]] = {"depends": [], "required": []}
    text = text or ""
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return text, sections
    section = "depends"   # blocks written before 'Required by' existed
    for line in text[start + len(BLOCK_START): end].splitlines():
        if line.startswith("**Required by**"):
            section = "required"
        elif line.startswith("**Depends on**"):
            section = "depends"
        elif line.startswith("- ") and (ref := REF_IN_TEXT_RE.search(line[2:].split(" - ")[0])):
            number = ref.group(2) or ref.group(3)
            sections[section].append(f"#{number}" if number else ref.group(0).upper())
    return text[:start] + text[end + len(BLOCK_END):], sections


def text_references(text: str, repo: str, limit: int = 8) -> list[tuple[str, str]]:
    """(reference, quoted wording) for every 'depends on <ref>' style phrase.

    The reference has to sit in the same sentence as the wording, the sentence
    must not be a negation, and a github.com link has to point at this same
    repository - 'a bug in apache/maven-surefire#3453' is not a dependency of a
    Hadoop pull request.
    """
    found: list[tuple[str, str]] = []
    if not text:
        return found
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    clean = re.sub(r"```.*?```", " ", clean, flags=re.S)
    for phrase in DEP_PHRASE_RE.finditer(clean):
        start = max(clean.rfind(".", 0, phrase.start()),
                    clean.rfind("\n", 0, phrase.start())) + 1
        end = min(
            (pos for pos in (clean.find(mark, phrase.end()) for mark in (". ", "\n", "! ", "? "))
             if pos != -1),
            default=len(clean),
        )
        sentence = clean[start: min(end + 1, start + 400)]
        if NEGATION_RE.search(sentence):
            continue
        window = clean[phrase.start(): min(end + 1, phrase.end() + 100)]
        # 'To clear it, merge first:' followed by a numbered or bulleted list.
        if clean[phrase.end(): end].rstrip().endswith(":"):
            for line in clean[end:].lstrip("\n").splitlines()[:8]:
                if not line.strip():
                    continue
                if not LIST_ITEM_RE.match(line):
                    break
                window += "\n" + line
        for ref in REF_IN_TEXT_RE.finditer(window):
            if ref.group(1) and ref.group(1).lower() != repo.lower():
                continue  # a pull request of another project
            number = ref.group(2) or ref.group(3)
            reference = f"#{number}" if number else ref.group(0).upper()
            quote = " ".join(sentence.split())[:110]
            found.append((reference, quote))
            if len(found) >= limit:
                return found
    return found


def add_dependency(store: dict[str, dict[str, Any]], reference: str, reason: str,
                   data: dict[str, Any] | None = None, key: str | None = None,
                   source: str = "text") -> None:
    entry = store.setdefault(
        reference,
        {"ref": reference, "number": None, "title": "", "state": "UNKNOWN",
         "url": "", "jira": key, "reasons": [], "sources": [], "files": []},
    )
    if source not in entry["sources"]:
        entry["sources"].append(source)
    if source == "git":
        entry["ancestry"] = True
    if data and data.get("files"):
        entry["files"] = [f["path"] for f in ((data.get("files") or {}).get("nodes") or [])]
    if data:
        entry["number"] = data.get("number")
        entry["title"] = data.get("title", "")
        entry["state"] = data.get("state", "UNKNOWN")
        entry["url"] = data.get("url", "")
        entry["body"] = data.get("body") or ""
        entry["author"] = ((data.get("author") or {}) or {}).get("login", "")
    if key and not entry.get("jira"):
        entry["jira"] = key
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)


def collect_dependencies(
    pr: dict[str, Any],
    jira: Jira | None,
    repo: str,
    token: str | None,
    repo_path: str | None,
    use_diffs: bool = True,
    jira_base: str = DEFAULT_JIRA,
    search_external: bool = True,
) -> dict[str, Any]:
    """Find the PRs this one needs merged first, and the ones waiting on it.

    Signals, in decreasing order of confidence:

    1. git history - the head branch of this PR already contains the commits of
       another open PR, i.e. the branches are stacked;
    2. the code itself - this diff uses a class, method, constant, maven
       property or configuration key that another open PR introduces, or the
       two diffs edit the same lines of the same file;
    3. the CI - this PR is red (Yetus, the Jenkins unit log, the GitHub
       Actions history of its branch) because of a trunk warning, a test or a
       module build it does not touch, and another open PR edits exactly that
       class, test or module, or its description names the failing test;
    4. wording in the PR description, in the discussion or in the JIRA
       description - 'depends on #8699', 'based on HADOOP-19970', 'to clear
       the -1, merge first: ...';
    5. JIRA issue links - 'is blocked by' / 'depends upon' (and their reverse).

    Signals 4 and 5 are written by hand, so every one of them is checked
    against the diffs and the precommit history: each dependency carries a
    verdict of CONFIRMED, CI-FIX, LIKELY, WEAK, UNSUPPORTED, UNVERIFIED or
    STALE, and dependencies nobody declared are reported as DISCOVERED.

    A failure still red that no PR of the author clears is looked up among
    everybody's PRs and in JIRA. An open PR of somebody else that clears it
    (strong match, with a JIRA key) becomes a DISCOVERED dependency like any
    other; the rest - weaker matches, merged fixes, JIRAs without a PR, and a
    suggested new JIRA when nothing turns up - is reported ('unexplained').
    """
    self_number = pr["number"]
    self_key = (jira.key.upper() if jira else None)
    depends: dict[str, dict[str, Any]] = {}
    blocks: dict[str, dict[str, Any]] = {}
    overlaps: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []

    def resolve(reference: str, reason: str, store: dict[str, dict[str, Any]],
                source: str = "text") -> None:
        if reference.startswith("#"):
            number = int(reference[1:])
            if number == self_number:
                return
            data = fetch_pr_summary(repo, number, token)
            if data:
                add_dependency(store, f"#{number}", reason, data, source=source)
            return
        key = reference.upper()
        if self_key and key == self_key:
            return
        number = pr_for_jira_cached(repo, key, token)
        data = fetch_pr_summary(repo, number, token) if number else None
        if data and data["number"] == self_number:
            return
        label = f"#{data['number']}" if data else key
        add_dependency(store, label, reason, data, key=key, source=source)

    # ----- 1. wording ------------------------------------------------------- #
    bots = {b.lower() for b in DEFAULT_BOTS} | {"github-actions"}
    author = ((pr.get("author") or {}) or {}).get("login", "")
    description, listed = split_managed_block(pr.get("body") or "")
    for reference in listed["depends"]:
        resolve(reference, "the 'Depends on' list of the PR description names it", depends,
                source="block")
    for reference in listed["required"]:
        resolve(reference, "the 'Required by' list of the PR description names it", blocks,
                source="block")
    sources: list[tuple[str, str]] = [("the PR description", description)]
    for comment in ((pr.get("comments") or {}).get("nodes") or []):
        login = ((comment.get("author") or {}) or {}).get("login", "")
        if login.lower() in bots:
            continue
        who = "you" if login == author else login
        sources.append((f"a comment by {who}", comment.get("body") or ""))
    if jira and jira.found:
        sources.append((f"the {jira.key} description", jira.description))

    for where, text in sources:
        for reference, quote in text_references(text, repo):
            resolve(reference, f'{where} says "{quote}"', depends)

    # ----- 2. JIRA issue links ---------------------------------------------- #
    if jira and jira.found:
        for link in jira.links:
            label, key = link["label"], link["key"]
            if link.get("resolution") or link.get("status", "").lower() in ("resolved", "closed"):
                continue  # already done, nothing to wait for
            if any(token_text in label for token_text in JIRA_BLOCKING_LABELS):
                resolve(key, f"{jira.key} '{label}' {key} ({link['status']}) in JIRA",
                        depends, source="jira")
            elif any(token_text in label for token_text in JIRA_BLOCKED_LABELS):
                resolve(key, f"{jira.key} '{label}' {key} ({link['status']}) in JIRA",
                        blocks, source="jira")

    # ----- 3. stacked branches ---------------------------------------------- #
    head = (((pr.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}).get("oid")
    peers: list[dict[str, Any]] = []
    if author:
        try:
            peers = [p for p in fetch_peer_prs(repo, author, token) if p["number"] != self_number]
        except SystemExit:
            peers = []

    if repo_path and head and has_commit(repo_path, head):
        for peer in peers:
            peer_head = peer.get("headRefOid")
            if not peer_head or peer_head == head or not has_commit(repo_path, peer_head):
                continue
            if is_ancestor(repo_path, peer_head, head):
                add_dependency(
                    depends, f"#{peer['number']}",
                    f"branch {pr.get('headRefName')} already contains the commits of "
                    f"{peer.get('headRefName')}",
                    peer, source="git",
                )
            elif is_ancestor(repo_path, head, peer_head):
                add_dependency(
                    blocks, f"#{peer['number']}",
                    f"branch {peer.get('headRefName')} is built on top of "
                    f"{pr.get('headRefName')}",
                    peer, source="git",
                )

    # A PR opened against another branch than the base of the repository is
    # stacked by construction.
    base = pr.get("baseRefName") or ""
    if base and base not in ("trunk", "main", "master") and not base.startswith("branch-"):
        for peer in peers:
            if peer.get("headRefName") == base:
                add_dependency(
                    depends, f"#{peer['number']}",
                    f"this PR targets {base}, the head branch of that PR", peer,
                    source="git",
                )

    # ----- shared files: a candidate nobody declared ------------------------- #
    own_files = sorted({f["path"] for f in ((pr.get("files") or {}).get("nodes") or [])})
    peer_by_number = {p["number"]: p for p in peers}
    candidates: dict[int, list[str]] = {}
    for peer in peers:
        shared = set(own_files) & {f["path"] for f in ((peer.get("files") or {}).get("nodes") or [])}
        if shared:
            candidates[peer["number"]] = sorted(shared)

    # ----- 4. the diffs themselves: verify and discover ---------------------- #
    base_ref = resolve_base_ref(repo_path)
    checked: dict[int, dict[str, Any]] = {}
    if use_diffs:
        self_diff = pr_diff(repo, self_number, token)
        # Everything somebody declared, plus every PR touching the same files.
        to_check: list[int] = []
        for entry in list(depends.values()) + list(blocks.values()):
            if entry.get("number"):
                to_check.append(entry["number"])
        to_check += [n for n in candidates if n not in to_check]
        for number in to_check[:8]:
            peer = peer_by_number.get(number) or fetch_pr_summary(repo, number, token) or {}
            peer_paths = [f["path"] for f in ((peer.get("files") or {}).get("nodes") or [])]
            checked[number] = code_evidence(
                repo, token, self_diff, number, repo_path, base_ref, own_files, peer_paths
            )

        # The precommit: a -1 here that the other PR clears, or the reverse.
        own_ci = fetch_ci_failures(repo, self_number, token)
        own_title = pr.get("title") or ""
        own_body = pr.get("body") or ""
        # A branch stacked on another carries its failures too.
        inherited = [
            {**failure, "via": entry["ref"]}
            for entry in depends.values() if entry.get("ancestry") and entry.get("number")
            for failure in fetch_ci_failures(repo, entry["number"], token)
        ]
        rank = {"strong": 3, "medium": 2, "weak": 1}
        for entry in depends.values():
            if not entry.get("number"):
                continue
            found = [ci_fix_match(failures, entry.get("files") or [], entry.get("title") or "",
                                  entry.get("body") or "",
                                  lambda n=entry["number"]: pr_diff(repo, n, token))
                     for failures in (own_ci, [f for f in inherited if f["via"] != entry["ref"]])
                     if failures]
            found = [m for m in found if m]
            if found:
                entry["ci"] = max(found, key=lambda m: rank[m["strength"]])
        for entry in blocks.values():
            if entry.get("number"):
                match = ci_fix_match(fetch_ci_failures(repo, entry["number"], token),
                                     own_files, own_title, own_body, lambda: self_diff)
                if match:
                    match["reason"] = match["reason"].replace("this PR", "that PR")
                    entry["ci"] = match

        for store in (depends, blocks):
            for entry in store.values():
                if entry.get("number") in checked:
                    entry["evidence"] = checked[entry["number"]]
                entry["verdict"], entry["verdict_reason"] = verdict_for(entry)

        # A precommit fix nobody wrote down, in either direction. Only for a
        # -1 still red in the latest run: an old flake proposes no new link.
        for peer in peers:
            reference = f"#{peer['number']}"
            peer_paths = [f["path"] for f in ((peer.get("files") or {}).get("nodes") or [])]
            if reference not in depends and own_ci:
                match = ci_fix_match(own_ci, peer_paths, peer.get("title") or "",
                                     peer.get("body") or "",
                                     lambda n=peer["number"]: pr_diff(repo, n, token))
                if match and match["strength"] == "strong" and match["current"]:
                    add_dependency(depends, reference, match["reason"], peer, source="ci")
                    depends[reference].update(
                        ci=match, verdict="DISCOVERED",
                        verdict_reason=f"undeclared: {match['reason']}",
                    )
            if reference not in blocks:
                match = ci_fix_match(fetch_ci_failures(repo, peer["number"], token),
                                     own_files, own_title, own_body, lambda: self_diff)
                if match and match["strength"] == "strong" and match["current"]:
                    match["reason"] = match["reason"].replace("this PR", "that PR")
                    add_dependency(blocks, reference, match["reason"], peer, source="ci")
                    blocks[reference].update(
                        ci=match, verdict="DISCOVERED",
                        verdict_reason=f"undeclared: {match['reason']}",
                    )

        # Still red, and none of the author's PRs clears it: somebody else's
        # PR may, a JIRA may track it, or it needs a JIRA.
        if search_external:
            fixers = [(e.get("files") or [], e.get("title") or "", e.get("body") or "",
                       lambda n=e["number"]: pr_diff(repo, n, token))
                      for e in depends.values() if e.get("number")]
            fixers += [([f["path"] for f in ((p.get("files") or {}).get("nodes") or [])],
                        p.get("title") or "", p.get("body") or "",
                        lambda n=p["number"]: pr_diff(repo, n, token)) for p in peers]
            fixers.append((own_files, own_title, own_body, lambda: self_diff))  # a PR that clears its own -1
            skip_numbers = {self_number} | {e["number"] for e in depends.values() if e.get("number")}
            skip_keys = {k for k in [self_key] + [e.get("jira") for e in depends.values()] if k}
            for failure in [f for f in own_ci if f.get("current")]:
                if len(unexplained) >= UNEXPLAINED_LIMIT:
                    break
                if any((m := ci_fix_match([failure], *fixer)) and m["strength"] in ("strong", "medium")
                       for fixer in fixers):
                    continue
                hits = existing_fixes(failure, repo, token, jira_base, skip_numbers, skip_keys)
                new_jira = None if hits["prs"] or hits["jiras"] else new_jira_summary(failure)
                # Somebody else's open PR that surely clears it counts as a
                # dependency, like one of the author's own: proposed, confirmed
                # one by one. A medium match or a merged fix stays a report.
                for fix in [p for p in hits["prs"] if p["state"] == "OPEN"
                            and p["strength"] == "strong" and p.get("jira")]:
                    data = fetch_pr_summary(repo, fix["number"], token)
                    if not data:
                        continue
                    reference = f"#{fix['number']}"
                    add_dependency(depends, reference, fix["reason"], data,
                                   key=fix["jira"], source="ci")
                    depends[reference].update(
                        ci={"strength": "strong", "reason": fix["reason"], "current": True},
                        external=fix["author"], verdict="DISCOVERED",
                        verdict_reason=f"undeclared, a PR of {fix['author']}: {fix['reason']}",
                    )
                    fixers.append((depends[reference]["files"], fix["title"],
                                   depends[reference].get("body") or ""))
                    skip_numbers.add(fix["number"])
                    skip_keys.add(fix["jira"])
                    hits["prs"].remove(fix)
                if hits["prs"] or hits["jiras"] or new_jira:
                    unexplained.append({
                        "failure": failure["detail"],
                        "seen_in": failure.get("seen_in") or [],
                        "last_seen": failure.get("last_seen") or "",
                        **hits,
                        "new_jira": new_jira,
                        "record": failure,  # what create_jira.py describes
                    })

        # A dependency the diffs show but nobody wrote down anywhere.
        for number, evidence in checked.items():
            reference = f"#{number}"
            peer = peer_by_number.get(number) or fetch_pr_summary(repo, number, token) or {}
            if evidence.get("uses") and reference not in depends:
                names = join([f"{n} ({os.path.basename(p)})" for n, p in evidence["uses"][:3]])
                add_dependency(
                    depends, reference,
                    f"this diff uses {names}, introduced by that PR", peer, source="code",
                )
                depends[reference]["evidence"] = evidence
                depends[reference]["verdict"] = "DISCOVERED"
                depends[reference]["verdict_reason"] = f"undeclared: this diff uses {names}"
            if evidence.get("provides") and reference not in blocks:
                names = join([f"{n} ({os.path.basename(p)})" for n, p in evidence["provides"][:3]])
                add_dependency(
                    blocks, reference,
                    f"that PR uses {names}, introduced here", peer, source="code",
                )
                blocks[reference]["evidence"] = evidence
                blocks[reference]["verdict"] = "DISCOVERED"
                blocks[reference]["verdict_reason"] = f"undeclared: that diff uses {names}"

    # ----- what is left is a plain conflict risk ----------------------------- #
    for number, shared in candidates.items():
        reference = f"#{number}"
        if reference in depends or reference in blocks:
            continue
        peer = peer_by_number.get(number, {})
        evidence = checked.get(number, {})
        overlaps.append(
            {
                "ref": reference, "number": number,
                "title": peer.get("title", ""), "url": peer.get("url", ""),
                "state": peer.get("state", "OPEN"),
                "files": shared[:3], "count": len(shared),
                "clashes": evidence.get("clashes", []),
            }
        )
    overlaps.sort(key=lambda o: (-len(o.get("clashes") or []), -o["count"]))
    del overlaps[3:]

    for entry in list(depends.values()) + list(blocks.values()):
        entry["open"] = entry["state"] not in ("MERGED", "CLOSED")
        entry.setdefault("verdict", "UNVERIFIED")
        entry.setdefault("verdict_reason", "the diffs were not compared")
    return {
        "depends_on": sorted(depends.values(), key=lambda e: (not e["open"], e["ref"])),
        "blocks": sorted(blocks.values(), key=lambda e: e["ref"]),
        "overlaps": overlaps,
        "unexplained": unexplained,
        "base_ref": base_ref,
        "diffs_compared": bool(checked),
    }


def describe_dependency(entry: dict[str, Any]) -> str:
    label = entry["ref"]
    if entry.get("jira") and entry["ref"].startswith("#"):
        label = f"{entry['ref']} ({entry['jira']})"
    elif entry.get("jira"):
        label = f"{entry['jira']} (no PR)"
    state = (entry.get("state") or "unknown").lower()
    return f"{label} [{state}]"


def format_dependencies(deps: dict[str, Any]) -> str:
    parts = []
    for entry in deps.get("depends_on", []):
        verb = "needs" if entry["open"] else "built on"
        parts.append(f"{verb} {describe_dependency(entry)} - {entry.get('verdict', '?')}")
    for entry in deps.get("blocks", []):
        parts.append(f"blocks {describe_dependency(entry)} - {entry.get('verdict', '?')}")
    for entry in deps.get("overlaps", []):
        clash = f", {len(entry['clashes'])} line clash(es)" if entry.get("clashes") else ""
        parts.append(f"shares {entry['count']} file(s) with {entry['ref']}{clash}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    jira_id: str
    jira_title: str
    pr_id: str
    pr_title: str
    status: str
    comments: str
    suggestion: str
    dependencies: str = ""
    url: str = ""
    jira_url: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def module_owners(repo_path: str, paths: list[str], exclude: list[str], limit: int = 3) -> list[str]:
    """Names of the people who most recently touched the same areas."""
    if not paths or not os.path.isdir(repo_path):
        return []
    roots = sorted({p.split("/")[0] + "/" + p.split("/")[1] for p in paths if p.count("/") >= 1})[:4]
    skip = {e.strip().lower() for e in exclude if e and e.strip()}
    counts: dict[str, int] = {}
    for root in roots:
        result = subprocess.run(
            ["git", "log", "-40", "--format=%an", "--", root],
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            continue
        for name in result.stdout.splitlines():
            name = name.strip()
            if name and name.lower() not in skip:
                counts[name] = counts.get(name, 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def unanswered_question(pr: dict[str, Any]) -> str | None:
    """Login of the last commenter waiting for an answer from the author."""
    author = ((pr.get("author") or {}) or {}).get("login", "")
    bots = {b.lower() for b in DEFAULT_BOTS} | {"github-actions"}
    for comment in reversed(((pr.get("comments") or {}).get("nodes") or [])):
        login = ((comment.get("author") or {}) or {}).get("login", "")
        if login.lower() in bots:
            continue
        if login and login != author:
            return login
        return None
    return None


def open_review_threads(pr: dict[str, Any]) -> list[str]:
    paths = []
    for thread in ((pr.get("reviewThreads") or {}).get("nodes") or []):
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        nodes = (thread.get("comments") or {}).get("nodes") or []
        if nodes:
            paths.append(nodes[0].get("path") or "?")
    return paths


def body_gaps(body: str) -> list[str]:
    """Parts of the Hadoop PR template that are missing or left empty."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S).strip()
    if len(text) < 60 and not re.search(r"^###", text, re.M):
        return ["the PR description is empty or a single line"]

    # Split the template into '### heading' -> body sections.
    sections: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        heading = re.match(r"^#{2,4}\s*(.+?)\s*$", line)
        if heading:
            current = heading.group(1).lower()
            sections.setdefault(current, "")
        elif current:
            sections[current] += line + "\n"

    gaps = []
    if sections:
        for heading, content in sections.items():
            if "description of pr" in heading and not content.strip():
                gaps.append("the 'Description of PR' section of the template is empty")
            if "how was this patch tested" in heading and not content.strip():
                gaps.append("the 'How was this patch tested?' section of the template is empty")
    else:
        if "test" not in text.lower():
            gaps.append("the PR description does not say how the change was tested")
    return gaps


def analyse(
    jira: Jira | None,
    pr: dict[str, Any] | None,
    repo: str,
    jira_base: str,
    repo_path: str | None,
    stale_days: int,
    reviewer_hints: bool,
    deps: dict[str, Any] | None = None,
) -> Report:
    notes: list[str] = []
    actions: list[str] = []
    details: dict[str, Any] = {}

    jira_id = jira.key if jira else "-"
    jira_title = (jira.title if jira and jira.found else "") or "-"
    jira_url = f"{jira_base.rstrip('/')}/browse/{jira.key}" if jira else ""

    # ----- no pull request at all ------------------------------------------ #
    if pr is None:
        status = "NO PR"
        if jira and jira.found:
            notes.append(f"JIRA is {jira.status}" + (f"/{jira.resolution}" if jira.resolution else ""))
            notes.append(f"no pull request in {repo} mentions {jira.key} in its title")
            if jira.resolution:
                actions.append(
                    f"{jira.key} is already {jira.resolution}; check whether a patch was "
                    "committed without a PR before opening one"
                )
            else:
                actions.append(
                    f"open a PR against trunk whose title is '{jira.key}. {jira.title}'"
                )
                actions.append(
                    "assign the JIRA to yourself and move it to Patch Available once the PR is up"
                )
        else:
            notes.append("neither a JIRA issue nor a pull request could be read")
            actions.append("check the identifier")
        return Report(
            jira_id=jira_id, jira_title=jira_title, pr_id="-", pr_title="-",
            status=status, comments="; ".join(notes),
            suggestion=" | ".join(f"{i}. {a}" for i, a in enumerate(actions, 1)),
            jira_url=jira_url, details=details,
        )

    number = pr["number"]
    pr_state = pr.get("state") or ""
    author = ((pr.get("author") or {}) or {}).get("login", "")

    # ----- closed / merged short circuit ------------------------------------ #
    if pr_state in ("MERGED", "CLOSED"):
        if pr_state == "MERGED":
            status = "MERGED"
            merged_by = ((pr.get("mergedBy") or {}) or {}).get("login", "?")
            notes.append(
                f"merged into {pr.get('baseRefName')} on {(pr.get('mergedAt') or '')[:10]} by {merged_by}"
            )
            if jira and jira.found:
                if not jira.resolution:
                    notes.append(f"but {jira.key} is still {jira.status} with no resolution")
                    actions.append(
                        f"resolve {jira.key} as Fixed and set the Fix Version to the release "
                        "that carries the commit"
                    )
                else:
                    notes.append(f"{jira.key} is {jira.status}/{jira.resolution}")
                if not jira.fix_versions and not jira.resolution:
                    actions.append("fill in the Fix Version field")
            if not actions:
                actions.append("nothing left to do")
        else:
            status = "CLOSED"
            notes.append(f"closed without merging on {(pr.get('closedAt') or '')[:10]}")
            actions.append(
                "reopen the PR, or open a fresh one from a rebased branch, if the change is still wanted"
            )
            if jira and jira.found and not jira.resolution:
                actions.append(f"leave a note on {jira.key} explaining why the PR was closed")
        return Report(
            jira_id=jira_id, jira_title=jira_title,
            pr_id=f"#{number}", pr_title=pr.get("title", ""),
            status=status, comments="; ".join(notes),
            suggestion=" | ".join(f"{i}. {a}" for i, a in enumerate(actions, 1)),
            url=pr.get("url", ""), jira_url=jira_url,
            details={"state": pr_state, "merged_at": pr.get("mergedAt")},
        )

    # ----- open PR: status and evidence ------------------------------------- #
    row = evaluate(pr, DEFAULT_BOTS, stale_days)
    status, notes = row.status, [n for n in row.comments.split("; ") if n]
    details = dict(row.details)

    actions_group, yetus_checks = classify_contexts(pr)
    yetus = parse_yetus_comment(pr, DEFAULT_BOTS)
    approvers, requesters, _ = summarise_reviews(pr)
    pending = requested_reviewers(pr)
    waiting_on = unanswered_question(pr)
    threads = open_review_threads(pr)
    gaps = body_gaps(pr.get("body", ""))
    title_ok = bool(jira and re.match(rf"^{re.escape(jira.key)}\.\s+\S", pr.get("title", ""), re.I))

    if jira and jira.found:
        notes.append(
            f"JIRA {jira.key} is {jira.status}"
            + (f"/{jira.resolution}" if jira.resolution else "")
            + (f", assigned to {jira.assignee}" if jira.assignee else ", unassigned")
        )
    elif jira:
        notes.append(f"JIRA {jira.key} could not be read")
    else:
        notes.append("the PR title carries no JIRA id")

    if threads:
        notes.append(f"{len(threads)} unresolved review thread(s) on {join(sorted(set(threads)))}")
    if waiting_on:
        notes.append(f"the last word in the discussion is {waiting_on}'s, still unanswered")
    for gap in gaps:
        notes.append(gap)
    if not title_ok and jira:
        notes.append("the PR title does not follow the 'JIRA-ID. Summary.' convention")

    # ----- dependencies on other pull requests ------------------------------ #
    deps = deps or {"depends_on": [], "blocks": [], "overlaps": []}
    blocking = [d for d in deps.get("depends_on", []) if d.get("open")]
    satisfied = [d for d in deps.get("depends_on", []) if not d.get("open")]
    blocked_by_me = [d for d in deps.get("blocks", []) if d.get("open")]

    solid = [d for d in blocking
             if d.get("verdict") in ("CONFIRMED", "CI-FIX", "LIKELY", "DISCOVERED")]
    doubtful = [d for d in blocking if d.get("verdict") in ("UNSUPPORTED", "WEAK")]

    if blocking:
        notes.append(
            "depends on "
            + join([f"{describe_dependency(d)} {d.get('verdict', '?')}" for d in blocking])
        )
        for entry in blocking[:3]:
            notes.append(f"{entry['ref']}: {entry.get('verdict_reason', entry['reasons'][0])}")
    for entry in deps.get("depends_on", []):
        if entry.get("verdict") == "DISCOVERED":
            notes.append(
                f"{entry['ref']} is not declared anywhere - neither the PR text nor a JIRA link "
                "mentions it"
            )
    for entry in satisfied:
        if entry["state"] == "MERGED":
            notes.append(
                f"{entry['ref']}, which this PR was built on, is already merged - "
                "a rebase on trunk should drop its commits"
            )
    if blocked_by_me:
        notes.append(
            "other open work waits for this PR: " + join([d["ref"] for d in blocked_by_me])
        )
    for overlap in deps.get("overlaps", []):
        if overlap.get("clashes"):
            notes.append(
                f"edits the same lines as {overlap['ref']} ({join(overlap['clashes'][:2])}) - "
                "whichever merges second will have to resolve a conflict"
            )
        else:
            notes.append(
                f"touches the same {overlap['count']} file(s) as {overlap['ref']} "
                f"({join(overlap['files'])}) - whichever merges second will need a rebase"
            )
    for item in deps.get("unexplained", []):
        where = " and ".join(item.get("seen_in") or ["CI"])
        for fix in item.get("prs", [])[:2]:
            if fix["state"] == "MERGED":
                actions.append(f"rebase on trunk: #{fix['number']} (merged {fix['merged']}) "
                               f"fixes {item['failure']} ({where})")
            else:
                notes.append(f"{item['failure']} ({where}) is fixed by #{fix['number']} of "
                             f"{fix['author']}, still open: {fix['reason']}")
        for issue in item.get("jiras", [])[:2]:
            notes.append(f"{item['failure']} ({where}) is tracked in {issue['key']} "
                         f"({issue['resolution'] or issue['status']}), with no PR found: "
                         f"{issue['summary']}")
        if item.get("new_jira"):
            actions.append(f"{item['failure']} ({where}) has no PR and no JIRA: consider opening "
                           f"'{item['new_jira']}'")

    details.update(
        {
            "unresolved_threads": threads,
            "waiting_on": waiting_on,
            "description_gaps": gaps,
            "title_follows_convention": title_ok,
            "changed_files": pr.get("changedFiles"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "jira": asdict(jira) if jira else None,
            "dependencies": deps,
        }
    )

    # ----- course of action -------------------------------------------------- #
    if solid:
        first = solid[0]
        actions.append(
            f"get {first['ref']} merged first"
            + (f" ({first['title']})" if first.get("title") else "")
            + ", then rebase this PR on trunk and force-push"
        )
        for entry in solid[1:]:
            actions.append(f"the same applies to {entry['ref']}")
    for entry in deps.get("depends_on", []):
        if entry.get("verdict") == "DISCOVERED" and entry.get("open"):
            actions.append(
                f"declare the dependency on {entry['ref']}: add an 'is blocked by' link on "
                f"{jira.key if jira and jira.found else 'the JIRA'} and say it in the PR "
                "description, so it is not merged out of order"
            )
    for entry in doubtful:
        advice = f"check the dependency on {entry['ref']}: {entry.get('verdict_reason', '')}"
        if "jira" in entry.get("sources", []):
            advice += (
                " - remove the JIRA link if it was added by mistake, otherwise explain in the "
                "PR description why the order matters"
            )
        actions.append(advice)
    if solid:
        actions.append(
            "state the dependency in the PR description so a committer does not merge "
            "this one out of order"
        )
    for entry in satisfied:
        if entry["state"] == "MERGED":
            actions.append(
                f"{entry['ref']} is merged: rebase on trunk so only this change is left in the diff"
            )
    if pr.get("isDraft"):
        actions.append("take the PR out of draft so reviewers and committers can act on it")
    if pr.get("mergeable") == "CONFLICTING":
        actions.append("rebase onto the latest trunk to clear the merge conflicts, then force-push")

    if yetus and yetus.needs_rebase:
        actions.append(YETUS_ADVICE["patch"])
    elif yetus and yetus.overall == "-1":
        for subsystem in yetus.failures:
            advice = YETUS_ADVICE.get(subsystem)
            actions.append(advice or f"address the Yetus -1 on '{subsystem}'")
    elif yetus_checks.verdict == "fail":
        actions.append("open the Jenkins job for the Yetus run and fix what it reports")

    if actions_group.verdict == "fail":
        actions.append(
            f"check the failing GitHub Actions job(s) ({join(actions_group.failed)}) "
            "and push a fix"
        )
    if actions_group.verdict == "running" or yetus_checks.verdict == "running":
        actions.append("wait for the running checks to finish before asking for a review")

    for gap in gaps:
        actions.append(
            "fill in the PR description: say what changes and how it was tested"
            if "tested" in gap or "empty" in gap
            else f"complete the PR template ({gap})"
        )
        break
    if not title_ok and jira:
        actions.append(f"rename the PR to '{jira.key}. {jira.title}'")

    # GitHub keeps blocking the merge while the review decision is
    # CHANGES_REQUESTED, even when no current review carries that state.
    changes_requested = bool(requesters) or pr.get("reviewDecision") == "CHANGES_REQUESTED"
    if changes_requested:
        who = join(requesters) or join(pending) or "the reviewer"
        actions.append(f"address the changes requested by {who} and re-request the review")
    if threads:
        actions.append("reply to the open review threads and resolve them once addressed")
    if waiting_on and not changes_requested:
        actions.append(f"answer {waiting_on} - the discussion is waiting on you")

    green = actions_group.verdict in ("pass", "none") and (
        yetus_checks.verdict in ("pass", "none") and (not yetus or yetus.overall != "-1")
    )
    if green and not changes_requested:
        if approvers:
            actions.append(
                f"the change is approved by {join(approvers)}: ask a committer to merge it"
                + (
                    f" once {join([d['ref'] for d in solid])} is in"
                    if solid
                    else ", on the PR and on the JIRA"
                )
            )
        elif pending:
            actions.append(f"ping {join(pending)}, the review was requested but never delivered")
        else:
            hints = []
            if reviewer_hints and repo_path:
                author_name = ((pr.get("author") or {}) or {}).get("name") or ""
                hints = module_owners(
                    repo_path,
                    [f["path"] for f in ((pr.get("files") or {}).get("nodes") or [])],
                    [author, author_name],
                )
            who = f" - people active in these files lately: {join(hints)}" if hints else ""
            actions.append(
                f"the PR is green and nobody is reviewing it: request a review from a "
                f"committer for this module{who}"
            )
            actions.append(
                "if it stays quiet for a week, send a short reminder to "
                "common-dev@hadoop.apache.org with the PR link"
            )

    if jira and jira.found:
        if not jira.assignee:
            actions.append(f"assign {jira.key} to yourself")
        if jira.status.lower() in ("open", "in progress") and not jira.resolution:
            actions.append(f"move {jira.key} to Patch Available so reviewers find it")
        if jira.resolution:
            actions.append(
                f"{jira.key} is already {jira.resolution} - confirm the PR is not a duplicate "
                "of what was committed"
            )
        if "pull-request-available" not in jira.labels:
            actions.append(f"link the PR in {jira.key} (the ASF bot then adds pull-request-available)")
    elif jira is None:
        actions.append("file a JIRA issue and rename the PR to 'JIRA-ID. Summary.'")

    idle = days_since(pr.get("updatedAt"))
    if idle is not None and idle >= stale_days and not actions:
        actions.append("rebase on trunk to trigger a fresh precommit run and bring the PR back up the queue")
    if not actions:
        actions.append("nothing blocking found: keep an eye on the checks and wait for a committer")

    # De-duplicate while preserving order.
    actions = list(dict.fromkeys(actions))

    # Nothing of its own is wrong with a blocked PR, so say so in the status
    # rather than letting it look mergeable or merely unreviewed.
    if solid and status in ("READY TO MERGE", "WAITING FOR REVIEW", "CI RUNNING"):
        status = "BLOCKED BY " + ", ".join(d["ref"] for d in solid[:2])

    return Report(
        jira_id=jira_id,
        jira_title=jira_title,
        pr_id=f"#{number}",
        pr_title=pr.get("title", ""),
        status=status,
        comments="; ".join(notes),
        suggestion=" | ".join(f"{i}. {a}" for i, a in enumerate(actions, 1)),
        dependencies=format_dependencies(deps),
        url=pr.get("url", ""),
        jira_url=jira_url,
        details=details,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def wrap(text: str, width: int, indent: str) -> str:
    lines = textwrap.wrap(text, max(30, width - len(indent))) or [""]
    return ("\n" + indent).join(lines)


def render_report(reports: list[Report], width: int) -> str:
    out: list[str] = []
    label = 14
    indent = " " * label
    for report in reports:
        out.append("=" * min(width, 100))
        rows = [
            ("JIRA", f"{report.jira_id}  {report.jira_title}"),
            ("PR", f"{report.pr_id}  {report.pr_title}"),
            ("Status", report.status),
            ("Comments", report.comments),
        ]
        if report.dependencies:
            rows.append(("Depends on", report.dependencies))
        for name, value in rows:
            out.append(f"{name + ':':<{label}}{wrap(value, width, indent)}")
        out.append("Suggestion:")
        for step in report.suggestion.split(" | "):
            out.append(f"  {wrap(step, width - 2, '     ')}")
        links = [link for link in (report.url, report.jira_url) if link]
        if links:
            out.append(f"{'Links:':<{label}}" + "  ".join(links))
    return "\n".join(out)


def render_markdown(reports: list[Report]) -> str:
    lines = [
        "| JIRA | PR | Status | Comments | Depends on | Suggestion |",
        "|---|---|---|---|---|---|",
    ]
    for r in reports:
        jira = f"[{r.jira_id}]({r.jira_url})" if r.jira_url else r.jira_id
        pr = f"[{r.pr_id}]({r.url})" if r.url else r.pr_id
        steps = "<br>".join(r.suggestion.split(" | ")).replace("|", "\\|")
        lines.append(
            f"| {jira} {r.jira_title.replace('|', chr(92) + '|')} | {pr} "
            f"{r.pr_title.replace('|', chr(92) + '|')} | {r.status} | "
            f"{r.comments.replace('|', chr(92) + '|')} | "
            f"{(r.dependencies or '-').replace('|', chr(92) + '|')} | {steps} |"
        )
    return "\n".join(lines)


def render_csv(reports: list[Report], stream) -> None:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        ["JIRA-ID", "JIRA title", "PR ID", "PR title", "Status", "Comments",
         "Depends on", "Suggestion"]
    )
    for r in reports:
        writer.writerow(
            [r.jira_id, r.jira_title, r.pr_id, r.pr_title, r.status, r.comments,
             r.dependencies, r.suggestion]
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", nargs="*", help="PR number(s) and/or JIRA id(s)")
    parser.add_argument(
        "--all-open", nargs="?", const=DEFAULT_AUTHOR, default=None, metavar="AUTHOR",
        help=f"analyse every open PR of an author instead of a list of targets "
             f"(default author: {DEFAULT_AUTHOR})",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"repository (default: {DEFAULT_REPO})")
    parser.add_argument("--jira-base", default=DEFAULT_JIRA, help=f"JIRA base URL (default: {DEFAULT_JIRA})")
    parser.add_argument("--stale-days", type=int, default=14, help="idle days before the PR is called stale (default: 14)")
    parser.add_argument("--repo-path", default=None, help=f"clone used to suggest reviewers (default: {DEFAULT_REPO_PATH})")
    parser.add_argument("--no-reviewer-hints", action="store_true", help="do not look in git history for people to ask for a review")
    parser.add_argument("--no-deps", action="store_true", help="skip the search for dependencies on other pull requests")
    parser.add_argument("--no-ci-search", action="store_true", help="do not look for PRs of others or JIRA issues that fix the failures none of your PRs clears")
    parser.add_argument("--no-diffs", action="store_true", help="do not download the diffs; dependencies are then reported as declared, without a verdict")
    parser.add_argument("--format", choices=("report", "markdown", "csv", "json"), default="report")
    parser.add_argument("--width", type=int, default=None, help="report width (default: terminal width)")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    return parser.parse_args(argv)


def resolve_target(
    target: str, repo: str, jira_base: str, token: str | None
) -> tuple[Jira | None, dict[str, Any] | None, list[dict]]:
    candidates: list[dict] = []
    if target.isdigit():
        pr = fetch_pr(repo, int(target), token)
        if pr is None:
            raise SystemExit(f"{repo} has no pull request #{target}.")
        match = JIRA_IN_TEXT_RE.search(pr.get("title") or "")
        jira = fetch_jira(jira_base, match.group(0).upper()) if match else None
        return jira, pr, candidates

    if not JIRA_ID_RE.match(target):
        raise SystemExit(
            f"'{target}' is neither a PR number nor a JIRA id such as HADOOP-19987."
        )
    key = target.upper()
    jira = fetch_jira(jira_base, key)
    number, candidates = find_pr_for_jira(repo, key, token)
    pr = fetch_pr(repo, number, token) if number else None
    return jira, pr, candidates


def main(argv: list[str] | None = None) -> int:
    # JIRA names and PR bodies are full of non-ASCII characters.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args = parse_args(argv)
    token = resolve_token(args.token)
    repo_path = args.repo_path or (DEFAULT_REPO_PATH if os.path.isdir(DEFAULT_REPO_PATH) else None)
    if repo_path and not shutil.which("git"):
        repo_path = None

    targets = list(args.target)
    if args.all_open:
        numbers = sorted((pr["number"] for pr in fetch_peer_prs(args.repo, args.all_open, token)),
                         reverse=True)
        targets += [str(n) for n in numbers if str(n) not in targets]
        if not targets:
            raise SystemExit(f"{args.all_open} has no open pull request in {args.repo}.")
    if not targets:
        raise SystemExit("give a PR number or a JIRA id, or use --all-open.")

    reports: list[Report] = []
    for target in targets:
        jira, pr, candidates = resolve_target(target, args.repo, args.jira_base, token)
        deps = None
        if pr is not None and pr.get("state") == "OPEN" and not args.no_deps:
            deps = collect_dependencies(
                pr, jira, args.repo, token, repo_path, use_diffs=not args.no_diffs,
                jira_base=args.jira_base, search_external=not args.no_ci_search,
            )
        report = analyse(
            jira, pr, args.repo, args.jira_base, repo_path, args.stale_days,
            not args.no_reviewer_hints, deps,
        )
        if len(candidates) > 1 and pr is not None:
            others = ", ".join(
                f"#{c['number']} ({c['state'].lower()})"
                for c in candidates if c["number"] != pr["number"]
            )
            report.comments += f"; other PRs mention this JIRA: {others}"
        reports.append(report)

    if args.format == "json":
        json.dump([asdict(r) for r in reports], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.format == "csv":
        render_csv(reports, sys.stdout)
        return 0
    if args.format == "markdown":
        print(render_markdown(reports))
        return 0
    width = args.width or shutil.get_terminal_size((100, 24)).columns
    print(render_report(reports, max(70, width)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
