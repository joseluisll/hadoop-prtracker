#!/usr/bin/env python3
"""Propose JIRA issues for what the nightly Apache Hadoop qbt report shows broken on trunk.

The qbt ("quality build trunk") jobs on ci-hadoop.apache.org run the whole
Yetus precommit against trunk every night or two, once per JDK
(hadoop-qbt-trunk-java17-linux-x86_64, ...-java21-...). Their report is what
the "Apache Hadoop qbt Report: trunk+JDK17 on Linux/x86_64" mails carry.
This script reads the latest report of each of those jobs and the builds
before it, and turns what they show into JIRA candidates:

* a unit test class that fails (one candidate per class, its methods listed);
* a Maven plugin goal that fails on a module, so its unit vote is -1 without
  a test failing (a Jasmine run, an enforcer rule);
* trunk spotbugs warnings, grouped by the module whose source has them;
* with --include-lint, a tree-wide -1 such as xml or pathlen.

The output starts with every candidate ranked by the open PRs it would help:
those whose latest precommit has a -1 that fixing it clears, or is part of.
A PR that changes a root file (a LICENSE, hadoop-project/pom.xml) gets
spotbugs run over the whole repo and a -1 for its ~90 old warnings; one of
those is no help to it, so it does not count.

No JIRA is proposed for a candidate somebody already works on: an open pull
request, or one merged since the failing build, that the same matching as
analyze_pr.py says fixes it, or a JIRA issue unresolved (or resolved since
that build) whose summary names it. The ranking still lists it when it helps
open PRs, with the PR that fixes it ("a rebase picks it up" once merged).
The others are proposed, in the same order, with the reasons for their
priority, the JIRA they would become, and the evidence.

*This is a dry run, always.* Nothing is written to JIRA or GitHub. With
--save-dir each proposal's description is written to a file, together with
the create_jira.py command that would file it, for you to run if you agree.

Priority
--------
Every candidate gets points, all shown with the reason:

    open PR whose latest precommit has it:
        fixing it clears that -1               +10 each
        it is part of the -1 on its module      +5 each
        it is one of the ~90 warnings of a       0
          whole-repo spotbugs run
    open PR that had it in an earlier run      +3 each (whole-repo runs: 0)
        (the PR points are capped at 50)
    open PR that changes the module, so its    +1 each, at most 10
        next precommit will run into it
    kind: plugin goal fails (module untested)  +10
          unit test fails                      +6
          spotbugs correctness/security/MT     +5
          other spotbugs warning               +2
          tree-wide lint                        0
    nightly builds it failed in                +2 each, at most 14  (tests and
    failed in every build read, 3 or more      +5    builds only: deterministic,
                                                     not flaky)
    fails with more than one JDK               +5   (not for lint)
    new: absent from an earlier build read     +4   (the commits in between
                                                     are named as suspects)
    reported only by an aggregate module run   -5   (its own module is clean)

    P1 >= 40, P2 >= 20, P3 below.

Examples
--------
    python qbt_jira.py                          # every trunk qbt job, dry run
    python qbt_jira.py --job hadoop-qbt-trunk-java21-linux-x86_64 --build 113
    python qbt_jira.py --format markdown --show-discarded
    python qbt_jira.py --save-dir proposals     # one description file each
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import analyze_pr as core
from list_upstream_prs import DEFAULT_BOTS, graphql, join, resolve_token

DEFAULT_JENKINS = "https://ci-hadoop.apache.org"
# The trunk jobs on Linux; java8 and java11 are kept but no longer built.
TRUNK_JOB_RE = re.compile(r"^hadoop-qbt-trunk-java\d+-linux-x86_64$")
HISTORY = 7
PR_DAYS = 90

POINTS = {
    "pr_clears": 10, "pr_own": 5, "pr_earlier": 3, "pr_cap": 50,
    "pr_exposed": 1, "pr_exposed_cap": 10,
    "kind_build": 10, "kind_test": 6, "kind_spotbugs_bug": 5, "kind_spotbugs": 2,
    "build": 2, "build_cap": 14, "deterministic": 5, "jdks": 5, "new": 4, "aggregate_only": -5,
}
TIERS = ((40, "P1"), (20, "P2"), (0, "P3"))
# spotbugs categories that are bugs rather than style.
BUG_CATEGORIES = {"CORRECTNESS", "MT_CORRECTNESS", "SECURITY"}
# Checks the qbt runs over the whole tree; precommit only runs them on a patch.
LINT = {"blanks", "pathlen", "xml", "hadolint", "shellcheck", "pylint", "checkstyle",
        "javadoc", "javac", "cc", "asflicense", "codespell", "yamllint", "markdownlint"}


# --------------------------------------------------------------------------- #
# The qbt report
# --------------------------------------------------------------------------- #
SPECIFIC_RE = re.compile(r"^ {4}(\S[^:]*?)\s*:\s*$")
LINKS_RE = re.compile(r"^ {3}(\S[^:]*?):\s*$")
ITEM_RE = re.compile(r"^ {6,}(\S.*?)\s*$")
CONSOLE_ROW_RE = re.compile(r"^\|\s*([+-]?\d)\s*\|\s*(\S*)\s*\|[^|]*\|(.*)$")
CONSOLE_MORE_RE = re.compile(r"^\|\s*\|\s*\|\s*\|(.*)$")
REVISION_RE = re.compile(r"^\|\s*git revision\s*\|\s*\S+\s*/\s*([0-9a-f]{7,40})", re.M)


def short_test(name: str) -> str:
    """'org.apache.hadoop.x.TestY$Inner' -> 'hadoop.x.TestY', as Yetus prints it."""
    return re.sub(r"^org\.apache\.", "", name.split("$")[0])


def module_of_log(url: str) -> str:
    """'.../patch-unit-hadoop-tools_hadoop-distcp.txt' -> 'hadoop-tools/hadoop-distcp'."""
    name = os.path.basename(urllib.parse.urlparse(url).path)
    match = re.match(r"patch-unit-(.+)\.txt$", name)
    return match.group(1).replace("_", "/") if match else ""


def parse_email_report(text: str) -> dict[str, Any]:
    """The parts of email-report.txt this script uses.

    voted: subsystems that voted -1 (not the filtered ones); tests: failed or
    timed-out test classes; spotbugs: module -> text of its warnings;
    links: subsystem -> report URLs; unit_modules: modules whose unit run failed.
    """
    report: dict[str, Any] = {"voted": [], "filtered": [], "tests": [], "spotbugs": {},
                              "links": {}, "unit_modules": []}
    lines = text.splitlines()
    section, module, mode = "", "", ""
    for index, line in enumerate(lines):
        if line.startswith("The following subsystems voted -1"):
            target = "filtered" if line.rstrip().endswith("but") else "voted"
            following = lines[index + 2 if target == "filtered" else index + 1:]
            for item in following:
                if not item.strip():
                    break
                if item.startswith("    "):
                    report[target] += item.split()
            continue
        header = SPECIFIC_RE.match(line)
        if header and header.group(1) != "module":
            section, module, mode = header.group(1).lower(), "", "specific"
            continue
        header = LINKS_RE.match(line)
        if header:
            section, module, mode = header.group(1).lower(), "", "links"
            continue
        item = ITEM_RE.match(line)
        if not item or not section:
            continue
        value = item.group(1)
        if mode == "links":
            url = value.split()[0]
            if url.startswith("http"):
                report["links"].setdefault(section, []).append(url)
        elif "junit tests" in section:
            report["tests"].append(short_test(value))
        elif section == "spotbugs":
            if value.startswith("module:"):
                module = value[len("module:"):].strip()
                report["spotbugs"].setdefault(module, "")
            elif module:
                report["spotbugs"][module] += value + "\n"
    report["tests"] = list(dict.fromkeys(report["tests"]))
    report["unit_modules"] = [m for m in (module_of_log(u) for u in report["links"].get("unit", []))
                              if m]
    return report


def parse_console_report(text: str) -> dict[str, Any]:
    """git revision, and the comment of every -1 row: subsystem -> [comments]."""
    # A long comment wraps onto rows with empty vote and subsystem cells, cut
    # mid-word: the pieces are joined as they are.
    comments: dict[str, list[str]] = {}
    subsystem, parts = "", []

    def flush() -> None:
        if subsystem:
            comments.setdefault(subsystem, []).append(" ".join("".join(parts).split()))
    for line in text.splitlines():
        row = CONSOLE_ROW_RE.match(line)
        more = CONSOLE_MORE_RE.match(line)
        if row:
            flush()
            subsystem, parts = (row.group(2), [row.group(3)]) if row.group(1) == "-1" else ("", [])
        elif more and subsystem:
            parts.append(more.group(1))
        else:
            flush()
            subsystem, parts = "", []
    flush()
    revision = REVISION_RE.search(text)
    return {"revision": revision.group(1) if revision else "", "comments": comments}


def parse_spotbugs_xml(text: str) -> list[dict[str, Any]]:
    """One dict per BugInstance of a spotbugs XML report."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    warnings = []
    for bug in root.iter("BugInstance"):
        cls = bug.find("Class")
        method, fld = bug.find("Method"), bug.find("Field")
        line = bug.find("SourceLine")
        source = (cls.find("SourceLine") if cls is not None else None)
        warnings.append({
            "type": bug.get("type", ""),
            "category": bug.get("category", ""),
            "rank": int(bug.get("rank") or 20),
            "class": cls.get("classname", "") if cls is not None else "",
            "method": method.get("name", "") if method is not None else "",
            "field": fld.get("name", "") if fld is not None else "",
            "line": (line.get("start") or "") if line is not None else "",
            "sourcepath": (source.get("sourcepath") or "") if source is not None else "",
        })
    return warnings


