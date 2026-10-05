#!/usr/bin/env python3
"""Create the JIRA issues fix_dependencies.py says are missing.

fix_dependencies.py reports a CI failure that none of your open PRs clears,
and for which no PR and no JIRA exists, as "a new JIRA is needed, e.g.
'YARN: TestYarnNativeServices fails on trunk'". This script turns each of
those into a JIRA issue: the project and summary it suggests, and a
description with the evidence - the PRs whose CI it turns red, when it was
seen, the report, the classes and bug types spotbugs names.

The same failure seen by several PRs is one issue. Unresolved issues of the
project with the same test or class in their summary are shown as possible
duplicates, searched again right before each write; creating one anyway is
asked for even with --force, which skips it instead. Nothing else is touched: no link to your issues, no PR edit. After a
creation the script prints the --add-link command that would record the
dependency, for you to run if you want it.

A single issue can also be given by hand with --project and --summary.

*Nothing is written without --apply.* Every issue is printed in full and
asked for one at a time; --apply --force answers yes to all of them (still
printing each one first). A dry run (the default) only prints them.

The JIRA Personal Access Token is read from JIRA_TOKEN (or JIRA_PAT) in the
environment only, never from the command line.

Examples
--------
    python create_jira.py 8720 8704                  # dry run, two PRs
    python create_jira.py --all-open                 # dry run, every open PR
    python create_jira.py 8720 --apply               # ask, then create
    python create_jira.py --project HADOOP --summary "Fix X" --description-file x.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import analyze_pr as core
import fix_dependencies as fd
from list_upstream_prs import join, resolve_token


# --------------------------------------------------------------------------- #
# What to file
# --------------------------------------------------------------------------- #
def split_suggestion(suggestion: str) -> tuple[str, str]:
    """'YARN: TestX fails on trunk' -> ('YARN', 'TestX fails on trunk')."""
    project, _, summary = suggestion.partition(": ")
    if not summary or not project.isupper():
        return core.DEFAULT_PROJECT, suggestion
    return project, summary


def describe(record: dict[str, Any], seen_by: list[dict[str, Any]]) -> str:
    """The issue description, in JIRA wiki markup."""
    lines: list[str] = []
    if record.get("test"):
        lines.append(f"{{{{{record['test']}}}}} fails on trunk, independently of the change "
                     f"under test.")
    elif record.get("project"):
        lines.append(f"The {{{{{record.get('plugin')}}}}} goal fails on "
                     f"{{{{{record['project']}}}}} on trunk.")
    elif record.get("classes"):
        lines.append(f"{record['subsystem'].capitalize()} reports warnings on trunk in "
                     f"{{{{{record.get('module') or 'the build'}}}}}:")
        lines.append("")
        lines += [f"* {{{{{cls}}}}}: {join(types)}" for cls, types in record["classes"].items()]
    else:
        lines.append(f"{record.get('detail')}.")
    lines.append("")
    lines.append("It turns the precommit of pull requests that do not touch it red:")
    lines.append("")
    for seen in seen_by:
        pr = seen["pr"]
        key = f" ({seen['jira']})" if seen.get("jira") else ""
        lines.append(f"* [PR #{pr['number']}|{pr.get('url') or ''}]{key}")
    lines.append("")
    first = min((s["record"].get("first_seen") or "")[:10] for s in seen_by)
    last = max((s["record"].get("last_seen") or "")[:10] for s in seen_by)
    places = join(sorted({p for s in seen_by for p in (s["record"].get("seen_in") or [])}))
    lines.append(f"Seen in: {places or 'CI'}; first seen {first or 'unknown'}, "
                 f"last seen {last or 'unknown'}.")
    reports = sorted({s["record"]["report"] for s in seen_by if s["record"].get("report")})
    if reports:
        lines.append("")
        lines += [f"Report: {url}" for url in reports[:3]]
    lines.append("")
    lines.append("No open pull request or JIRA issue that fixes it was found.")
    return "\n".join(lines)


def proposals_from_prs(args: argparse.Namespace, token: str | None) -> list[dict[str, Any]]:
    """One proposal per missing issue, merged across the PRs that see it."""
    targets = list(args.target)
    if args.all_open:
        numbers = sorted((p["number"] for p in core.fetch_peer_prs(args.repo, args.all_open, token)),
                         reverse=True)
        targets += [str(n) for n in numbers if str(n) not in targets]
    repo_path = args.repo_path or (
        core.DEFAULT_REPO_PATH if os.path.isdir(core.DEFAULT_REPO_PATH) else None)
    found: dict[str, dict[str, Any]] = {}
    done: set[int] = set()
    for target in targets:
        jira, pr, _ = core.resolve_target(target, args.repo, args.jira_base, token)
        if pr is None or pr.get("state") != "OPEN" or pr["number"] in done:
            if pr is None or pr.get("state") != "OPEN":
                print(f"{target}: no open pull request, skipped")
            continue
        done.add(pr["number"])
        deps = core.collect_dependencies(pr, jira, args.repo, token, repo_path,
                                         jira_base=args.jira_base, search_external=True)
        key = jira.key if jira and jira.found else None
        missing = [i for i in deps.get("unexplained", []) if i.get("new_jira")]
        print(f"#{pr['number']} {key or 'no JIRA'}: {len(missing)} missing JIRA issue(s)")
        for item in missing:
            project, summary = split_suggestion(item["new_jira"])
            entry = found.setdefault(summary, {"project": project, "summary": summary,
                                               "record": item.get("record") or {
                                                   "detail": item["failure"]},
                                               "seen_by": []})
            entry["seen_by"].append({"pr": pr, "jira": key, "record": item.get("record") or {
                "seen_in": item.get("seen_in"), "last_seen": item.get("last_seen")}})
    for entry in found.values():
        entry["description"] = describe(entry["record"], entry["seen_by"])
    return list(found.values())


# --------------------------------------------------------------------------- #
# JIRA
# --------------------------------------------------------------------------- #
def search_words(proposal: dict[str, Any]) -> list[str]:
    record = proposal.get("record") or {}
    if len(record.get("classes") or {}) > 1 and record.get("module"):
        # Filed for the whole module: look for the module, not one class.
        name = record["module"].rsplit("/", 1)[-1]
        return [w for w in name.split("-") if w not in core.MODULE_NOISE][:1] + [record["subsystem"]]
    words = core.failure_words(record) if record.get("subsystem") else []
    return words[0] if words else [proposal["summary"]]


def already_filed(jira_base: str, proposal: dict[str, Any]) -> list[dict[str, str]]:
    """Unresolved issues of the project whose summary has the same words."""
    terms = " AND ".join(f'summary ~ "\\"{w}\\""' for w in search_words(proposal))
    jql = f"project = {proposal['project']} AND resolution = Unresolved AND {terms}"
    data = core.jira_get(jira_base, "search",
                         {"jql": jql, "fields": "summary,status", "maxResults": "5"}) or {}
    return [{"key": i.get("key", ""), "summary": (i.get("fields") or {}).get("summary", "")}
            for i in data.get("issues") or []]


def myself(jira_base: str) -> str | None:
    status, body = fd.request_json(f"{jira_base.rstrip('/')}/rest/api/2/myself",
                                   fd.jira_token())
    return body.get("name") if status == 200 and isinstance(body, dict) else None


def create_issue(jira_base: str, proposal: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    """Create and read back; assign only when asked."""
    fields: dict[str, Any] = {
        "project": {"key": proposal["project"]},
        "summary": proposal["summary"],
        "issuetype": {"name": args.type},
        "priority": {"name": args.priority},
        "description": proposal["description"],
    }
    if args.component:
        fields["components"] = [{"name": c} for c in args.component]
    if args.label:
        fields["labels"] = list(args.label)
    status, body = fd.request_json(f"{jira_base.rstrip('/')}/rest/api/2/issue",
                                   fd.jira_token(), "POST", {"fields": fields})
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("key"):
        return False, f"JIRA refused the issue ({status}): {body}"
    key = body["key"]
    after = core.fetch_jira(jira_base, key)
    if not after.found:
        return False, f"JIRA answered {key}, but it cannot be read back"
    message = f"{key} created: {jira_base.rstrip('/')}/browse/{key}"
    if args.assign_me:
        name = myself(jira_base)
        status, answer = fd.request_json(f"{jira_base.rstrip('/')}/rest/api/2/issue/{key}/assignee",
                                         fd.jira_token(), "PUT", {"name": name}) \
            if name else (0, "who the token belongs to is unknown")
        message += f", assigned to {name}" if status in (200, 204) \
            else f" (not assigned: {status} {answer})"
    return True, message


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def show(proposal: dict[str, Any], index: int, total: int, args: argparse.Namespace) -> None:
    print()
    print("-" * 78)
    print(f"[{index}/{total}] create {proposal['project']}: {proposal['summary']}")
    print(f"    type {args.type}, priority {args.priority}"
          + (f", components {join(args.component)}" if args.component else "")
          + (f", labels {join(args.label)}" if args.label else "")
          + (", assigned to you" if args.assign_me else ""))
    print("    description:")
    for line in proposal["description"].splitlines():
        print(f"      {line}")
    for issue in proposal.get("duplicates", []):
        print(f"    possible duplicate: {issue['key']} {issue['summary']} "
              f"({args.jira_base.rstrip('/')}/browse/{issue['key']})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     parents=[core.profile_parser(argv)])
    parser.add_argument("target", nargs="*", help="PR number(s) and/or JIRA id(s)")
    parser.add_argument("--all-open", nargs="?", const=core.DEFAULT_AUTHOR, default=None,
                        metavar="AUTHOR", help="every open PR of an author")
    parser.add_argument("--project", help="file one issue by hand in this project (e.g. YARN)")
    parser.add_argument("--summary", help="with --project: its summary")
    parser.add_argument("--description-file", help="with --project: its description (wiki markup)")
    parser.add_argument("--type", default="Bug", help="issue type (default: %(default)s)")
    parser.add_argument("--priority", default="Major", help="priority (default: %(default)s)")
    parser.add_argument("--component", action="append", default=[],
                        help="component, as JIRA names it. Repeatable.")
    parser.add_argument("--label", action="append", default=[], help="label. Repeatable.")
    parser.add_argument("--assign-me", action="store_true",
                        help="assign each issue to the owner of the JIRA token")
    parser.add_argument("--apply", action="store_true",
                        help="offer to create the issues (each one is still asked for)")
    parser.add_argument("--force", action="store_true",
                        help="with --apply: answer yes to every issue instead of asking "
                             "(each one is still printed before it is created)")
    parser.add_argument("--repo", default=core.DEFAULT_REPO)
    parser.add_argument("--jira-base", default=core.DEFAULT_JIRA)
    parser.add_argument("--repo-path", default=None)
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    args = parser.parse_args(argv)
    if args.force and not args.apply:
        parser.error("--force only goes with --apply")
    if bool(args.project) != bool(args.summary):
        parser.error("--project and --summary go together")
    if args.description_file and not args.project:
        parser.error("--description-file goes with --project")
    if not (args.target or args.all_open or args.project):
        parser.error("give a PR number or a JIRA id, --all-open, or --project/--summary")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    token = resolve_token(args.token)

    proposals: list[dict[str, Any]] = []
    if args.project:
        description = ""
        if args.description_file:
            with open(args.description_file, encoding="utf-8") as handle:
                description = handle.read().strip()
        proposals.append({"project": args.project.upper(), "summary": args.summary,
                          "description": description, "record": {}})
    if args.target or args.all_open:
        proposals += proposals_from_prs(args, token)

    if not proposals:
        print("\nNo JIRA issue is missing.")
        return 0
    for proposal in proposals:
        proposal["duplicates"] = already_filed(args.jira_base, proposal)
    print(f"\n{len(proposals)} JIRA issue(s) to create:")
    for proposal in proposals:
        print(f"  {proposal['project']}: {proposal['summary']}"
              + (f"  - possible duplicate of {join([d['key'] for d in proposal['duplicates']])}"
                 if proposal["duplicates"] else ""))

    if not args.apply:
        for index, proposal in enumerate(proposals, 1):
            show(proposal, index, len(proposals), args)
        print("\nThis was a dry run - nothing was created. Re-run with --apply to be asked "
              "about each issue one at a time.")
        return 0
    if not fd.jira_token():
        raise SystemExit(
            "JIRA_TOKEN is not set: create a Personal Access Token at "
            f"{args.jira_base}/secure/ViewProfile.jspa -> Personal Access Tokens and export it")

    created = skipped = failed = 0
    for index, proposal in enumerate(proposals, 1):
        show(proposal, index, len(proposals), args)
        # Searched again right before the write: somebody may have filed it.
        proposal["duplicates"] = already_filed(args.jira_base, proposal)
        if proposal["duplicates"]:
            # A duplicate is public and outlives a mistake: --force never
            # answers this one.
            if args.force:
                print("    skipped - it may be filed already; without --force you are asked")
                skipped += 1
                continue
            question = "it may be filed already - create it anyway?"
        else:
            question = "create this issue?"
        if args.force:
            print(f"    {question} [y/N] y  (--force)")
        elif not fd.confirm(question):
            skipped += 1
            continue
        ok, message = create_issue(args.jira_base, proposal, args)
        print(f"    {'done' if ok else 'not done'}: {message}")
        if not ok:
            failed += 1
            continue
        created += 1
        new_key = message.split(" ", 1)[0]
        for seen in proposal.get("seen_by", []):
            if seen.get("jira"):
                print(f"    to record that {seen['jira']} waits for it: python fix_dependencies.py "
                      f"--add-link {seen['jira']}:{new_key} --apply")
    print(f"\n{created} created, {skipped} skipped, {failed} not done.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
