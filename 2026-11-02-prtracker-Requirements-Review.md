# prtracker requirements review, 2026-11-02

Review of [REQUIREMENTS.md](REQUIREMENTS.md) (R1-R110, inferred from the code) against the
code at commit `ddcd686`, in a detached worktree. No code, document or configuration was
changed; this report is the only new file besides the requirements themselves.

The review was started with a `claude -p` worker. Its first round produced a report with
errors (ten requirements left unclassified, a suspicion dismissed for the wrong reason, a
CI claim that `ci.yml` disproves); its second round wrote five reproduction scripts and
then looped re-reading files, and was stopped. This report keeps the worker's reproductions
(each re-run here) and its per-requirement reading where it holds, and replaces the rest
with a check made in this session.

## Verdict

The code does what the requirements say almost everywhere: the safety model (dry run by
default, one confirmation per write, re-read before write, no force push), the verdict
rules, the scoring tables and the workflows all match. The defects are at the edges: two
safety guards that fail open when a lookup fails, one HBase-only ordering bug that shows
the wrong Yetus result, and a qbt candidate that can vanish from the output.

| Status | Count | Requirements |
| --- | --- | --- |
| Met | 102 | all others |
| Partly met | 8 | R2, R4, R21, R24, R33, R38, R80, R90 |
| Not met | 0 | |
| Not checkable | 0 | |

**Reproduced** means a script in the scratch directory ran the project's real function on
synthetic input, with network calls stubbed, and showed the behaviour; **from reading**
means the claim rests on the code alone.

## Serious findings

### 1. HBase: the PR status shows the oldest commit's Yetus result (R33) - reproduced

`analyze_pr.actions_yetus_comments` (analyze_pr.py:865-910) builds one synthetic Yetus
comment per head commit, newest first, and `add_actions_yetus` (analyze_pr.py:920) appends
them after the PR's comments. `list_upstream_prs.parse_yetus_comment`
(list_upstream_prs.py:319-327) keeps the *last* matching comment. `fetch_pr` and
`fetch_ci_failures` read 3 commits (`YETUS_COMMITS`), so `analyze_pr.py` and the TUI's
Analysis tab take the status and the -1 list from the oldest of the three. `yetus_reports`
sorts by date and is not affected; list views read one commit and are not affected either.

```
python3 -I scratch/r33_yetus_order.py
comment order (first = newest): ['2026-11-02T00:00:00Z', '2026-11-01T00:00:00Z']
selected overall: -1
selected posted_at: 2026-11-01T00:00:00Z
expected newest overall: +1
```

Fix: small. Pick the comment with the newest `createdAt` in `parse_yetus_comment`, or
append the synthetic comments oldest first.

### 2. `create_jira.py --force` files an issue when the duplicate search fails (R21) - reproduced

`already_filed` (create_jira.py:145-152) does `core.jira_get(...) or {}`. A failed JIRA
search, whether the network failed or the JQL was refused, therefore looks like "no
duplicate". `main` (create_jira.py:292-311) then asks the plain "create this issue?",
and `--force` answers yes. The guard that `--force` must never answer is skipped exactly
when the check could not be made. Without `--force` the user is still asked, but is told
nothing about a possible duplicate.

```
python3 -I scratch/s3_create_jira.py      # jira_get stubbed to None, POST stubbed
already_filed: []
    create this issue? [y/N] y  (--force)
    done: TEST-1 created: ...
1 created, 0 skipped, 0 not done.
```

Fix: small. Have `already_filed` tell a failed search from an empty one, and treat a
failure as "may be filed".

### 3. The owner check before a PR-description edit is skipped when the login is unknown (R24) - reproduced

`update_pr_body` (fix_dependencies.py:620-641) checks `if me and owner != me`. When
`viewer_login` returns None (GraphQL failure, or a token without `read:user`), the check is
skipped and the PATCH is sent. The plan-time check in `plan_for` uses a fallback login
(`viewer_login(token) or args.all_open or DEFAULT_AUTHOR`), so in practice only PRs planned
as yours reach this point. But the write-time check, which is the last line of defence,
fails open. GitHub's own permissions still stop an edit the token is not allowed to make.

```
python3 -I scratch/r24_update_pr_body.py   # viewer_login -> None, owner 'someone-else'
  PATCH https://api.github.com/repos/owner/repo/pulls/1 {'body': 'old\nnew'}
PATCH attempted: True
```

Fix: small. Refuse the write when `me` is None.

### 4. A qbt candidate with a negative score disappears (R80) - reproduced