def warning_id(warning: dict[str, Any]) -> str:
    return f"{warning['type']}|{warning['class']}|{warning['method']}|{warning['field']}"


def warning_text(warning: dict[str, Any]) -> str:
    short = warning["class"].rsplit(".", 1)[-1]
    where = f"{short}.{warning['method']}()" if warning["method"] else short
    if warning["field"] and not warning["method"]:
        where = f"{short}.{warning['field']}"
    line = f" line {warning['line']}" if warning.get("line") else ""
    return f"{warning['type']} in {where}{line}"


# --------------------------------------------------------------------------- #
# Jenkins
# --------------------------------------------------------------------------- #
def jenkins_json(url: str, tree: str) -> dict[str, Any]:
    data = core.http_get(f"{url.rstrip('/')}/api/json?tree={urllib.parse.quote(tree, safe=',')}")
    try:
        return json.loads(data) if data else {}
    except ValueError:
        return {}


def jenkins_text(url: str, limit: int = 80_000_000) -> str | None:
    data = core.http_get(url, limit=limit)
    return data.decode("utf-8", "replace") if data is not None else None


def trunk_jobs(jenkins: str) -> list[str]:
    jobs = jenkins_json(jenkins, "jobs[name,buildable]").get("jobs") or []
    return sorted(j["name"] for j in jobs if TRUNK_JOB_RE.match(j.get("name") or "")
                  and j.get("buildable"))


def jdk_of(job: str) -> str:
    match = re.search(r"java(\d+)", job)
    return f"JDK {match.group(1)}" if match else job


def job_builds(jenkins: str, job: str, history: int, build: int | None) -> list[dict[str, Any]]:
    """The completed builds to read, newest first: the one asked for (or the
    latest) and up to history-1 before it."""
    tree = "builds[number,result,timestamp,url,building,changeSet[items[commitId,msg]]]{0,60}"
    builds = [b for b in jenkins_json(f"{jenkins}/job/{job}", tree).get("builds") or []
              if not b.get("building") and b.get("result") not in (None, "ABORTED", "NOT_BUILT")]
    if build is not None:
        builds = [b for b in builds if b["number"] <= build]
        if not builds or builds[0]["number"] != build:
            raise SystemExit(f"{job} #{build} is not a completed build Jenkins still keeps")
    return builds[:history]


def read_build(job: str, build: dict[str, Any], latest: bool) -> dict[str, Any] | None:
    """What one build reports. The latest also gets its spotbugs XML, unit logs
    and test report; the older ones only their email report."""
    url = build["url"].rstrip("/") + "/"
    out = url + "artifact/out/"
    email = core.disk_cached(f"qbt-email-{job}-{build['number']}",
                             lambda: jenkins_text(out + "email-report.txt", 5_000_000))
    if not email:
        return None
    report = parse_email_report(email)
    record = {
        "job": job, "number": build["number"], "url": url,
        "date": datetime.datetime.fromtimestamp(build["timestamp"] / 1000,
                                                datetime.timezone.utc).date().isoformat(),
        "commits": [{"sha": (i.get("commitId") or "")[:10], "msg": (i.get("msg") or "").strip()}
                    for i in ((build.get("changeSet") or {}).get("items") or [])],
        **report,
    }
    if not latest:
        return record
    console = core.disk_cached(f"qbt-console-{job}-{build['number']}",
                               lambda: jenkins_text(out + "console-report.txt", 5_000_000)) or ""
    record.update(parse_console_report(console))
    record["warnings"] = {}
    for module in report["spotbugs"]:
        xml_url = f"{out}branch-spotbugs-{module.replace('/', '_')}-warnings.xml"
        record["warnings"][module] = core.disk_cached(
            f"qbt-spotbugs-{job}-{build['number']}-{module}",
            lambda xml_url=xml_url: spotbugs_warnings(xml_url)) or []
    record["logs"] = {}
    for log in report["links"].get("unit", []):
        record["logs"][module_of_log(log)] = (core.build_log_failures(log) or {}) | {"url": log}
    record["cases"] = core.disk_cached(f"qbt-tests-{job}-{build['number']}",
                                       lambda: failed_cases(url)) or {}
    return record


