# prtracker requirements review, 2026-11-02 13:20

Review of [REQUIREMENTS.md](REQUIREMENTS.md) (R1-R110) against the code at commit
`38fed83` ("Show the fork's Build and Yetus runs in list_my_jiras.py (#31)"), in a detached
worktree. No code, document or configuration was changed; this report and the notes and
scratch scripts under `.review/` are the only new files.

The review was done by a `claude -p` worker over four rounds, then checked in the session that
started it: every reproduction was re-run, the code behind each Partly met item was read, and
Met items with strong words were spot-checked. That check found one more Partly met (R13), which
is the checker's, not the worker's; it also corrected the finding numbers in the Partly met
table.

The previous review, at `ddcd686`, found 8 Partly met and 0 Not met. This review re-confirmed
all eight, found two more (R92: `review_queue.py` uses REST under the `hbase` profile; R13: one HTTP
request is not retried), and
read the new `list_my_jiras.py` code added in #31 closely. CI at `38fed83` passed on Ubuntu
and Windows (run 41422428253), including the eight offline smoke tests.

## Verdict

The code matches the requirements almost everywhere: the safety model, the verdict and
scoring rules, the branch tools and the workflows all hold. The defects are at the edges: two
safety guards fail open when a lookup fails, one HBase-only ordering bug shows the wrong
Yetus result, a qbt candidate can vanish from the output, and the new fork columns in
`list_my_jiras.py` report some run conclusions as failures.

| Status | Count | Requirements |
| --- | --- | --- |
| Met | 100 | all others |
| Partly met | 10 | R2, R4, R13, R21, R24, R33, R38, R80, R90, R92 |
| Not met | 0 | |
| Not checkable | 0 | |

**Reproduced** means a script in `.review/scratch/` ran the project's real function on
synthetic input, with network calls stubbed. **From reading** means the claim rests on the
code alone. R21, R24, R33, R80 and R90 are reproduced; R2, R4, R13, R38 and R92 are from reading.

## Serious findings

### 1. The owner check before a PR-description edit is skipped when the login is unknown (R24) - reproduced

`update_pr_body` (fix_dependencies.py:620-641) checks `if me and owner != me`. When
`viewer_login` returns None (a GraphQL failure, or a token without `read:user`), the check is
skipped and the PATCH is sent. The plan-time check uses a fallback login
(`viewer_login(token) or args.all_open or DEFAULT_AUTHOR`), so in practice only PRs planned
as yours reach this point, but the write-time check - the last line of defence - fails open.
GitHub's own permissions still stop an edit the token is not allowed to make.

```
python3 -I .review/scratch/test_r24.py    # viewer_login -> None, owner 'someone-else'
calls: [('https://api.github.com/repos/owner/repo/pulls/1', 'GET', None),
        ('https://api.github.com/repos/owner/repo/pulls/1', 'PATCH',
         {'body': 'old\nnew'})]
PATCH attempted: True
result: True description of #1 updated
```

Fix: small. Refuse the write when `me` is None.

### 2. `create_jira.py --force` files an issue when the duplicate search fails (R21) - reproduced

`already_filed` (create_jira.py:145-152) does `core.jira_get(...) or {}`. A failed JIRA
search, whether the network failed or the JQL was refused, therefore looks like "no
duplicate". `main` (create_jira.py:292-311) then asks the plain "create this issue?", and
`--force` answers yes. The guard that `--force` must never answer (R21) is skipped exactly
when the check could not be made. Without `--force` the user is still asked, but is told
nothing about a possible duplicate.

```
python3 -I .review/scratch/test_r21.py     # jira_get -> None, POST stubbed
1 JIRA issue(s) to create:
  TEST: Test summary
...
    create this issue? [y/N] y  (--force)
    done: TEST-1 created: http://example.com/browse/TEST-1
1 created, 0 skipped, 0 not done.
```

Fix: small. Have `already_filed` tell a failed search from an empty one, and treat a failure
as "may be filed".

