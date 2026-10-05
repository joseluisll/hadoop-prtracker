#!/usr/bin/env python3
"""Apply the dependencies analyze_pr.py finds to JIRA and to the PR description.

For every pull request it looks at, the script builds the same dependency
analysis as ``analyze_pr.py`` - branch ancestry, diff-level symbol use, PR
wording and JIRA links, each with a verdict - and turns it into a list of
concrete changes:

* add a JIRA 'is blocked by' link for a CONFIRMED, CI-FIX (the other PR
  clears a Yetus -1 or a red GitHub Actions run of this one) or DISCOVERED
  dependency that nobody has recorded yet, in either direction;
* delete a JIRA link that is recorded but no longer planned: the diffs do not
  support it (WEAK, UNSUPPORTED), or its CI failure is gone (STALE: not seen
  for --stale-days days, 30 by default, absent from the latest run, and with
  a green run of that check since). LIKELY and UNVERIFIED links, links to
  resolved issues and pairs another PR of the run still plans are kept;
* rewrite the dependency block of the pull request description - 'Depends
  on' and 'Required by' - so it matches what the code actually shows.

The two systems keep a dependency differently. In JIRA one link is enough:
it shows on both issues, whoever reported them or works on them, so each pair
is proposed once, and JIRA is read again right before the write. On GitHub
nothing links two pull requests: each side is a line in each description,
written by hand. Both sides are written when both PRs are yours - the other
one's block gets the matching line, in its own plan or as a single added
item - and only your side when the other PR belongs to somebody else, whose
description is never edited.

A failure that none of your open PRs clears is searched for in other people's
PRs and in JIRA (--no-ci-search skips the search). An open PR that clears it
for sure, and has a JIRA key, is proposed like one of yours: a JIRA link and a
line in the 'Depends on' list of your PR (its own description is left to its
author), each confirmed on its own. Everything else is
only reported: a weaker match (link it yourself with --add-link), a merged fix
(rebase), a JIRA with no PR, or a suggested summary for a new JIRA when
nothing was found.

*Nothing is written without --apply.* Every change is printed in full, with
its evidence, and asked for one at a time; --apply --force answers yes to all
of them (still printing each one first). A dry run (the default) only prints
the plan.

JIRA writes need a Personal Access Token from
https://issues.apache.org/jira/secure/ViewProfile.jspa -> Personal Access
Tokens, exported as JIRA_TOKEN (or JIRA_PAT). It is read from the environment
only, never from the command line, so it stays out of the shell history.
GitHub writes reuse $GITHUB_TOKEN / $GH_TOKEN / 'gh auth token', which needs
the 'repo' (or 'public_repo') scope to edit a pull request body.

Examples
--------
    python fix_dependencies.py 8704                 # dry run, one PR
    python fix_dependencies.py --all-open           # dry run, every open PR
    python fix_dependencies.py 8704 --apply         # ask, then write
    python fix_dependencies.py --all-open --apply --jira-only
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import analyze_pr as core
    from list_upstream_prs import RETRIES, RETRY_WAIT, join, resolve_token
except ImportError:  # pragma: no cover - misplaced file
    raise SystemExit(
        "analyze_pr.py and list_upstream_prs.py must sit next to this script."
    )

# 'Blocker' on the ASF instance: outward 'blocks', inward 'is blocked by'.
BLOCKER_LINK_TYPE = "Blocker"
BLOCK_START, BLOCK_END = core.BLOCK_START, core.BLOCK_END

# Verdicts good enough to write down, and those worth deleting once recorded.
WORTH_DECLARING = ("CONFIRMED", "CI-FIX", "DISCOVERED")
WORTH_DELETING = ("WEAK", "UNSUPPORTED", "STALE")


# --------------------------------------------------------------------------- #
# Authenticated requests
# --------------------------------------------------------------------------- #
def jira_token() -> str | None:
    for name in ("JIRA_TOKEN", "JIRA_PAT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def request_json(url: str, token: str | None, method: str = "GET",
                 payload: dict[str, Any] | None = None,
                 accept: str = "application/json") -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": accept, "User-Agent": "fix-dependencies"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response.status, (json.loads(body) if body.strip() else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code in (429, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            return exc.code, detail
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError) as exc:
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            return 0, str(getattr(exc, "reason", exc))
    return 0, "unreachable"


# --------------------------------------------------------------------------- #
# The changes this script knows how to make
# --------------------------------------------------------------------------- #
@dataclass
class Change:
    kind: str                      # jira-link-add | jira-link-remove | pr-body
    target: str                    # what it touches, for the summary line
    headline: str                  # one line describing the change
    evidence: list[str] = field(default_factory=list)
    preview: str = ""              # shown before asking
    run: Callable[[], tuple[bool, str]] | None = None
    needs: str = ""                # 'jira' or 'github'
    key: str = ""                  # the same write proposed twice has the same key


def jira_key_of(entry: dict[str, Any]) -> str | None:
    """The JIRA issue a dependency stands for, whichever way it was written."""
    if entry.get("jira"):
        return entry["jira"].upper()
    reference = (entry.get("ref") or "").strip()
    if core.JIRA_IN_TEXT_RE.fullmatch(reference):
        return reference.upper()
    match = core.JIRA_IN_TEXT_RE.match((entry.get("title") or "").strip())
    return match.group(0).upper() if match else None


def has_blocking_link(jira: core.Jira, key: str) -> dict[str, Any] | None:
    for link in jira.links:
        if link["key"].upper() != key.upper():
            continue
        if any(word in link["label"] for word in core.JIRA_BLOCKING_LABELS):
            return link
    return None


def has_blocked_link(jira: core.Jira, key: str) -> dict[str, Any] | None:
    """The link saying this issue blocks `key`, if JIRA has one."""
    for link in jira.links:
        if link["key"].upper() == key.upper() and \
                any(word in link["label"] for word in core.JIRA_BLOCKED_LABELS):
            return link
    return None


def author_of(pr: dict[str, Any]) -> str:
    return (((pr.get("author") or {}) or {}).get("login") or "").lower()


# --------------------------------------------------------------------------- #
# The managed block: 'Depends on' and 'Required by'
# --------------------------------------------------------------------------- #
# On GitHub nothing links two pull requests: each side of a dependency is a
# line written by hand in each description. This script writes both sides,
# but only in descriptions of PRs you opened - somebody else's stays as it is.
SECTIONS = (("depends", core.DEPENDS_HEADER), ("required", core.REQUIRED_HEADER))


def entry_text(entry: dict[str, Any], section: str) -> str:
    """One item of the block; the lines under it say why, for the reader."""
    key = jira_key_of(entry)
    label = entry["ref"] + (f" ({key})" if key else "")
    title = f" - {entry['title']}" if entry.get("title") else ""
    why = ""
    if section == "depends" and entry.get("ci") and \
            entry.get("verdict") in ("CI-FIX", "DISCOVERED"):
        why = f"  \n  _clears a CI failure: {entry['ci']['reason']}_"
    if entry.get("mutual"):
        why += ("  \n  _mutual: each PR clears a CI failure of the other, so neither "
                "goes green alone - merge them back to back_")
    return f"- {label}{title}{why}"


def block_items(body: str) -> dict[str, list[tuple[str, str]]]:
    """The block as it is now: (reference, text) per item, per section."""
    items: dict[str, list[list[str]]] = {"depends": [], "required": []}
    body = body or ""
    start, end = body.find(BLOCK_START), body.find(BLOCK_END)
    if start == -1 or end == -1:
        return {"depends": [], "required": []}
    section, current = "depends", None
    for line in body[start + len(BLOCK_START): end].splitlines():
        if line.startswith("**Required by**"):
            section, current = "required", None
        elif line.startswith("**Depends on**"):
            section, current = "depends", None
        elif line.startswith("- "):
            _, listed = core.split_managed_block(f"{BLOCK_START}\n{line}\n{BLOCK_END}")
            current = [listed["depends"][0] if listed["depends"] else line, line]
            items[section].append(current)
        elif current is not None and line.strip():
            current[1] += "\n" + line
    return {name: [(ref, raw) for ref, raw in values] for name, values in items.items()}


def render_block(items: dict[str, list[tuple[str, str]]]) -> str | None:
    if not any(items.values()):
        return None
    lines = [BLOCK_START]
    for section, header in SECTIONS:
        if items.get(section):
            # A blank line ends the list above: without it GitHub folds the
            # next header into the last item, indented.
            if len(lines) > 1:
                lines.append("")
            lines.append(header)
            lines += [raw for _, raw in items[section]]
    lines.append(BLOCK_END)
    return "\n".join(lines)


def build_block(depends: list[dict[str, Any]],
                required: list[dict[str, Any]] | None = None) -> str | None:
    return render_block({
        "depends": [(e["ref"], entry_text(e, "depends")) for e in depends],
        "required": [(e["ref"], entry_text(e, "required")) for e in required or []],
    })


def apply_block(body: str, block: str | None) -> str:
    """Insert, replace or drop the managed block, leaving the rest untouched."""
    body = body or ""
    start, end = body.find(BLOCK_START), body.find(BLOCK_END)
    if start != -1 and end != -1:
        tail = body[end + len(BLOCK_END):].lstrip("\n")
        head = body[:start].rstrip("\n")
        if block is None:
            return (head + ("\n\n" if head and tail else "") + tail).strip() + "\n"
        return (head + ("\n\n" if head else "") + block + "\n\n" + tail).strip() + "\n"
    if block is None:
        return body
    return (block + "\n\n" + body.lstrip()).strip() + "\n"


def add_to_block(body: str, section: str, reference: str, raw: str) -> str:
    """Add one item to a section, keeping every other line of the block."""
    items = block_items(body)
    if any(ref == reference for ref, _ in items[section]):
        return body
    items[section].append((reference, raw))
    return apply_block(body, render_block(items))


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    pr: dict[str, Any]
    jira: core.Jira | None
    deps: dict[str, Any]


def solid_lists(target: Target) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """The dependencies worth writing down, both ways, and why some are not linked."""
    pr, jira, deps = target.pr, target.jira, target.deps
    number = pr["number"]
    depends = [e for e in deps.get("depends_on", [])
               if e.get("open") and e.get("verdict") in WORTH_DECLARING]
    required = [e for e in deps.get("blocks", [])
                if e.get("open") and e.get("number") and e.get("verdict") in WORTH_DECLARING]
    notes: list[str] = []
    # Two PRs that each clear a -1 of the other: 'is blocked by' both ways
    # would be a cycle nobody can merge through, so say it instead.
    waiting_on_me = {
        e["ref"] for e in deps.get("blocks", [])
        if e.get("open") and e.get("verdict") in WORTH_DECLARING + ("LIKELY",)
    }
    for entry in depends:
        other = jira_key_of(entry)
        if entry["ref"] in waiting_on_me or (jira and other and has_blocked_link(jira, other)):
            entry["mutual"] = True
            notes.append(
                f"#{number} and {entry['ref']} each clear a precommit -1 of the other - "
                "no JIRA link is proposed (it would be a cycle); merge them back to back"
            )
    mutual = {e["ref"] for e in depends if e.get("mutual")}
    for entry in required:
        other = jira_key_of(entry)
        if entry["ref"] in mutual or (jira and other and has_blocking_link(jira, other)):
            entry["mutual"] = True
    return depends, required, notes


def link_add(jira_base: str, blocker: str, blocked: str, reference: str,
             evidence: list[str]) -> Change:
    return Change(
        kind="jira-link-add",
        target=blocked,
        headline=f"link {blocked} 'is blocked by' {blocker}  ({reference})",
        evidence=evidence,
        preview=(f"POST {jira_base}/rest/api/2/issueLink\n"
                 f"    {blocker} blocks {blocked}   (type: {BLOCKER_LINK_TYPE})\n"
                 f"    one link: JIRA shows it on {blocked} as 'is blocked by' and on "
                 f"{blocker} as 'blocks'"),
        needs="jira",
        key=f"link:{blocker}>{blocked}",
        run=lambda: create_link(jira_base, blocker, blocked),
    )


def plan_for(
    pr: dict[str, Any], jira: core.Jira | None, deps: dict[str, Any],
    repo: str, jira_base: str, token: str | None,
    jira_only: bool, pr_only: bool, me: str | None = None,
    extra: dict[str, list[dict[str, Any]]] | None = None,
    keep: dict[frozenset[str], str] | None = None,
    analysed: set[int] | None = None,
) -> tuple[list[Change], list[str]]:
    """The changes for one PR. `extra` adds what other PRs of the run say of it,
    `keep` the JIRA pairs one of them still plans (see `supported_pairs`)."""
    changes: list[Change] = []
    number = pr["number"]
    key = jira.key if jira and jira.found else None
    depends, required, notes = solid_lists(Target(pr, jira, deps))
    # Recorded but not planned: what JIRA links that this plan would not write.
    planned = {jira_key_of(e) for e in depends + required}
    removable = [
        e for e in deps.get("depends_on", []) + deps.get("blocks", [])
        if "jira" in e.get("sources", []) and e.get("verdict") in WORTH_DELETING
        and jira_key_of(e) not in planned
    ]

    # ----- JIRA: links to add ------------------------------------------------ #
    # One link is enough: JIRA shows it on both issues, whoever owns them.
    if key and not pr_only:
        for entry in depends:
            other = jira_key_of(entry)
            if not other or entry.get("mutual") or has_blocking_link(jira, other):
                continue
            changes.append(link_add(
                jira_base, other, key, entry["ref"],
                [entry.get("verdict_reason", "")] + entry.get("reasons", [])[:1]))
        for entry in required:
            other = jira_key_of(entry)
            if not other or entry.get("mutual") or has_blocked_link(jira, other):
                continue
            changes.append(link_add(
                jira_base, key, other, entry["ref"],
                [f"{entry['ref']} waits for #{number}: {entry.get('verdict_reason', '')}"]
                + entry.get("reasons", [])[:1]))

    # ----- JIRA: links the diffs do not support ------------------------------ #
    if key and not pr_only:
        for entry in removable:
            other = jira_key_of(entry) or entry.get("jira")
            link = (has_blocking_link(jira, other) or has_blocked_link(jira, other)) \
                if other else None
            if not link or not link.get("id") or link.get("resolution"):
                continue
            # One link serves both issues: the other side may see what this
            # one does not (a -1 it inherits from a branch it is stacked on).
            backer = (keep or {}).get(frozenset((key, other)))
            if backer:
                notes.append(f"the link {key} - {other} is not proposed for deletion: {backer}")
                continue
            evidence = [entry.get("verdict_reason", "")]
            if entry in deps.get("blocks", []) and entry.get("number") not in (analysed or set()):
                evidence.append(
                    f"judged from #{number} only: the CI of {entry['ref']} was read, but not "
                    f"what it inherits from a branch it is stacked on - analyse "
                    f"{entry['ref']} too before deleting"
                )
            # How to put this very link back: the CLI shortcut when it is a
            # 'Blocker' link, the raw payload otherwise.
            if link["type"] == BLOCKER_LINK_TYPE:
                pair = f"{key}:{other}" if link["direction"] == "inward" else f"{other}:{key}"
                recreate = f"python fix_dependencies.py --add-link {pair} --apply"
            else:
                outward, inward = ((key, other) if link["direction"] == "outward"
                                   else (other, key))
                recreate = (
                    f"POST {jira_base}/rest/api/2/issueLink with "
                    f'{{"type":{{"name":"{link["type"]}"}},'
                    f'"outwardIssue":{{"key":"{outward}"}},'
                    f'"inwardIssue":{{"key":"{inward}"}}}}'
                )
            changes.append(
                Change(
                    kind="jira-link-remove",
                    target=key,
                    headline=f"delete the link {key} '{link['label']}' {other}  "
                             f"[{entry.get('verdict')}]",
                    evidence=evidence,
                    preview=(
                        f"DELETE {jira_base}/rest/api/2/issueLink/{link['id']}\n"
                        f"    it goes from both issues at once\n"
                        f"    to put it back exactly as it is now:\n"
                        f"        {recreate}\n"
                        f"    or by hand in the 'Issue Links' section of {key}"
                    ),
                    needs="jira",
                    key=f"unlink:{link['id']}",
                    run=lambda link_id=link["id"]: delete_link(jira_base, link_id),
                )
            )

    # ----- the pull request description -------------------------------------- #
    owner = author_of(pr)
    if not jira_only and me and owner and owner != me.lower():
        notes.append(f"#{number} was opened by {owner}: its description is not edited here - "
                     "only your side of each dependency is written, in your own PRs")
    elif not jira_only:
        for section, entries in (("depends", depends), ("required", required)):
            known = {e["ref"] for e in entries}
            entries += [e for e in (extra or {}).get(section, []) if e["ref"] not in known]
        # A description edited in the GitHub web UI comes back with CRLF endings.
        body = (pr.get("body") or "").replace("\r\n", "\n")
        block = build_block(depends, required)
        new_body = apply_block(body, block)
        if new_body.strip() != body.strip():
            evidence = [f"needs {e['ref']}: {e.get('verdict_reason', '')}" for e in depends]
            evidence += [f"{e['ref']} waits for it: {e.get('verdict_reason', '')}"
                         for e in required]
            kept_refs = {e["ref"] for e in depends + required}
            evidence += [f"drops {e['ref']} [{e.get('verdict')}]: {e.get('verdict_reason', '')}"
                         for e in deps.get("depends_on", []) + deps.get("blocks", [])
                         if "block" in e.get("sources", []) and e["ref"] not in kept_refs]
            stale = [e["ref"] for e in deps.get("depends_on", [])
                     if "text" in e.get("sources", []) and e.get("verdict") == "UNSUPPORTED"]
            if stale:
                evidence.append(
                    f"the description still claims a dependency on {join(stale)}, which the "
                    "diffs no longer show - that prose is not touched, edit it yourself"
                )
            changes.append(
                Change(
                    kind="pr-body",
                    target=f"#{number}",
                    headline=(f"update the dependency block of #{number}"
                              if block else
                              f"remove the dependency block from #{number}"),
                    evidence=evidence,
                    preview=(block or "(block removed)"),
                    needs="github",
                    key=f"body:{number}",
                    run=lambda number=number, block=block: update_pr_body(
                        repo, number, lambda text: apply_block(text, block), token
                    ),
                )
            )
    for entry in depends + required:
        if entry.get("number") and entry.get("author") and me \
                and entry["author"].lower() != me.lower():
            notes.append(f"{entry['ref']} was opened by {entry['author']}: only #{number} "
                         f"records this dependency on GitHub (JIRA shows the link on both)")
    return changes, notes


def as_entry(target: Target, like: dict[str, Any]) -> dict[str, Any]:
    """A PR of the run as the other side of a dependency lists it."""
    pr, jira = target.pr, target.jira
    return {"ref": f"#{pr['number']}", "number": pr["number"], "title": pr.get("title") or "",
            "jira": jira.key if jira and jira.found else None, "open": True,
            "author": author_of(pr), "verdict": like.get("verdict"),
            "verdict_reason": like.get("verdict_reason", ""), "mutual": like.get("mutual")}


def supported_pairs(targets: list[Target]) -> dict[frozenset[str], str]:
    """The JIRA pairs some PR of the run still plans, with who and why.

    A link is one object shared by two issues, so it is proposed for deletion
    only when no side analysed in this run plans it.
    """
    kept: dict[frozenset[str], str] = {}
    for target in targets:
        jira = target.jira
        if not (jira and jira.found):
            continue
        for entry in target.deps.get("depends_on", []) + target.deps.get("blocks", []):
            other = jira_key_of(entry) or entry.get("jira")
            verdict = entry.get("verdict")
            if not other or verdict not in WORTH_DECLARING:
                continue
            kept.setdefault(frozenset((jira.key, other)),
                            f"#{target.pr['number']} judges it {verdict} - "
                            f"{entry.get('verdict_reason', '')}")
    return kept


def plan_all(
    targets: list[Target], repo: str, jira_base: str, token: str | None,
    jira_only: bool, pr_only: bool, me: str | None,
) -> list[tuple[int, list[Change], list[str]]]:
    """Plan every PR of a run, each write of it proposed once.

    A dependency found from one side is written on the other side too: in the
    description of the other PR when you opened it (folded into its own plan
    when it is part of the run, an item added to its block otherwise), and in
    JIRA only once, since one link shows on both issues.
    """
    in_run = {t.pr["number"]: t for t in targets}
    keep = supported_pairs(targets)
    extra: dict[int, dict[str, list[dict[str, Any]]]] = {
        n: {"depends": [], "required": []} for n in in_run}
    outside: dict[str, Change] = {}
    origin: dict[str, int] = {}
    mine = (me or "").lower()
    for target in targets if not jira_only else []:
        number = target.pr["number"]
        depends, required, _ = solid_lists(target)
        for entries, side in ((depends, "required"), (required, "depends")):
            for entry in entries:
                other = entry.get("number")
                if not other or not mine or (entry.get("author") or "").lower() != mine:
                    continue
                item = as_entry(target, entry)
                if other in in_run:
                    extra[other][side].append(item)
                    continue
                change_key = f"add:{other}:{side}:{number}"
                if change_key in outside:
                    continue
                raw = entry_text(item, side)
                header = "Required by" if side == "required" else "Depends on"
                outside[change_key] = Change(
                    kind="pr-body",
                    target=f"#{other}",
                    headline=f"add #{number} to the '{header}' list of #{other}",
                    evidence=[f"the other side of a dependency of #{number}: "
                              f"{entry.get('verdict_reason', '')}",
                              f"#{other} is yours and not part of this plan, so only this "
                              f"item is added - the rest of its block stays as it is"],
                    preview=raw,
                    needs="github",
                    key=change_key,
                    run=lambda other=other, side=side, ref=f"#{number}", raw=raw:
                        update_pr_body(repo, other,
                                       lambda text: add_to_block(text, side, ref, raw), token),
                )
                origin[change_key] = number

    results: list[tuple[int, list[Change], list[str]]] = []
    seen: dict[str, int] = {}
    for target in targets:
        number = target.pr["number"]
        changes, notes = plan_for(target.pr, target.jira, target.deps, repo, jira_base, token,
                                  jira_only, pr_only, me, extra[number], keep,
                                  set(in_run))
        changes += [c for k, c in outside.items() if origin[k] == number]
        kept = []
        for change in changes:
            if change.key and change.key in seen:
                notes.append(f"'{change.headline}' is the same write as one proposed for "
                             f"#{seen[change.key]}, listed there only")
                continue
            if change.key:
                seen[change.key] = number
            kept.append(change)
        results.append((number, kept, notes))
    return results


# --------------------------------------------------------------------------- #
# The writes themselves
# --------------------------------------------------------------------------- #
def create_link(jira_base: str, blocker: str, blocked: str) -> tuple[bool, str]:
    """One link, from one side: JIRA shows it on both issues by itself.

    JIRA is read again first, so a link made meanwhile - by hand, or from the
    other issue of the pair - is not made twice, and no cycle is closed.
    """
    now = core.fetch_jira(jira_base, blocked)
    if now.found:
        if has_blocking_link(now, blocker):
            return True, f"{blocked} was already blocked by {blocker} - nothing written"
        if has_blocked_link(now, blocker):
            return False, (f"{blocked} already blocks {blocker}: the link asked for would "
                           f"close a cycle - nothing written")
    status, body = request_json(
        f"{jira_base.rstrip('/')}/rest/api/2/issueLink",
        jira_token(), "POST",
        {
            "type": {"name": BLOCKER_LINK_TYPE},
            # JIRA's POST reads these the other way round from how an issue
            # shows them: the inwardIssue is the one that 'blocks'.
            "inwardIssue": {"key": blocker},
            "outwardIssue": {"key": blocked},
        },
    )
    if status not in (200, 201, 204):
        return False, f"JIRA refused the link ({status}): {body}"
    # Read it back: the direction is what matters, and JIRA does not say.
    after = core.fetch_jira(jira_base, blocked)
    if has_blocking_link(after, blocker):
        return True, f"{blocker} now blocks {blocked} (read back from JIRA)"
    if has_blocked_link(after, blocker):
        return False, (f"JIRA recorded it reversed: {blocked} blocks {blocker}. Fix it with "
                       f"python fix_dependencies.py --flip-link {blocked}:{blocker} --apply")
    return False, f"JIRA accepted the link, but {blocked} does not show it"


def delete_link(jira_base: str, link_id: str) -> tuple[bool, str]:
    status, body = request_json(
        f"{jira_base.rstrip('/')}/rest/api/2/issueLink/{link_id}", jira_token(), "DELETE"
    )
    if status in (200, 204):
        return True, f"link {link_id} deleted"
    return False, f"JIRA refused the deletion ({status}): {body}"


_VIEWER: dict[str, str | None] = {}


def viewer_login(token: str | None) -> str | None:
    """Who the GitHub token belongs to: the only author whose PRs are edited."""
    if token not in _VIEWER:
        try:
            data = core.graphql("query { viewer { login } }", {}, token)
            _VIEWER[token] = ((data.get("viewer") or {}) or {}).get("login")
        except SystemExit:
            _VIEWER[token] = None
    return _VIEWER[token]


def update_pr_body(repo: str, number: int, edit: Callable[[str], str],
                   token: str | None) -> tuple[bool, str]:
    """Re-read the description first, so an edit made meanwhile is not lost."""
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    status, payload = request_json(url, token, accept="application/vnd.github+json")
    if status != 200 or not isinstance(payload, dict):
        return False, f"could not re-read #{number} ({status}): {payload}"
    owner = ((payload.get("user") or {}) or {}).get("login") or ""
    me = viewer_login(token)
    if me and owner.lower() != me.lower():
        return False, (f"#{number} was opened by {owner}, not by {me}: its description is "
                       f"theirs to edit - nothing written")
    current = (payload.get("body") or "").replace("\r\n", "\n")
    body = edit(current)
    if body.strip() == current.strip():
        return True, f"description of #{number} was already up to date"
    status, payload = request_json(
        url, token, "PATCH", {"body": body}, accept="application/vnd.github+json"
    )
    if status == 200:
        return True, f"description of #{number} updated"
    return False, f"GitHub refused the edit ({status}): {payload}"


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #
def change_text(change: Change, index: int, total: int) -> str:
    """A change in full - headline, evidence and what will be sent."""
    lines = [f"[{index}/{total}] {change.headline}"]
    lines += [f"    why: {line}" for line in change.evidence if line]
    lines += [f"    {line}" for line in change.preview.splitlines()]
    return "\n".join(lines)


def show(change: Change, index: int, total: int) -> None:
    print()
    print("-" * 78)
    print(change_text(change, index, total))


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        print(f"    skipped - {question} needs a terminal to answer on")
        return False
    try:
        answer = input(f"    {question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n    aborted")
        raise SystemExit(130)
    return answer in ("y", "yes")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def unexplained_lines(items: list[dict[str, Any]], jira: core.Jira | None) -> list[str]:
    """CI failures no proposed dependency clears. Report only: no link, no PR edit."""
    if not items:
        return []
    own = jira.key if jira and jira.found else None
    lines = ["still red, and no PR proposed above clears it (report only, nothing is changed):"]
    for item in items:
        where = " and ".join(item.get("seen_in") or ["CI"])
        lines.append(f"  - {item['failure']} ({where})")
        for fix in item.get("prs", [])[:3]:
            state = f"merged {fix['merged']} - a rebase clears it" if fix["state"] == "MERGED" \
                else f"open, by {fix['author']}"
            lines.append(f"      PR #{fix['number']} {fix['title']} [{state}]")
            lines.append(f"        {fix['reason']}")
            if fix["state"] == "OPEN" and fix.get("jira") and own:
                lines.append(f"        to link it yourself: python fix_dependencies.py "
                             f"--add-link {own}:{fix['jira']} --apply")
        for issue in item.get("jiras", [])[:3]:
            lines.append(f"      JIRA {issue['key']} {issue['summary']} "
                         f"[{issue['resolution'] or issue['status']}] - no PR found for it")
        if item.get("new_jira"):
            lines.append(f"      no PR and no JIRA found: a new JIRA is needed, e.g. "
                         f"'{item['new_jira']}'")
    return lines


def report_unexplained(items: list[dict[str, Any]], jira: core.Jira | None) -> None:
    for line in unexplained_lines(items, jira):
        print(f"    {line}")


def prose_notes(pr: dict[str, Any], deps: dict[str, Any]) -> list[str]:
    """Prose nobody can rewrite safely: say it, do not touch it."""
    return [
        f"the description of #{pr['number']} still claims a dependency on {entry['ref']}, "
        f"which the diffs do not show - edit that prose yourself, this script never "
        f"rewrites it"
        for entry in deps.get("depends_on", [])
        if "text" in entry.get("sources", []) and entry.get("verdict") == "UNSUPPORTED"
    ]


def link_change(jira_base: str, pair: str, why: str) -> Change:
    """One 'is blocked by' link asked for by hand, as BLOCKED:BLOCKER."""
    blocked, _, blocker = pair.partition(":")
    blocked, blocker = blocked.strip().upper(), blocker.strip().upper()
    if not core.JIRA_IN_TEXT_RE.fullmatch(blocked or "") or \
            not core.JIRA_IN_TEXT_RE.fullmatch(blocker or ""):
        raise ValueError(f"want two JIRA ids as BLOCKED:BLOCKER, got '{pair}'")
    return Change(
        kind="jira-link-add",
        target=blocked,
        headline=f"link {blocked} 'is blocked by' {blocker}",
        evidence=[why],
        preview=(f"POST {jira_base}/rest/api/2/issueLink\n"
                 f"    {blocker} blocks {blocked}   (type: {BLOCKER_LINK_TYPE})"),
        needs="jira",
        key=f"link:{blocker}>{blocked}",
        run=lambda: create_link(jira_base, blocker, blocked),
    )


def flip_change(jira_base: str, pair: str) -> Change:
    """Turn a reversed link round: BLOCKED:BLOCKER, the way it should read."""
    blocked, _, blocker = pair.partition(":")
    blocked, blocker = blocked.strip().upper(), blocker.strip().upper()
    if not core.JIRA_IN_TEXT_RE.fullmatch(blocked or "") or             not core.JIRA_IN_TEXT_RE.fullmatch(blocker or ""):
        raise ValueError(f"want two JIRA ids as BLOCKED:BLOCKER, got '{pair}'")
    now = core.fetch_jira(jira_base, blocked)
    wrong = has_blocked_link(now, blocker) if now.found else None
    if not wrong or wrong.get("type") != BLOCKER_LINK_TYPE:
        raise ValueError(f"{blocked} has no '{BLOCKER_LINK_TYPE}' link saying it blocks "
                         f"{blocker} - nothing to flip")

    def run() -> tuple[bool, str]:
        ok, message = delete_link(jira_base, wrong["id"])
        if not ok:
            return ok, message
        ok, made = create_link(jira_base, blocker, blocked)
        if ok:
            return True, f"{message}; {made}"
        return False, (f"{message}, but the new link failed: {made}. To put the old one "
                       f"back: python fix_dependencies.py --add-link {blocker}:{blocked} --apply")

    return Change(
        kind="jira-link-flip",
        target=blocked,
        headline=f"turn round the link {blocked} 'blocks' {blocker}: "
                 f"{blocked} 'is blocked by' {blocker}",
        evidence=["asked for on the command line"],
        preview=(f"DELETE {jira_base}/rest/api/2/issueLink/{wrong['id']}   "
                 f"({blocked} blocks {blocker})\n"
                 f"POST {jira_base}/rest/api/2/issueLink\n"
                 f"    {blocker} blocks {blocked}   (type: {BLOCKER_LINK_TYPE}), read back after"),
        needs="jira",
        key=f"flip:{wrong['id']}",
        run=run,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", nargs="*", help="PR number(s) and/or JIRA id(s)")
    parser.add_argument("--all-open", nargs="?", const=core.DEFAULT_AUTHOR, default=None,
                        metavar="AUTHOR", help="every open PR of an author")
    parser.add_argument("--apply", action="store_true",
                        help="offer to write the changes (each one is still asked for)")
    parser.add_argument("--force", action="store_true",
                        help="with --apply: answer yes to every change instead of asking "
                             "(each one is still printed before it is written)")
    parser.add_argument("--jira-only", action="store_true", help="do not touch PR descriptions")
    parser.add_argument("--pr-only", action="store_true", help="do not touch JIRA")
    parser.add_argument("--add-link", action="append", default=[], metavar="BLOCKED:BLOCKER",
                        help="just add one 'is blocked by' link, e.g. HADOOP-19972:HADOOP-19970; "
                             "use it to undo a deletion. Repeatable.")
    parser.add_argument("--flip-link", action="append", default=[], metavar="BLOCKED:BLOCKER",
                        help="turn round a link JIRA holds the wrong way: BLOCKED:BLOCKER is "
                             "how it should read. Repeatable.")
    parser.add_argument("--stale-days", type=int, default=core.STALE_DAYS, metavar="DAYS",
                        help="a CI fix is STALE once its failure has not been seen for this "
                             "many days and a run of that check was green since "
                             "(default: %(default)s)")
    parser.add_argument("--no-ci-search", action="store_true",
                        help="do not look for PRs of others or JIRA issues for the failures "
                             "none of your PRs clears")
    parser.add_argument("--repo", default=core.DEFAULT_REPO)
    parser.add_argument("--jira-base", default=core.DEFAULT_JIRA)
    parser.add_argument("--repo-path", default=None)
    parser.add_argument("--token", default=None, help="GitHub token (else $GITHUB_TOKEN or gh)")
    args = parser.parse_args(argv)
    if args.force and not args.apply:
        parser.error("--force only goes with --apply")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args = parse_args(argv)
    core.STALE_DAYS = args.stale_days
    token = resolve_token(args.token)
    repo_path = args.repo_path or (
        core.DEFAULT_REPO_PATH if os.path.isdir(core.DEFAULT_REPO_PATH) else None
    )

    changes: list[Change] = []
    for pair in args.add_link:
        try:
            changes.append(link_change(args.jira_base, pair, "asked for on the command line"))
        except ValueError as exc:
            raise SystemExit(f"--add-link: {exc}")
    for pair in args.flip_link:
        try:
            changes.append(flip_change(args.jira_base, pair))
        except ValueError as exc:
            raise SystemExit(f"--flip-link: {exc}")

    targets = list(args.target)
    if args.all_open:
        numbers = sorted(
            (p["number"] for p in core.fetch_peer_prs(args.repo, args.all_open, token)),
            reverse=True,
        )
        targets += [str(n) for n in numbers if str(n) not in targets]
    if not targets and not changes:
        raise SystemExit("give a PR number or a JIRA id, or use --all-open / --add-link / "
                         "--flip-link.")

    planned: list[Target] = []
    for target in targets:
        jira, pr, _ = core.resolve_target(target, args.repo, args.jira_base, token)
        if pr is None or pr.get("state") != "OPEN":
            print(f"{target}: no open pull request, skipped")
            continue
        if any(t.pr["number"] == pr["number"] for t in planned):
            continue
        deps = core.collect_dependencies(pr, jira, args.repo, token, repo_path,
                                         jira_base=args.jira_base,
                                         search_external=not args.no_ci_search)
        planned.append(Target(pr, jira, deps))

    me = viewer_login(token) or args.all_open or core.DEFAULT_AUTHOR
    for target, (number, found, notes) in zip(planned, plan_all(
            planned, args.repo, args.jira_base, token,
            args.jira_only, args.pr_only, me)):
        jira = target.jira
        label = jira.key if jira and jira.found else "no JIRA"
        print(f"#{number} {label}: {len(found)} change(s) proposed")
        for note in notes:
            print(f"    note: {note}")
        for note in prose_notes(target.pr, target.deps):
            print(f"    note: {note}")
        report_unexplained(target.deps.get("unexplained", []), jira)
        changes += found

    if not changes:
        print("\nNothing to change: JIRA and the PR descriptions already match the code.")
        return 0

    print(f"\n{len(changes)} change(s) proposed in total:")
    for change in changes:
        print(f"  [{change.kind}] {change.headline}")

    if not args.apply:
        for index, change in enumerate(changes, 1):
            show(change, index, len(changes))
        print(
            "\nThis was a dry run - nothing was written. Re-run with --apply to be asked "
            "about each change one at a time."
        )
        return 0

    missing = []
    if any(c.needs == "jira" for c in changes) and not jira_token():
        missing.append(
            "JIRA_TOKEN is not set: create a Personal Access Token at "
            f"{args.jira_base}/secure/ViewProfile.jspa -> Personal Access Tokens and export it"
        )
    if any(c.needs == "github" for c in changes) and not token:
        missing.append("no GitHub token: set GITHUB_TOKEN or run 'gh auth login'")
    for line in missing:
        print(f"\nwarning: {line}")
    if missing:
        print("The changes needing that credential will be skipped.\n")

    applied = skipped = failed = 0
    for index, change in enumerate(changes, 1):
        show(change, index, len(changes))
        if change.needs == "jira" and not jira_token():
            print("    skipped - no JIRA token")
            skipped += 1
            continue
        if change.needs == "github" and not token:
            print("    skipped - no GitHub token")
            skipped += 1
            continue
        if args.force:
            print("    apply this change? [y/N] y  (--force)")
        elif not confirm("apply this change?"):
            print("    skipped")
            skipped += 1
            continue
        ok, message = change.run()
        print(f"    {'done' if ok else 'FAILED'}: {message}")
        applied += ok
        failed += not ok

    print(f"\napplied {applied}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