def spotbugs_warnings(url: str) -> list[dict[str, Any]] | None:
    text = jenkins_text(url)
    return parse_spotbugs_xml(text) if text else None


def failed_cases(build_url: str) -> dict[str, list[dict[str, Any]]] | None:
    data = jenkins_json(build_url + "testReport",
                        "suites[cases[className,name,status,age,errorDetails]]")
    if not data:
        return None
    cases: dict[str, list[dict[str, Any]]] = {}
    for suite in data.get("suites") or []:
        for case in suite.get("cases") or []:
            if case.get("status") in ("FAILED", "REGRESSION"):
                cases.setdefault(short_test(case.get("className") or ""), []).append({
                    "name": case.get("name") or "", "age": case.get("age") or 0,
                    "error": " ".join((case.get("errorDetails") or "").split())[:300],
                })
    return cases


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
def source_modules(repo_path: str | None) -> dict[str, str]:
    """'org/apache/x/Y.java' -> the module whose src/main/java has it, from the clone."""
    ref = core.resolve_base_ref(repo_path)
    if not ref:
        return {}
    code, out = core.git_run(repo_path, "ls-tree", "-r", "--name-only", ref)
    found: dict[str, str] = {}
    if code == 0:
        for path in out.splitlines():
            module, sep, rest = path.partition("/src/main/java/")
            if sep:
                found.setdefault(rest, module)
    return found


def owner_of(warning: dict[str, Any], listed_in: list[str], sizes: dict[str, int],
             sources: dict[str, str]) -> str:
    """The module a warning belongs to: by its source file in the clone, else
    the most specific module report listing it, the smallest one on a tie."""
    if warning["sourcepath"] in sources:
        return sources[warning["sourcepath"]]
    leaves = [m for m in listed_in if m != "root"
              and not any(o != m and o.startswith(m + "/") for o in listed_in)]
    return min(leaves or listed_in, key=lambda m: (sizes.get(m, 0), -m.count("/")))


def project_key(module: str, test: str = "") -> str:
    if test:
        return core.JIRA_PROJECT_OF_TREE.get(core._project_of_test(test), "") \
            or core.JIRA_PROJECT_OF_TREE.get(module.split("/", 1)[0], "HADOOP")
    return core.JIRA_PROJECT_OF_TREE.get(module.split("/", 1)[0], "HADOOP")


def build_candidates(runs: dict[str, list[dict[str, Any]]], sources: dict[str, str],
                     include_lint: bool) -> list[dict[str, Any]]:
    """One candidate per failing test class, failing plugin goal, spotbugs
    owner module (and lint subsystem), merged across the jobs' latest builds."""
    found: dict[tuple[str, ...], dict[str, Any]] = {}

    def add(key: tuple[str, ...], job: str, **data: Any) -> dict[str, Any]:
        entry = found.setdefault(key, {"kind": key[0], "jobs": [], **data})
        if job not in entry["jobs"]:
            entry["jobs"].append(job)
        return entry

    for job, builds in runs.items():
        latest = builds[0]
        test_module = {t: m for m, log in latest["logs"].items() for t in log.get("tests", [])}
        for test in dict.fromkeys(latest["tests"] + list(test_module)):
            module = test_module.get(test, "")
            entry = add(("test", test), job, test=test, module=module, methods={}, logs=[])
            entry["module"] = entry["module"] or module
            for case in latest["cases"].get(test, []):
                entry["methods"].setdefault(case["name"], case)
            if module and latest["logs"][module]["url"] not in entry["logs"]:
                entry["logs"].append(latest["logs"][module]["url"])
        for module, log in latest["logs"].items():
            for plugin, artifact in log.get("goals", []):
                entry = add(("build", module, plugin), job, module=module, plugin=plugin,
                            artifact=artifact, logs=[])
                if log["url"] not in entry["logs"]:
                    entry["logs"].append(log["url"])
        # Spotbugs: every report lists the warnings of the modules below it too.
        listed: dict[str, list[str]] = {}
        sizes = {m: len(w) for m, w in latest["warnings"].items()}
        by_id: dict[str, dict[str, Any]] = {}
        for module, warnings in latest["warnings"].items():
            for warning in warnings:
                by_id.setdefault(warning_id(warning), warning)
                listed.setdefault(warning_id(warning), []).append(module)
        for wid, warning in by_id.items():
            owner = owner_of(warning, listed[wid], sizes, sources)
            entry = add(("spotbugs", owner), job, module=owner, warnings={}, reported_in={})
            entry["warnings"][wid] = warning
            for module in listed[wid]:
                entry["reported_in"].setdefault(module, set()).add(wid)
        if include_lint:
            for subsystem in latest["voted"]:
                if subsystem in LINT:
                    entry = add(("lint", subsystem), job, subsystem=subsystem, module="",
                                comments=[])
                    for comment in latest.get("comments", {}).get(subsystem, []):
                        if comment not in entry["comments"]:
                            entry["comments"].append(comment)

    # What each module's report holds over all jobs, to tell a candidate
    # that clears a -1 from one that is part of it.
    report_size: dict[str, set[str]] = {}
    for entry in found.values():
        for module, ids in (entry.get("reported_in") or {}).items():
            report_size.setdefault(module, set()).update(ids)
    for entry in found.values():
        if entry["kind"] == "spotbugs":
            entry["clears"] = sorted(m for m, ids in entry["reported_in"].items()
                                     if report_size[m] <= set(entry["warnings"]))
            entry["aggregate_only"] = entry["module"] not in entry["reported_in"]
            entry["report_total"] = {m: len(report_size[m]) for m in entry["reported_in"]}
        entry["record"] = failure_record(entry)
    return list(found.values())