### 3. HBase: the PR status shows the oldest commit's Yetus result (R33) - reproduced

`analyze_pr.actions_yetus_comments` (analyze_pr.py:865-910) builds one synthetic Yetus
comment per head commit, newest first, and `add_actions_yetus` (analyze_pr.py:920) appends
them after the PR's comments. `list_upstream_prs.parse_yetus_comment`
(list_upstream_prs.py:319-327) keeps the *last* matching comment. `fetch_pr` and
`fetch_ci_failures` read 3 commits (`YETUS_COMMITS`), so `analyze_pr.py` and the TUI's
Analysis tab take the status and the -1 list from the oldest of the three. `yetus_reports`
sorts by date and is not affected; list views read one commit and are not affected either.

```
python3 -I .review/scratch/test_r33.py
comment order (first = newest): ['2026-11-02T00:00:00Z', '2026-11-01T00:00:00Z']
selected overall: -1
selected posted_at: 2026-11-01T00:00:00Z
expected newest overall: +1
```

Fix: small. Pick the comment with the newest `createdAt` in `parse_yetus_comment`, or append
the synthetic comments oldest first.

### 4. A qbt candidate with a negative score disappears (R80) - reproduced

`score` (qbt_jira.py:819-896) can go below 0: a style-only spotbugs warning (+2) seen only
in aggregate runs (-5) with one JDK scores -3. `main` (qbt_jira.py:1214-1219) puts a
candidate in `discarded` only when it is tracked, and in `proposals` only when
`score >= --min-score` (default 0). A negative, untracked candidate is in neither list, so
the ranking that R80 says lists "every candidate" never shows it, not even with
`--show-discarded`.

```
python3 -I .review/scratch/s2_negative_score.py
score: -3 tier: P3
proposals: 0
discarded: 0
neither proposals nor discarded: True
```

Fix: small. Decide whether such candidates belong in the ranking (recommended: list them,
since the ranking is "by open PRs helped"), or document that `--min-score 0` hides them.

### 5. HBase reads no Yetus result without a GitHub token (R38) - from reading

`actions_yetus_comments` returns `()` when there is no token (analyze_pr.py:871-872). Under
`hbase` every tool then reports "Yetus not run yet", with no warning. The
`list_upstream_prs.py` docstring says a token is only needed for the rate limit. Fix: small.
Print a warning, or require a token under `hbase`.

## New code in #31 (`list_my_jiras.py`)

The new fork Build/Yetus columns are a useful addition, and the 404 handling is correct. One
new defect and three notes:

- **`fork_verdict` maps every non-success/non-cancelled conclusion to failure.**
  `list_my_jiras.py:101-112` turns `neutral`, `skipped`, `timed_out`, `action_required` and
  `stale` into `Fail` (Build) or `-1` (Yetus). `build_cell` (list_my_jiras.py:67-77) already
  has a specific `ACTION_REQUIRED -> "Awaiting approval to run"` for the PR's Build check, so
  the fork Build column is inconsistent with the PR Build column. Reproduced:

  ```
  python3 -I .review/scratch/s3_fork_verdict.py
  neutral         Build=Fail     Yetus=-1
  skipped         Build=Fail     Yetus=-1
  timed_out       Build=Fail     Yetus=-1
  action_required Build=Fail     Yetus=-1
  stale           Build=Fail     Yetus=-1
  ```

  This is new behavior with no requirement and no test: `tests/jiras_smoke.py` covers
  success, failure, cancelled, queued and running only. Fix: small - map `action_required` to
  "Awaiting approval to run" and `skipped`/`neutral` to a non-failure label, and add those
  cases to `tests/jiras_smoke.py`.

- **"older than tip" is applied to Build as well as Yetus.** `fork_cell`
  (list_my_jiras.py:115-124) compares `run.head_sha` with the tip for Build, and uses the
  `Yetus` status target for Yetus. `tests/jiras_smoke.py:148-149` expects the Build mark, so
  the code and test agree; the brief's description mentions only Yetus. Wording difference,
  not a code defect.