`score` (qbt_jira.py:819-896) can go below 0: a style-only spotbugs warning (+2) seen only
in aggregate runs (-5) with one JDK scores -3. `main` (qbt_jira.py:1214-1219) puts a
candidate in `discarded` only when it is tracked, and in `proposals` only when
`score >= --min-score` (default 0). A negative, untracked candidate is in neither list, so
the ranking that R80 says lists "every candidate" never shows it, not even with
`--show-discarded`.

```
python3 -I scratch/s2_negative_score.py
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
`list_upstream_prs.py` docstring says a token is only needed for the rate limit. Fix:
small. Print a warning, or require a token under `hbase`.

## Partly met

| Requirement | Gap |
| --- | --- |
| R2 | `pr_graph.py` is a library module with no command line; R2 says every tool is a CLI script. Requirement wording. |
| R4 | `pr_graph.py` has no `main()` or `--help` (ci.yml's help loop rightly leaves it out). Requirement wording. |
| R21 | Finding 2: `--force` creates the issue when the duplicate search fails. |
| R24 | Finding 3: the write-time owner check fails open. |
| R33 | Finding 1: HBase single-PR analysis reads the oldest of 3 commits. |
| R38 | Finding 5: HBase artifacts are read only with a token. |
| R80 | Finding 4: negative-score candidates are missing from the ranking. |
| R90 | `--top 0` returns every candidate (review_queue.py:514-527: `len(picked) == top` never holds). Reproduced: `scratch/r90_top_zero.py` printed `picked: [1, 2, 3, 4]`. Fix: one line (`if len(picked) >= top`, or reject `--top < 1`). |

## Documentation out of date

- README "Tests" lists 8 commands and calls 7 offline, but leaves out
  `tests/graph_smoke.py`, which ci.yml runs. (The worker's claim that ci.yml skips tests
  is wrong: ci.yml runs all eight offline tests.)
- README "Dependency verdicts" gives `--stale-days` a default of 30. That is true only for
  `fix_dependencies.py`. `analyze_pr.py` and `list_upstream_prs.py` have a
  `--stale-days` defaulting to 14 that means PR inactivity, and `analyze_pr.py` has no
  flag for the 30-day CI window.
- `list_upstream_prs.py` docstring: "a token is only needed for the rate limit", which is
  false under `hbase` (finding 5).
- REQUIREMENTS.md R2 and R4 overstate: `pr_graph.py` is not a CLI.
- REQUIREMENTS.md R95 says `--protect` defaults to "the base branch"; the code protects
  the base branch plus `trunk`, `main` and `master` (list_stale_branches.py:567).
- README "Review queue" says drafts are never recommended; `--include-drafts` makes them
  eligible.

## Gaps with no requirement or to-do item

- No offline test covers `create_jira.py`, `list_stale_branches.classify`,
  `list_upstream_prs.evaluate`, `analyze_pr.verdict_for`, or the managed-block functions
  `apply_block` / `block_items`. Every finding above sits in code no test exercises.
- `qbt_jira.save` prints shell commands with the summary in double quotes; a summary with
  `$` or a backtick would be expanded when pasted. Summaries come from test and class
  names, so the risk is low.
- The branch scripts ignore `git fetch` failures (`check=False`) and go on with stale refs
  without saying so.

## Open items

REQUIREMENTS.md has no to-do items. Each partly-met item above is a confirmed open defect;
findings 1-4 and R90 are reproduced, finding 5 and the wording items are from reading.

## Recommended order

1. Make the two safety guards fail closed: R24 (refuse the edit when the login is unknown)
   and R21 (a failed duplicate search counts as "may be filed"). These are the safety
   model's promises.
2. Fix the HBase Yetus ordering (R33), because it gives wrong analysis results.
3. Warn when HBase runs without a token (R38).
4. Decide on negative qbt scores (R80). Recommendation: keep them in the ranking.
5. `--top 0` (R90), the README fixes, and the R2/R4/R95 wording in REQUIREMENTS.md.
6. Add offline tests for the four functions behind findings 1-4, so the fixes stay fixed.

## What this review did not cover

- `pr_manager.py` and `tests/tui_smoke.py` were not run (`textual` is not installed).
  Nothing touched the network.
- R60-R92 rest on the worker's first-round reading, spot-checked here only for
  `create_jira.py`, `qbt_jira.py` and `review_queue.py`. `analyze_pr.analyse` (the
  Suggestion logic, R36) and `pr_graph` layout (R74) were not read line by line.
- The workflows were read but not run. Which CI runs contain `ddcd686` was not checked.
- The worker's cost (round 1 ~$28 by the CLI's estimate, round 2 stopped) is approximate:
  the CLI does not know the model.