def failure_record(entry: dict[str, Any]) -> dict[str, Any]:
    """The candidate as analyze_pr.py describes a CI failure, for its searches."""
    kind = entry["kind"]
    if kind == "test":
        return {"subsystem": "unit", "test": entry["test"], "module": entry["module"],
                "detail": f"{entry['test'].rsplit('.', 1)[-1]} fails"}
    if kind == "build":
        return {"subsystem": "build", "project": entry["artifact"], "plugin": entry["plugin"],
                "detail": f"{entry['plugin']} fails on {entry['artifact']}"}
    if kind == "spotbugs":
        classes: dict[str, list[str]] = {}
        for warning in entry["warnings"].values():
            types = classes.setdefault(warning["class"], [])
            if warning["type"] not in types:
                types.append(warning["type"])
        return {"subsystem": "spotbugs", "module": entry["module"], "classes": classes,
                "warning_count": len(entry["warnings"]),
                "detail": f"spotbugs on {entry['module']}"}
    return {"subsystem": entry["subsystem"], "detail": "; ".join(entry["comments"][:1])
            or f"{entry['subsystem']} votes -1 on trunk"}


def present_in(entry: dict[str, Any], build: dict[str, Any]) -> bool:
    """Does an (older) build report the candidate? Read from its email only."""
    kind = entry["kind"]
    if kind == "test":
        return entry["test"] in build["tests"]
    if kind == "build":
        return entry["module"] in build["unit_modules"]
    if kind == "lint":
        return entry["subsystem"] in build["voted"]
    text = "".join(build["spotbugs"].values())
    return any(w["class"].replace("$", ".") in text.replace("$", ".")
               for w in entry["warnings"].values())


def history(entry: dict[str, Any], runs: dict[str, list[dict[str, Any]]]) -> None:
    """Per job: how many builds read show it, the streak up to the latest, and
    the build it appeared in when an older one did not have it."""
    entry["history"] = {}
    for job, builds in runs.items():
        seen = [present_in(entry, b) for b in builds]  # newest first
        if not any(seen):
            entry["history"][job] = {"failed": 0, "read": len(builds)}
            continue
        streak = next((i for i, s in enumerate(seen) if not s), len(seen))
        regression = None
        if 0 < streak < len(seen) and not any(seen[streak:]):
            # Absent before, present ever since: the commits of the build it
            # appeared in (Jenkins lists those since the previous build) did it.
            appeared = builds[streak - 1]
            regression = {"build": appeared["number"], "date": appeared["date"],
                          "after": builds[streak]["number"], "suspects": appeared["commits"]}
        entry["history"][job] = {"failed": sum(seen), "read": len(builds), "streak": streak,
                                 "latest_date": builds[0]["date"] if seen[0] else "",
                                 "regression": regression}


# --------------------------------------------------------------------------- #
# Open pull requests: what their precommit says, what they change
# --------------------------------------------------------------------------- #
OPEN_PRS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 15, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft updatedAt author { login }
        files(first: 100) { nodes { path } }
        comments(last: 25) { nodes { author { login } createdAt body } }
      }
    }
  }
}
"""
EXTANT_RE = re.compile(r"^\s*([\w.-]+(?:/[\w.-]+)*)\s+in\s+trunk\s+has\s+(\d+)\s+extant", re.I)


def fetch_open_prs(repo: str, days: int, token: str | None) -> list[dict[str, Any]]:
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    query = f"repo:{repo} is:pr is:open base:trunk updated:>={since}"
    prs, after = [], None
    while True:
        data = graphql(OPEN_PRS_QUERY, {"q": query, "after": after}, token)["search"]
        prs += [n for n in data["nodes"] or [] if n]
        if not data["pageInfo"]["hasNextPage"]:
            return prs
        after = data["pageInfo"]["endCursor"]


def precommit_of(pr: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{'latest': ..., 'earlier': ...}: failed tests, unit -1 modules and
    'in trunk has N extant' spotbugs modules of its Yetus comments."""
    bots = {b.lower() for b in DEFAULT_BOTS}
    own = {os.path.basename(f["path"]) for f in ((pr.get("files") or {}).get("nodes") or [])}
    comments = sorted((c for c in ((pr.get("comments") or {}).get("nodes") or [])
                       if ((c.get("author") or {}).get("login") or "").lower() in bots
                       and "overall" in (c.get("body") or "")),
                      key=lambda c: c.get("createdAt") or "")
    result = {name: {"tests": set(), "unit": set(), "spotbugs": set()}
              for name in ("latest", "earlier")}
    for index, comment in enumerate(comments):
        into = result["latest" if index == len(comments) - 1 else "earlier"]
        in_tests = False
        for line in (comment.get("body") or "").splitlines():
            tests = core.YETUS_TESTS_RE.match(line)
            if tests and (tests.group(1) or in_tests):
                in_tests = True
                if f"{tests.group(2).rsplit('.', 1)[-1]}.java" not in own:
                    into["tests"].add(short_test(tests.group(2)))
                continue
            in_tests = False
            if not core.YETUS_ROW_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            subsystem = cells[1].lower()
            if subsystem == "unit":
                link = re.search(r"\((https?://[^)\s]+)\)", cells[3])
                if link and module_of_log(link.group(1)):
                    into["unit"].add(module_of_log(link.group(1)))
            elif subsystem == "spotbugs":
                extant = EXTANT_RE.match(cells[-1])
                if extant:
                    into["spotbugs"].add(extant.group(1))
    result["earlier"] = {k: v - result["latest"][k] for k, v in result["earlier"].items()}
    return result


def pr_impact(entry: dict[str, Any], prs: list[dict[str, Any]]) -> None:
    """The open PRs whose precommit has this candidate, and those that change
    its module and so will run into it."""
    entry["blocked"], entry["exposed"] = [], []
    for pr in prs:
        ci = pr["_precommit"]
        found = None
        for when in ("latest", "earlier"):
            run = ci[when]
            if entry["kind"] == "test" and entry["test"] in run["tests"]:
                # Other tests failing too keep the unit vote -1 after this one is fixed.
                found = (when, "clears" if run["tests"] <= {entry["test"]} else "own",
                         entry["test"].rsplit(".", 1)[-1])
            elif entry["kind"] == "build" and entry["module"] in run["unit"]:
                found = (when, "own" if run["tests"] else "clears",
                         f"unit -1 on {entry['module']}")
            elif entry["kind"] == "spotbugs":
                hit = sorted(set(entry["reported_in"]) & run["spotbugs"])
                cleared = [m for m in hit if m in entry["clears"]]
                if cleared:
                    found = (when, "clears", f"clears the spotbugs -1 on {join(cleared, 2)}")
                elif entry["module"] in hit:
                    found = (when, "own", f"{len(entry['reported_in'][entry['module']])} of the "
                                          f"{entry['report_total'][entry['module']]} warnings of "
                                          f"the spotbugs -1 on {entry['module']}")
                elif hit:
                    # 'root has 94 extant warnings': one of them is not what keeps it red.
                    found = (when, "aggregate", "; ".join(
                        f"{len(entry['reported_in'][m])} of the {entry['report_total'][m]} "
                        f"warnings of the spotbugs -1 on {m}" for m in hit[:2]))
            if found:
                break
        info = {"number": pr["number"], "url": pr.get("url") or "",
                "author": ((pr.get("author") or {}) or {}).get("login", "")}
        if found:
            entry["blocked"].append({**info, "when": found[0], "share": found[1],
                                     "what": found[2]})
            continue
        module = entry.get("module") or ""
        if module and entry["kind"] != "lint" and not entry.get("aggregate_only") and any(
                f["path"].startswith(module + "/")
                for f in ((pr.get("files") or {}).get("nodes") or [])):
            entry["exposed"].append(info)