- **404 handling is safe.** `core.rest_json` (analyze_pr.py:745-751) returns `{}` when
  `http_get` returns `None` for a non-200 response, so `fork_branch` returns `None` and the
  "No branch" path is reached. The earlier crash suspicion is closed.

- **`classify_contexts(pr)[0]` fallback is safe.** `classify_contexts`
  (list_upstream_prs.py:266-308) always returns two `CheckGroup`s, so `build_cell` cannot
  IndexError.

- **Section 15 of REQUIREMENTS.md does not mention the fork columns or `--fork`.**
  README line 26 and the script docstring do. This is a documentation/gap item, not an R102
  failure: R102's original columns still work.

## Partly met

| Requirement | Gap |
| --- | --- |
| R2 | `pr_graph.py` is a library module with no command line; R2 says every tool is a CLI script. Requirement wording. |
| R4 | `pr_graph.py` has no `main()` or `--help` (ci.yml's help loop rightly leaves it out). Requirement wording. |
| R13 | `spotbugs_report` (analyze_pr.py:965-980) reads the Jenkins spotbugs report with a bare `urlopen`, not the retrying `fetch`, and swallows any error, so one dropped connection silently loses the class names `parse_ci_failures` attaches (analyze_pr.py:1166). Every other HTTP path goes through `fetch` (list_upstream_prs.py:157-173). From reading; found in the check, not by the worker. Fix: small (build the request through `http_get`/`fetch`). |
| R21 | Finding 2: `--force` creates the issue when the duplicate search fails. |
| R24 | Finding 1: the write-time owner check fails open. |
| R33 | Finding 3: HBase single-PR analysis reads the oldest of 3 commits. |
| R38 | Finding 5: HBase artifacts are read only with a token. |
| R80 | Finding 4: negative-score candidates are missing from the ranking. |
| R90 | `--top 0` returns every candidate (review_queue.py:514-527: `len(picked) == top` never holds). Reproduced: `python3 -I .review/scratch/r90_top_zero.py` printed `picked: [1, 2, 3, 4]`. Fix: one line (`if len(picked) >= top`, or reject `--top < 1`). |
| R92 | Under `hbase`, `fetch_open_prs` (review_queue.py:155-177) calls `core.add_actions_yetus` (line 172), which calls `actions_yetus_comments` (analyze_pr.py:864-910) and `rest_json` (lines 873, 886, 889). R92 says "only GraphQL queries and git read commands". The same code path makes README line 296 false under `hbase`. From reading. Fix: amend R92/README to state the read-only Actions API calls, or gate them. |

## Documentation out of date

- README "Tests" lists 8 commands and calls 7 offline, but leaves out
  `tests/graph_smoke.py`, which ci.yml runs. (The claim that ci.yml skips a test is wrong:
  ci.yml runs all eight offline tests.)
- README "Dependency verdicts" gives `--stale-days` a default of 30. That is true only for
  `fix_dependencies.py`. `analyze_pr.py` and `list_upstream_prs.py` have a `--stale-days`
  defaulting to 14 that means PR inactivity, and `analyze_pr.py` has no flag for the 30-day
  CI window.
- `list_upstream_prs.py` docstring: "a token is only needed for the rate limit", which is
  false under `hbase` (finding 5).
- REQUIREMENTS.md R2 and R4 overstate: `pr_graph.py` is not a CLI.
- REQUIREMENTS.md R95 says `--protect` defaults to "the base branch"; the code protects the
  base branch plus `trunk`, `main` and `master` (list_stale_branches.py:567).
- README "Review queue" says drafts are never recommended; `--include-drafts` makes them
  eligible.
- REQUIREMENTS.md section 15 (R102) does not mention the fork Build/Yetus columns or
  `--fork` added in #31, although README line 26 and the script docstring do.
- README line 296 says `review_queue.py` "only runs GraphQL queries and git ls-tree/git log";
  under `hbase` it also reads the Actions runs/jobs/artifacts over REST (R92).

## Gaps with no requirement or to-do item

- No offline test covers `create_jira.py`, `list_stale_branches.classify`,
  `list_upstream_prs.evaluate`, `analyze_pr.verdict_for`, or the managed-block functions
  `apply_block` / `block_items`. Every finding above sits in code no test exercises.
- `qbt_jira.save` prints shell commands with the summary in double quotes; a summary with
  `$` or a backtick would be expanded when pasted. Summaries come from test and class names,
  so the risk is low.
- The branch scripts ignore `git fetch` failures (`check=False`) and go on with stale refs
  without saying so.
- The new fork columns have no requirement and no test for `skipped`, `neutral`,
  `timed_out`, `action_required` or `stale` conclusions; `tests/jiras_smoke.py` stubs
  `rest_json`, so the real 404 path and those conclusions are not exercised.
- `list_my_jiras.py` makes a branch lookup, two Actions-run lookups and (when a Yetus run
  exists) a commit-status lookup per issue, with no caching or batching. A long issue list
  multiplies API calls; no requirement sets a budget.

## Open items

REQUIREMENTS.md has no to-do items. Each partly-met item above is a confirmed open defect:

- Reproduced: R21, R24, R33, R80, R90, and the new `fork_verdict` mapping.
- From reading: R2, R4, R13, R38, R92.
- The previous review's eight partly-met findings are still present at `38fed83`; none was
  fixed. Confidence is high: five are reproduced, and the other three (R2, R4, R38) rest on
  code that did not change.
- The earlier suspicion that `core.rest_json` crashes on a 404 is closed: it returns `{}`.
- The `classify_contexts(pr)[0]` fallback is safe: it always returns two groups.
- CI at `38fed83` passed on Ubuntu and Windows (run 41422428253; `test (windows-latest)` 45s,
  `test (ubuntu-latest)` 16s). The eight offline smoke tests ran there. `tui_smoke.py` is not
  part of CI.

## Recommended order

1. Make the two safety guards fail closed: R24 (refuse the edit when the login is unknown)
   and R21 (a failed duplicate search counts as "may be filed"). These are the safety
   model's promises.
2. Fix the HBase Yetus ordering (R33), because it gives wrong analysis results.
3. Warn when HBase runs without a token (R38).
4. Decide on negative qbt scores (R80). Recommendation: keep them in the ranking.
5. Fix the new `fork_verdict` mapping and add skipped/neutral/action_required cases to
   `tests/jiras_smoke.py`.
6. `--top 0` (R90), the `spotbugs_report` retry (R13), the R92/README `hbase` REST note, the README fixes, and the R2/R4/R95
   wording in REQUIREMENTS.md.
7. Add offline tests for the functions behind findings 1-5, so the fixes stay fixed.

## What this review did not cover

- `pr_manager.py` and `tests/tui_smoke.py` were not run (`textual` is not installed).
  `pr_manager.py` was read in full and R66-R73 were checked against it.
- The workflows were read, not run. Their scheduled runs were not inspected; only the CI run
  containing `38fed83` was checked.
- `analyze_pr.analyse` (R36) and the `pr_graph` layout (R74) were read and checked against
  `tests/graph_smoke.py`, but not executed live.
- Requirements outside the partly-met list rest on the previous review's per-requirement
  reading plus this review's spot checks of the code paths named in the notes; all 110 were
  not re-derived from scratch.
- No network beyond `gh run list` / `gh run view`; no CI artifacts were downloaded.

Good news that was verified: the dry-run default, one confirmation per write, re-read before
write and no-force-push rules hold (R19, R20, R25, R28); the branch tools R93-R101 match
their requirements and have offline tests; the workflows R103-R110 match their schedules,
tokens, concurrency and artifact rules; the graph lanes do not run behind boxes
(`tests/graph_smoke.py:32-41`); and the new 404 path and `classify_contexts` fallback are
safe.