# --------------------------------------------------------------------------- #
# Already tracked?
# --------------------------------------------------------------------------- #
def tracking(entry: dict[str, Any], repo: str, token: str | None,
             jira_base: str) -> dict[str, list[dict[str, Any]]]:
    """PRs and JIRAs that already address the candidate.

    A fix merged, or an issue resolved, before the latest build that still
    fails did not fix it, so only those since then count, plus open ones.
    A JIRA counts when its summary names the failure, a PR when the matching
    of analyze_pr.py calls it a fix rather than a mention in its description,
    and its latest precommit does not refute it. Its title or description
    is only the lead: each PR carries what its precommit shows ('verified',
    'partial', 'unverified'). The refuted ones go to 'related'.
    """
    since = max((h.get("latest_date") or "" for h in entry["history"].values()), default="")
    record = {**entry["record"], "first_seen": since}
    variants = [record]
    if entry["kind"] == "spotbugs":
        variants.append({k: v for k, v in record.items() if k != "classes"})
    if entry["kind"] == "lint":
        words = [[entry["subsystem"], "trunk"], [entry["subsystem"], "qbt"]]
        jiras = [j for w in words for j in core.search_jira_issues(jira_base, w, since)
                 if all(x.lower() in j["summary"].lower() for x in w)]
        return {"prs": [], "jiras": list({j["key"]: j for j in jiras}.values()), "related": []}
    prs: dict[int, dict[str, Any]] = {}
    jiras: dict[str, dict[str, Any]] = {}
    related: list[dict[str, Any]] = []
    for variant in variants:
        fixes = core.existing_fixes(variant, repo, token, jira_base, set(), set())
        for pr in fixes["prs"]:
            if pr["strength"] == "strong" or not pr["reason"].startswith("its description"):
                prs.setdefault(pr["number"], pr)
            else:
                related.append(pr)
        related += [pr for pr in fixes["refuted"] if pr["number"] not in prs]
        words = core.failure_words(variant)
        for issue in fixes["jiras"]:
            if any(all(w.lower() in issue["summary"].lower() for w in group) for group in words):
                jiras.setdefault(issue["key"], issue)
            else:
                related.append(issue)
    return {"prs": list(prs.values()), "jiras": list(jiras.values()), "related": related}


# --------------------------------------------------------------------------- #
# The open PRs a fix would help
# --------------------------------------------------------------------------- #
def aggregate_total(entry: dict[str, Any]) -> int:
    """The warnings of the biggest spotbugs report that lists it (root: ~90)."""
    return max((entry.get("report_total") or {}).values(), default=0)


def helps(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Open PRs whose latest precommit -1 a fix clears, or is part of.

    A PR that changes a root file gets spotbugs run over the whole repo, and
    a -1 for its ~90 old warnings: fixing one of them helps that PR in no way.
    The PRs that fix it are not helped by it either; they are the fix.
    """
    fixers = {p["number"] for p in (entry.get("tracking") or {}).get("prs", [])}
    return [b for b in entry["blocked"] if b["when"] == "latest"
            and b["share"] in ("clears", "own") and b["number"] not in fixers]


def status_of(entry: dict[str, Any], args: argparse.Namespace, proposal: int = 0) -> str:
    found = entry["tracking"]
    verdicts = {p.get("verified", "unverified") for p in found["prs"]}
    merged = [p for p in found["prs"] if p["state"] == "MERGED"]
    if merged:
        how = {"verified": "already fixed", "partial": "partly fixed"}.get(
            merged[0].get("verified"), "probably fixed (not verified)")
        refs = join([f"#{p['number']} (merged {p['merged']})" for p in merged])
        return f"{how} on trunk by {refs}: a rebase of the PRs picks it up"
    if found["prs"]:
        how = "being fixed by" if verdicts & {"verified", "partial"} \
            else "maybe being fixed (not verified) by"
        return f"{how} {tracked_by(entry, args)}"
    if found["jiras"]:
        return f"tracked by {tracked_by(entry, args)}"
    return f"nobody works on it: proposal [{proposal}] below" if proposal else "nobody works on it"


def checks_of(entry: dict[str, Any]) -> list[str]:
    """What the precommit of each PR that claims to fix it shows."""
    found = entry["tracking"]
    claims = found["prs"] + [p for p in found["related"] if p.get("verified") == "refuted"]
    return [f"#{p['number']} {p['verified']}: {p['evidence']}" for p in claims if p.get("evidence")]


def pr_list(items: list[dict[str, Any]], limit: int = 12) -> str:
    return join([f"#{b['number']} @{b['author']}"
                 + (" (one of its failures)" if b["share"] == "own" else "")
                 for b in items], limit)


def rank_key(entry: dict[str, Any]) -> tuple[int, int, str, str]:
    """Most open PRs helped first, then the score."""
    return -len(entry["helps"]), -entry["score"], entry["project"], entry["summary"]


# --------------------------------------------------------------------------- #
# Priority
# --------------------------------------------------------------------------- #
def score(entry: dict[str, Any]) -> tuple[int, str, list[tuple[int, str]]]:
    """(points, tier, [(points, reason)]) - see the module docstring."""
    reasons: list[tuple[int, str]] = []

    def add(points: int, text: str, info: bool = False) -> None:
        if points or info:
            reasons.append((points, text))

    def prs(items: list[dict[str, Any]]) -> str:
        return join([f"#{p['number']}" for p in items], 6)

    def blocked(when: str, share: str | None = None) -> list[dict[str, Any]]:
        return [b for b in entry["blocked"] if b["when"] == when
                and (share is None or b["share"] == share)]

    pr_points = 0
    for items, points, text in (
            (blocked("latest", "clears"), POINTS["pr_clears"],
             "fixing it clears a -1 in the latest precommit of open PR(s)"),
            (blocked("latest", "own"), POINTS["pr_own"],
             "part of a -1 on its module in the latest precommit of open PR(s)"),
            ([b for b in blocked("earlier") if b["share"] != "aggregate"], POINTS["pr_earlier"],
             "in an earlier precommit of open PR(s)")):
        if items:
            gained = min(points * len(items), POINTS["pr_cap"] - pr_points)
            pr_points += gained
            add(gained, f"{text}: {prs(items)}")
    if blocked("latest", "aggregate"):
        add(0, f"only among the {aggregate_total(entry)} warnings of the whole-repo spotbugs run "
               f"of open PR(s) {prs(blocked('latest', 'aggregate'))}: fixing it alone does not "
               f"clear their -1", info=True)
    if entry["exposed"]:
        add(min(POINTS["pr_exposed"] * len(entry["exposed"]), POINTS["pr_exposed_cap"]),
            f"{len(entry['exposed'])} open PR(s) change {entry['module']}, so their next "
            f"precommit runs into it: {prs(entry['exposed'])}")

    kind = entry["kind"]
    if kind == "build":
        add(POINTS["kind_build"], f"{entry['plugin']} fails, so the unit run of "
                                  f"{entry['artifact']} is -1 for every PR that touches it")
    elif kind == "test":
        add(POINTS["kind_test"], "a unit test fails")
    elif kind == "spotbugs":
        bugs = [w for w in entry["warnings"].values() if w["category"] in BUG_CATEGORIES]
        if bugs:
            add(POINTS["kind_spotbugs_bug"],
                f"{len(bugs)} warning(s) in a bug category ({join(sorted({w['category'] for w in bugs}))})")
        else:
            add(POINTS["kind_spotbugs"], "spotbugs warnings of style or bad practice")
        if entry["aggregate_only"]:
            add(POINTS["aggregate_only"],
                f"only reported by aggregate runs ({join(sorted(entry['reported_in']), 2)}); "
                f"the run of {entry['module']} itself is clean")

    # Flaky or steady tells a lot about a test or a build; a spotbugs warning
    # is there every time, and a tree-wide lint count blocks no PR at all.
    runtime = kind in ("test", "build")
    failed = sum(h["failed"] for h in entry["history"].values())
    add(min(POINTS["build"] * failed, POINTS["build_cap"]) if runtime else 0,
        f"failed in {failed} nightly build(s): " + "; ".join(
            f"{jdk_of(job)} {h['failed']} of {h['read']}"
            for job, h in entry["history"].items()), info=True)
    steady = [job for job, h in entry["history"].items() if h["read"] >= 3 and h["failed"] == h["read"]]
    if steady and runtime:
        add(POINTS["deterministic"], f"failed in every build read with {join([jdk_of(j) for j in steady])}"
                                     " - deterministic, not flaky")
    failing = [job for job, h in entry["history"].items() if h["failed"]]
    if len(failing) > 1 and kind != "lint":
        add(POINTS["jdks"], f"fails with {join([jdk_of(j) for j in failing])}")
    regressions = [(job, h["regression"]) for job, h in entry["history"].items()
                   if h.get("regression")]
    if regressions:
        job, reg = regressions[0]
        add(POINTS["new"], f"new: absent from {jdk_of(job)} #{reg['after']}, there since "
                           f"#{reg['build']} ({reg['date']})")
    total = sum(p for p, _ in reasons)
    tier = next(name for floor, name in TIERS if total >= floor) if total >= 0 else "P3"
    return total, tier, reasons


# --------------------------------------------------------------------------- #
# The proposal
# --------------------------------------------------------------------------- #
def summary_of(entry: dict[str, Any], jobs_read: int) -> tuple[str, str]:
    """(JIRA project, summary)."""
    kind = entry["kind"]
    if kind == "lint":
        return "HADOOP", f"Fix the {entry['subsystem']} -1 of the trunk qbt build"
    if kind == "test":
        project = project_key(entry["module"], entry["test"])
        summary = f"{entry['test'].rsplit('.', 1)[-1]} fails on trunk"
    else:
        project, _, summary = core.new_jira_summary(entry["record"]).partition(": ")
        classes = entry["record"].get("classes") or {}
        if len(classes) == 1 and len(next(iter(classes.values()))) > 2:
            # Three bug types and more read badly in a summary.
            short = next(iter(classes)).split("$")[0].rsplit(".", 1)[-1]
            summary = f"Fix the trunk SpotBugs warnings in {short}"
    failing = [job for job, h in entry["history"].items() if h["failed"]]
    if jobs_read > 1 and len(failing) == 1 and kind in ("test", "build"):
        summary += f" with {jdk_of(failing[0])}"
    return project, summary


def describe(entry: dict[str, Any], runs: dict[str, list[dict[str, Any]]]) -> str:
    """The issue description, in JIRA wiki markup."""
    lines: list[str] = []
    kind = entry["kind"]
    if kind == "test":
        lines.append(f"{{{{{entry['test']}}}}} fails in the nightly trunk build (qbt)"
                     + (f", module {{{{{entry['module']}}}}}" if entry["module"] else "") + ":")
        lines.append("")
        for name, case in list(entry["methods"].items())[:8]:
            lines.append(f"* {{{{{name}}}}}: {case['error'] or 'no message'}")
        if not entry["methods"]:
            lines.append("* (the test report names no method; see the unit log)")
    elif kind == "build":
        lines.append(f"{{{{{entry['plugin']}}}}} fails on {{{{{entry['module']}}}}} in the nightly "
                     f"trunk build (qbt), so the unit run of that module is -1 although no test "
                     f"fails.")
    elif kind == "spotbugs":
        lines.append(f"The nightly trunk build (qbt) reports {len(entry['warnings'])} spotbugs "
                     f"warning(s) in {{{{{entry['module']}}}}}:")
        lines.append("")
        for warning in sorted(entry["warnings"].values(), key=lambda w: (w["class"], w["type"]))[:40]:
            lines.append(f"* {{{{{warning['class']}}}}}: {warning_text(warning)} "
                         f"({warning['category'].lower()})")
        if len(entry["warnings"]) > 40:
            lines.append(f"* ... and {len(entry['warnings']) - 40} more")
        lines.append("")
        lines.append(f"They turn the spotbugs vote of these module runs -1: "
                     f"{join(sorted(entry['reported_in']), 8)}.")
    else:
        lines.append(f"The {{{{{entry['subsystem']}}}}} check votes -1 on the whole tree in the "
                     f"nightly trunk build (qbt): {join(entry['comments'], 2).rstrip('.') or 'see the report'}.")
    lines.append("")
    lines.append("Seen in:")
    lines.append("")
    for job, h in entry["history"].items():
        latest = runs[job][0]
        if h["failed"]:
            lines.append(f"* {jdk_of(job)}: {h['failed']} of the last {h['read']} builds, "
                         f"latest [#{latest['number']}|{latest['url']}] ({latest['date']}"
                         + (f", trunk {latest['revision'][:10]}" if latest.get("revision") else "")
                         + ")")
        else:
            lines.append(f"* {jdk_of(job)}: not in the last {h['read']} builds")
    regressions = [h["regression"] for h in entry["history"].values() if h.get("regression")]
    if regressions and regressions[0]["suspects"]:
        lines.append("")
        lines.append(f"It first appeared in build #{regressions[0]['build']}; the commits of "
                     f"that build:")
        lines.append("")
        lines += [f"* {c['sha']} {c['msg'].splitlines()[0][:100]}" for c in regressions[0]["suspects"][:8]]
    for log in entry.get("logs", [])[:2]:
        lines.append("")
        lines.append(f"Log: {log}")
    if entry["blocked"]:
        lines.append("")
        lines.append("It turns the precommit of these open pull requests red:")
        lines.append("")
        lines += [f"* [PR #{b['number']}|{b['url']}] - {b['what']}" for b in entry["blocked"][:15]]
    lines.append("")
    lines.append("No open pull request or JIRA issue that fixes it was found.")
    return "\n".join(lines)


def what_line(entry: dict[str, Any]) -> str:
    kind = entry["kind"]
    if kind == "test":
        methods = list(entry["methods"])
        return (f"{entry['test']}" + (f" in {entry['module']}" if entry["module"] else "")
                + (f": {join(methods, 3)}" if methods else ""))
    if kind == "build":
        return f"{entry['plugin']} fails on {entry['module']}"
    if kind == "spotbugs":
        types = sorted({w["type"] for w in entry["warnings"].values()})
        return f"{len(entry['warnings'])} spotbugs warning(s) in {entry['module']}: {join(types, 4)}"
    return f"{entry['subsystem']}: {join(entry['comments'], 1)}"


def render_text(proposals: list[dict[str, Any]], discarded: list[dict[str, Any]],
                args: argparse.Namespace, header: list[str]) -> str:
    out = list(header)
    out.append("")
    out.append(f"{len(proposals)} JIRA issue(s) proposed, {len(discarded)} candidate(s) "
               f"already tracked.")
    ranked, hidden = ranking(proposals, discarded, args)
    out.append("")
    out.append("Ranked by the open PRs each would help (fixing it clears, or is part of, "
               "a -1 in their latest precommit):")
    for index, (entry, status) in enumerate(ranked, 1):
        out.append("")
        out.append(f"{index:2d}. helps {len(entry['helps']):2d} open PR(s)  "
                   f"{entry['project']}: {entry['summary']}")
        if entry["helps"]:
            out.append(f"      PRs:    {pr_list(entry['helps'])}")
        out.append(f"      status: {status}")
        out += [f"      check:  {line}" for line in checks_of(entry)]
    if hidden:
        out.append("")
        out.append(f"{hidden} more candidate(s) are already tracked and help no open PR; "
                   f"--show-discarded lists them.")
    for index, entry in enumerate(proposals, 1):
        out.append("")
        out.append("-" * 78)
        out.append(f"[{index}] {entry['tier']}  score {entry['score']:3d}  "
                   f"{entry['project']}: {entry['summary']}")
        out.append(f"    what:    {what_line(entry)}")
        for points, reason in entry["reasons"]:
            out.append(f"    {points:+4d}     {reason}")
        for item in entry["tracking"]["related"][:3]:
            ref = f"#{item['number']}" if "number" in item else item["key"]
            why = ("claims to fix it, its precommit says no" if item.get("verified") == "refuted"
                   else "mentions it, does not fix it")
            out.append(f"    related: {ref} {item.get('title') or item.get('summary')} ({why})")
        out.append(f"    propose: {entry['project']}, type Bug, priority "
                   f"{'Major' if entry['tier'] != 'P3' else 'Minor'}")
        if args.show_description:
            out.append("    description:")
            out += [f"      {line}" for line in entry["description"].splitlines()]
    out.append("")
    out.append("This was a dry run - nothing was created.")
    return "\n".join(out)


def ranking(proposals: list[dict[str, Any]], discarded: list[dict[str, Any]],
            args: argparse.Namespace) -> tuple[list[tuple[dict[str, Any], str]], int]:
    """([(candidate, status)] by rank_key, tracked candidates left out).

    A tracked candidate is worth showing when it helps open PRs: they wait for
    its fix, or only for a rebase onto it.
    """
    shown = [e for e in discarded if e["helps"] or args.show_discarded]
    number = {id(e): i for i, e in enumerate(proposals, 1)}
    ranked = sorted(proposals + shown, key=rank_key)
    return ([(e, status_of(e, args, number.get(id(e), 0))) for e in ranked],
            len(discarded) - len(shown))


def tracked_by(entry: dict[str, Any], args: argparse.Namespace) -> str:
    found = entry["tracking"]
    items = [f"{j['key']} ({j['status'] or 'open'})" for j in found["jiras"]]
    items += [f"#{p['number']} ({p['state'].lower()}{', ' + p['jira'] if p.get('jira') else ''}"
              f", {p.get('verified', 'unverified')})" for p in found["prs"]]
    return join(items, 4)


def render_markdown(proposals: list[dict[str, Any]], discarded: list[dict[str, Any]],
                    args: argparse.Namespace, header: list[str]) -> str:
    out = ["# qbt JIRA candidates", ""] + [f"- {h}" for h in header] + [""]
    ranked, hidden = ranking(proposals, discarded, args)
    out += ["## Ranked by the open PRs each would help", "",
            "| Helps | Candidate | Open PRs it helps | Status |", "| --- | --- | --- | --- |"]
    out += [f"| {len(e['helps'])} | {e['project']}: {e['summary']} | "
            f"{pr_list(e['helps']) or '-'} | "
            + "<br>".join([status] + checks_of(e)).replace("|", "/") + " |" for e, status in ranked]
    if hidden:
        out += ["", f"{hidden} more candidate(s) are already tracked and help no open PR."]
    out += ["", "## Proposed JIRA issues", ""]
    out.append("| # | Tier | Score | Proposed JIRA | What |")
    out.append("| --- | --- | --- | --- | --- |")
    for index, entry in enumerate(proposals, 1):
        out.append(f"| {index} | {entry['tier']} | {entry['score']} | {entry['project']}: "
                   f"{entry['summary']} | {what_line(entry).replace('|', '/')} |")
    for index, entry in enumerate(proposals, 1):
        out += ["", f"### {index}. {entry['project']}: {entry['summary']}", "",
                f"**{entry['tier']}**, score {entry['score']}", ""]
        out += [f"- `{points:+d}` {reason}" for points, reason in entry["reasons"]]
        if args.show_description:
            out += ["", "```", entry["description"], "```"]
    return "\n".join(out)


def save(proposals: list[dict[str, Any]], directory: str) -> list[str]:
    os.makedirs(directory, exist_ok=True)
    commands = []
    for index, entry in enumerate(proposals, 1):
        name = re.sub(r"[^\w.-]+", "-", f"{index:02d}-{entry['project']}-{entry['summary']}")[:80]
        path = os.path.join(directory, name + ".txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(entry["description"] + "\n")
        priority = "Major" if entry["tier"] != "P3" else "Minor"
        summary = entry["summary"].replace('"', "'")
        commands.append(f'python create_jira.py --project {entry["project"]} --summary "{summary}" '
                        f'--description-file "{path}" --priority {priority}')
    return commands


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", action="append", default=[],
                        help="qbt job to read. Repeatable. Default: every buildable "
                             "hadoop-qbt-trunk-javaNN-linux-x86_64 job")
    parser.add_argument("--build", type=int, default=None,
                        help="with a single --job: read this build instead of the latest")
    parser.add_argument("--history", type=int, default=HISTORY,
                        help="builds per job to read, the latest included (default: %(default)s)")
    parser.add_argument("--pr-days", type=int, default=PR_DAYS,
                        help="open PRs updated in this many days count (default: %(default)s)")
    parser.add_argument("--include-lint", action="store_true",
                        help="also propose tree-wide -1 votes (xml, pathlen, blanks, ...)")
    parser.add_argument("--min-score", type=int, default=0, help="hide proposals below it")
    parser.add_argument("--limit", type=int, default=0, help="show at most this many proposals")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    parser.add_argument("--show-description", action="store_true",
                        help="print each JIRA description (always in json)")
    parser.add_argument("--show-discarded", action="store_true",
                        help="list the candidates already tracked, and by what")
    parser.add_argument("--save-dir", help="write each description there, with the "
                                           "create_jira.py command that would file it")
    parser.add_argument("--jenkins", default=DEFAULT_JENKINS)
    parser.add_argument("--repo", default=core.DEFAULT_REPO)
    parser.add_argument("--jira-base", default=core.DEFAULT_JIRA)
    parser.add_argument("--repo-path", default=None,
                        help="Hadoop clone, to place spotbugs warnings in their module")
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    args = parser.parse_args(argv)
    if args.build is not None and len(args.job) != 1:
        parser.error("--build goes with exactly one --job")
    if args.history < 1:
        parser.error("--history must be 1 or more")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    token = resolve_token(args.token)
    log = (lambda *a: print(*a, file=sys.stderr)) if args.format != "text" else print

    jobs = args.job or trunk_jobs(args.jenkins)
    if not jobs:
        raise SystemExit(f"no trunk qbt job found on {args.jenkins}")
    runs: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        builds = job_builds(args.jenkins, job, args.history, args.build)
        read = [r for r in (read_build(job, b, i == 0) for i, b in enumerate(builds)) if r]
        if not read or read[0]["number"] != builds[0]["number"]:
            log(f"{job}: the report of #{builds[0]['number'] if builds else '?'} cannot be read, "
                f"skipped")
            continue
        runs[job] = read
        log(f"{job} #{read[0]['number']} ({read[0]['date']}): -1 {join(read[0]['voted'], 8)}; "
            f"{len(read)} build(s) read")
    if not runs:
        raise SystemExit("no qbt report could be read")

    repo_path = args.repo_path or (core.DEFAULT_REPO_PATH
                                   if os.path.isdir(core.DEFAULT_REPO_PATH) else None)
    candidates = build_candidates(runs, source_modules(repo_path), args.include_lint)
    log(f"{len(candidates)} candidate(s); reading open PRs into trunk updated in the last "
        f"{args.pr_days} days...")
    prs = fetch_open_prs(args.repo, args.pr_days, token)
    for pr in prs:
        pr["_precommit"] = precommit_of(pr)
    log(f"{len(prs)} open PR(s); looking for PRs and JIRAs that already cover each candidate...")

    proposals, discarded = [], []
    for entry in candidates:
        history(entry, runs)
        pr_impact(entry, prs)
        entry["project"], entry["summary"] = summary_of(entry, len(runs))
        entry["tracking"] = tracking(entry, args.repo, token, args.jira_base)
        entry["score"], entry["tier"], entry["reasons"] = score(entry)
        entry["helps"] = helps(entry)
        if entry["tracking"]["prs"] or entry["tracking"]["jiras"]:
            discarded.append(entry)
            continue
        entry["description"] = describe(entry, runs)
        if entry["score"] >= args.min_score:
            proposals.append(entry)
    proposals.sort(key=rank_key)
    discarded.sort(key=rank_key)
    if args.limit:
        proposals = proposals[:args.limit]

    header = [f"{jdk_of(job)}: {job} #{b[0]['number']} ({b[0]['date']}), {len(b)} build(s) read"
              for job, b in runs.items()]
    header.append(f"{len(prs)} open PR(s) into trunk updated in the last {args.pr_days} days")
    if not repo_path:
        header.append("no Hadoop clone: spotbugs warnings placed by report, not by source file")
    if args.format == "json":
        def plain(entry: dict[str, Any]) -> dict[str, Any]:
            keep = {k: v for k, v in entry.items() if k not in ("record",)}
            return json.loads(json.dumps(keep, default=sorted))
        print(json.dumps({"builds": header, "proposals": [plain(e) for e in proposals],
                          "discarded": [{"project": e["project"], "summary": e["summary"],
                                         "tier": e["tier"], "score": e["score"],
                                         "helps": plain({"h": e["helps"]})["h"],
                                         "tracked_by": tracked_by(e, args),
                                         "status": status_of(e, args),
                                         "checks": checks_of(e)} for e in discarded]},
                         indent=2))
    elif args.format == "markdown":
        print(render_markdown(proposals, discarded, args, header))
    else:
        print(render_text(proposals, discarded, args, header))
    if args.save_dir and proposals:
        commands = save(proposals, args.save_dir)
        log(f"\n{len(commands)} description(s) written to {args.save_dir}. To file one after "
            f"reviewing it (create_jira.py asks before writing, and needs --apply):")
        for command in commands:
            log(f"  {command} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
