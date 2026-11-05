# prtracker requirements

Requirements inferred from the code, the README, the module docstrings and the
workflows as of commit `ddcd686` (2026-11-02). Each one is numbered for
reference; the *Source* names where it is implemented or stated. "Shall" marks
what the code is expected to do; nothing here is a wish list.

"You" is the user running the tools: the owner of the fork and of the GitHub
token (default login `joseluisll`).

## 1. Purpose and structure

- **R1** The tools shall track the pull requests opened from a personal fork into an
  upstream Apache project's base branch: why each one is not merged yet, what it
  depends on, which CI failures block it, and whether a JIRA issue exists for them.
  *Source:* README.md.
- **R2** Every tool shall be a standalone Python 3.12+ command-line script at the
  repository root, using only the standard library, except `pr_manager.py`, which
  also needs `textual` (the only entry of `requirements.txt`), and `pr_graph.py`,
  which is a library module without a command line.
  *Source:* requirements.txt, pr_manager.py imports, pr_graph.py.
- **R3** `analyze_pr.py` shall be the shared core that the other scripts import;
  `list_upstream_prs.py` shall sit next to it (its check, Yetus and review analysis
  is reused). *Source:* analyze_pr.py docstring, imports of every script.
- **R4** Every script shall print usage with `--help` and run its work only from
  `main()` under `if __name__ == "__main__"`; `pr_graph.py` has no command line.
  *Source:* every script, ci.yml.

## 2. Profiles and configuration

- **R5** Two project profiles shall exist, `hadoop` (default) and `hbase`, selected by
  `--profile` or `$PRTRACKER_PROFILE`; an unknown profile shall stop the script with
  the list of known ones. *Source:* `analyze_pr.PROFILES`, `use_profile`, `profile_parser`.
- **R6** A profile shall define the repository and base branch (`apache/hadoop`
  `trunk`; `apache/hbase` `master`), the precommit bot logins, the JIRA projects
  recognised in text and those searched for fixes, the default project for new
  issues, the source-tree→project map, the PR-title separator, the clone name and
  its environment variable, and the nightly build source. *Source:* `PROFILES`.
- **R7** The selected profile shall be exported to `$PRTRACKER_PROFILE`, so scripts
  started by another script use the same profile. *Source:* `use_profile`, pr_manager.py.
- **R8** The local clone shall be `--repo-path` if given, else `$HADOOP_REPO_PATH` /
  `$HBASE_REPO_PATH`, else `C:\dev\<clone>` on Windows and `~/code/<clone>` elsewhere.
  The branch scripts shall default to the current directory when it is a clone.
  *Source:* `default_repo_path`, README "Setup", `list_stale_branches.Git`.
- **R9** The default author / fork owner / reviewer shall be `joseluisll`, overridable
  per script (`--author`, `--fork-owner`, `--user`, `--reporter`, `--all-open AUTHOR`).
  *Source:* `DEFAULT_AUTHOR`, `DEFAULT_FORK_OWNER`.

## 3. Credentials

- **R10** The GitHub token shall come from `--token`, else `$GITHUB_TOKEN`, else
  `$GH_TOKEN`, else `gh auth token`; without one, requests go out anonymously.
  *Source:* `list_upstream_prs.resolve_token`.
- **R11** The JIRA token shall be read only from `$JIRA_TOKEN` or `$JIRA_PAT`, never
  from the command line; it is needed only for writes, and JIRA reads are anonymous.
  *Source:* `fix_dependencies.jira_token`, `analyze_pr.jira_get`.
- **R12** Credentials shall never be read from files in this repository and never be
  printed or shown in the UI. *Source:* README "Setup", pr_manager.py docstring.

## 4. Network access

- **R13** Every HTTP request shall be retried up to 3 times on 429, 502, 503, 504, a
  dropped connection, a TLS error or a timeout, waiting 2 s × attempt between tries.
  *Source:* `list_upstream_prs.fetch`, `analyze_pr.http_get`.
- **R14** An unreachable GitHub API or a GraphQL error shall end the script with a
  clear message; an HTTP error shall carry its status (`GitHubAPIError`) so a caller
  can react to it. *Source:* `list_upstream_prs.graphql`.
- **R15** `review_queue.py` shall halve its PR page size (25 down to at least 5) when
  GitHub answers 502 or 504 for a page, and fail only when it is already at 5.
  *Source:* `review_queue.fetch_open_prs`.
- **R16** The GitHub token shall not be forwarded on redirects (signed storage URLs of
  logs and artifacts). *Source:* `analyze_pr.http_get`.
- **R17** Finished build logs and Yetus artifacts shall be cached as JSON on disk in
  the temp directory, a failed read shall not be cached, and the committer list of
  `review_queue.py` shall be cached for the day. *Source:* `disk_cached`,
  `build_log_failures`, `yetus_artifact`, `review_queue.fetch_mergers`.

## 5. Safety model

- **R18** Every script that can write (`fix_dependencies.py`, `create_jira.py`,
  `merge_preparation.py`, `build_generated.py`) shall be a dry run by default and
  only print what it would change. *Source:* README "Safety model", each script.
- **R19** With `--apply`, each JIRA link, PR-description edit and new issue shall be
  printed in full (headline, evidence, payload) and asked for with its own `[y/N]`;
  without a terminal the change shall be skipped, and Ctrl-C/EOF shall abort.
  *Source:* `fix_dependencies.confirm`, `show`; `create_jira.main`.
- **R20** `--force` shall be accepted only with `--apply`; it answers yes but still
  prints every change before writing it. *Source:* `parse_args` of fix_dependencies.py
  and create_jira.py.
- **R21** `create_jira.py --force` shall never create an issue that may already be
  filed: such a proposal is skipped; without `--force` the user is asked. A duplicate
  search that failed counts as "may be filed". *Source:* `create_jira.already_filed`,
  `create_jira.main`.
- **R22** `list_upstream_prs.py`, `analyze_pr.py`, `review_queue.py`, `qbt_jira.py`,
  `list_stale_branches.py`, `list_my_jiras.py` and `list_feat_branches.py` shall not
  write to GitHub or JIRA (the branch scripts may `git fetch`). *Source:* README,
  module docstrings.
- **R23** The dependency tools shall never create a JIRA issue; a missing one is
  reported with a suggested summary. *Source:* README "Safety model", `fix_dependencies`.
- **R24** A PR description shall be edited only when the PR was opened by the owner of
  the GitHub token, checked when planning and again right before the write; when the
  login is unknown, the write is refused. *Source:* `fix_dependencies.plan_for`,
  `update_pr_body`.
- **R25** Right before each write the current state shall be read again: the JIRA
  links before a link is made (no duplicate, no cycle closed), the PR description
  before it is replaced (no concurrent edit lost), possible duplicates before an
  issue is created. *Source:* `create_link`, `update_pr_body`, `create_jira.main`.
- **R26** A change whose credential is missing shall be skipped with a warning, not
  attempted. *Source:* `fix_dependencies.main`, `pr_manager.apply_selected`.
- **R27** A writing script shall exit non-zero when any write failed.
  *Source:* `fix_dependencies.main`, `create_jira.main`, `merge_preparation.main`,
  `build_generated.main`.
- **R28** No script shall force-push; a push that cannot fast-forward is reported as
  failed. *Source:* `merge_preparation.merge_branch`, `build_generated.request_build`.

## 6. Open PR list (`list_upstream_prs.py`)

- **R29** It shall list the open PRs of an author into the upstream repository, keeping
  those on the base branch (`--base`, `*` for any), whose head repository belongs to
  the fork owner (unless `--any-head-repo`), and not drafts (unless
  `--include-drafts`), newest number first. *Source:* `main`.
- **R30** It shall print `ID | Title | Branch | Status | Comments` as a table sized to
  the terminal (at least 80 columns, or `--width`), as markdown or as JSON.
  *Source:* `render_table`, `render_markdown`, `main`.
- **R31** The status shall be the first that applies of: DRAFT, CONFLICTS, CHANGES
  REQUESTED, YETUS FAILED, CI FAILED, CI RUNNING, READY TO MERGE, WAITING FOR REVIEW.
  *Source:* `evaluate`.
- **R32** The head commit's checks shall be split into GitHub Actions and Yetus/Jenkins
  (name matching `yetus|jenkins`), counting `X` and `X(jenkins)` once.
  *Source:* `classify_contexts`.
- **R33** The comment by a precommit bot that contains "overall" and has the newest
  `createdAt` shall be read for the overall vote, the -1 subsystems and whether a rebase
  is required. *Source:* `parse_yetus_comment`.
- **R34** The comments shall give the evidence: CI, Yetus, conflicts, review situation
  (changes requested, approved, pending reviewers, comments only, no reviewer) and
  inactivity of `--stale-days` (default 14) or more. *Source:* `evaluate`.

## 7. One PR's analysis (`analyze_pr.py`)

- **R35** It shall accept one or more targets, each a PR number or a JIRA id of a
  recognised project; a JIRA id resolves to its PR, and other PRs naming the same id
  are mentioned. *Source:* `resolve_target`, `main`.
- **R36** It shall print per target: JIRA id and title, PR id and title, PR status,
  PR comments (why that status), Depends on (both directions) and Suggestion (an
  ordered course of action towards MERGED), as a report, markdown or JSON.
  *Source:* `analyse`, `render_report`, `render_markdown`.
- **R37** It shall read the JIRA issue (summary, status, resolution, assignee, labels,
  description, latest comments, links) and the PR (title, body, reviews, comments,
  checks). *Source:* `fetch_jira`, `PR_QUERY`.
- **R38** CI failures shall come from the Yetus comments, the Jenkins unit logs, the
  latest failed GitHub Actions runs of the branch (at most 6) and spotbugs reports;
  under the `hbase` profile, from the Yetus artifacts of the Actions runs of the last
  3 head commits, treated as one Yetus comment per commit; without a GitHub token, a
  warning is printed and no Yetus result is read. *Source:* `parse_ci_failures`,
  `actions_failures`, `actions_yetus_comments`, README "Profiles".
- **R39** Under `hbase`, an artifact of a passing job larger than 5 MB shall not be
  downloaded and shall count as a passing unit row. *Source:* `YETUS_ZIP_PASSED_MAX`.
- **R40** Each Yetus -1 shall come with practical advice for its subsystem.
  *Source:* `YETUS_ADVICE`.
- **R41** It shall suggest reviewers from the clone's git history of the touched
  modules unless `--no-reviewer-hints`. *Source:* `module_owners`.

### 7.1 Dependencies

- **R42** Dependencies shall be found from: the managed block of the description;
  wording such as "depends on #N" in the description, non-bot comments and the JIRA
  description (same sentence, no negation, github.com links only to this repository);
  unresolved JIRA blocking links; stacked branches (one head contains the other, or the
  PR targets another PR's head branch); the diffs (a symbol one introduces and the
  other uses, the same lines edited, shared files); and CI (one PR clears a -1 of the
  other). *Source:* `collect_dependencies`, `text_references`.
- **R43** Peers shall be the author's open PRs, or every open PR with `--peers all`;
  at most 8 peer diffs shall be read, declared ones first, then by shared files.
  *Source:* `collect_dependencies`.
- **R44** Each dependency shall carry one verdict with its reason: CONFIRMED (stacked,
  or uses a symbol of the other), CI-FIX (strong CI match), LIKELY (same lines, or
  medium CI match), WEAK (weak CI match or shared file only), UNSUPPORTED (diffs read,
  nothing in common), UNVERIFIED (no PR, or diff unreadable), DISCOVERED (found and not
  declared), STALE (see R45), and MERGED / CLOSED for a PR no longer open, whatever the
  evidence. *Source:* `verdict_for`, `_verdict_from`.
- **R45** A CI-FIX shall become STALE only when the failure is absent from the latest
  run of each check that reported it, was last seen `STALE_DAYS` (30) or more days
  ago, at least one green run of that check happened since, and nothing else supports
  the dependency. *Source:* `ci_stale`, `verdict_for`.
- **R46** An undeclared CI fix shall be DISCOVERED only for a failure still red in the
  latest run and a strong match; somebody else's PR qualifies only when it is open,
  matches strongly under the stricter search (it names the spotbugs class or bug type,
  its Yetus reports do not refute it) and has a JIRA key. *Source:* `collect_dependencies`,
  `existing_fixes`.
- **R47** A failure still red that no dependency clears shall be reported (at most 10)
  with the merged or open PRs and JIRAs that may cover it, or a suggested new JIRA
  summary `PROJECT: summary` when there is none. *Source:* `UNEXPLAINED_LIMIT`,
  `new_jira_summary`.
- **R48** PRs that only share files shall be listed as a conflict risk, at most 3.
  *Source:* `collect_dependencies`.
- **R49** `--no-deps`, `--no-diffs` and `--no-ci-search` shall skip respectively the
  dependency search, the diff comparison, and the search among other people's PRs and
  JIRA. *Source:* `analyze_pr.parse_args`.

### 7.2 The managed block

- **R50** Dependencies written to a PR description shall live between
  `<!-- dependencies: maintained by analyze_pr.py -->` and `<!-- /dependencies -->`,
  in a "**Depends on** (has to be merged first):" and a "**Required by** (waits for this
  one to be merged):" section separated by a blank line; text outside the block shall
  never change; a new block goes at the top; an empty block is removed; CRLF endings
  are normalised. *Source:* `BLOCK_START`, `apply_block`, `render_block`.

## 8. Dependency writer (`fix_dependencies.py`)

- **R51** Targets shall be PR numbers, JIRA ids or `--all-open [AUTHOR]`; a target with
  no open PR is skipped with a message. *Source:* `main`.
- **R52** Open CONFIRMED, CI-FIX and DISCOVERED dependencies shall be written as one
  JIRA "Blocker" link per pair (in whichever direction is missing) and as entries of
  the managed block. *Source:* `WORTH_DECLARING`, `plan_for`, `link_add`.
- **R53** A JIRA link recorded but judged WEAK, UNSUPPORTED or STALE shall be proposed
  for deletion, unless it is planned, resolved, or still planned by another PR of the
  run; LIKELY and UNVERIFIED links are kept; the preview shall say how to put the link
  back. *Source:* `WORTH_DELETING`, `plan_for`, `supported_pairs`.
- **R54** Two PRs that each clear a -1 of the other shall get no JIRA link (it would be
  a cycle) and a "merge them back to back" note instead. *Source:* `solid_lists`.
- **R55** When both PRs are yours, both descriptions shall get their side; a PR of yours
  outside the run gets a single added item; somebody else's description is never
  edited; the same write shall be proposed once per run. *Source:* `plan_all`.
- **R56** `--jira-only` shall leave descriptions alone and `--pr-only` shall leave JIRA
  alone. *Source:* `plan_for`.
- **R57** `--add-link BLOCKED:BLOCKER` and `--flip-link BLOCKED:BLOCKER` shall accept
  only two recognised JIRA ids; a new link shall be read back to check its direction,
  and a failed flip shall print how to restore the old link. *Source:* `link_change`,
  `flip_change`, `create_link`.
- **R58** A dependency claimed in the prose of a description and UNSUPPORTED by the
  diffs shall be reported, never rewritten. *Source:* `prose_notes`.
- **R59** `--stale-days` (default 30) shall set the STALE window of R45.
  *Source:* `fix_dependencies.parse_args`, `main`.

## 9. Issue creator (`create_jira.py`)

- **R60** From PR targets it shall propose one issue per missing JIRA reported by R47,
  merged across the PRs that see it, with a wiki-markup description of the evidence
  and those PRs. *Source:* `proposals_from_prs`, `describe`.
- **R61** One issue shall be given by hand with `--project` and `--summary` (together),
  optionally `--description-file` (only with `--project`). *Source:* `parse_args`.
- **R62** Unresolved issues of the project with the same words in their summary shall
  be shown as possible duplicates, and searched again right before each creation.
  *Source:* `already_filed`, `main`.
- **R63** `--type` (Bug), `--priority` (Major), `--component`, `--label` and
  `--assign-me` (the owner of the JIRA token) shall shape the issue.
  *Source:* `create_issue`.
- **R64** A created issue shall be read back, and the `--add-link` commands that would
  record the dependency printed; nothing else is written. *Source:* `create_issue`, `main`.
- **R65** `--apply` without a JIRA token shall stop before any write with how to get
  one. *Source:* `main`.

## 10. Terminal UI (`pr_manager.py`)

- **R66** It shall offer tabs Pull requests, Analysis, Dependencies and Branches, with
  keys `a` analyse, `d` plan, `D` plan all, `l` add link, `g` go to PR/JIRA, `o` open
  PR, `j` open JIRA, `r` reload, `f` fetch branches, `q` quit. *Source:* `PRManager.BINDINGS`.
- **R67** The PR list shall include drafts (stacked drafts carry dependencies) and keep
  the PRs on `--base`. *Source:* `load_prs`.
- **R68** Every write shall be shown in full in a dialog and need its own yes; there
  shall be no "apply all"; each item shows pending, applied, failed or skipped, and the
  cached data of the PR written to is dropped. *Source:* `apply_selected`, `applied`.
- **R69** Plan options shall be switchable: search other PRs and JIRA, JIRA only, PR only.
  *Source:* `option_changed`.
- **R70** "Plan all" shall write `pr-graph.svg` in the working directory; a failure of
  the graph shall not stop the plan. *Source:* `write_graph`.
- **R71** The Branches tab shall run `list_stale_branches.py --format json --all` on
  the clone and only report; without a clone it shall say how to give one.
  *Source:* `load_branches`.
- **R72** Without `textual` it shall exit with the install command. *Source:* imports.
- **R73** Reload shall drop the in-memory caches; the on-disk log caches stay.
  *Source:* `CORE_CACHES`, `action_reload`.

## 11. Dependency graph (`pr_graph.py`)

- **R74** It shall draw, in pure Python as SVG, your open PRs and the dependencies the
  plan writes (open, CONFIRMED / CI-FIX / DISCOVERED): one arrow per pair from the PR
  to merge first, coloured by the strongest verdict, double-headed when mutual;
  somebody else's PR or a JIRA with no PR as a dashed box; prerequisites in the left
  columns; arrows never behind a box; the reasons on hover; each box linked to its
  PR; PRs with no planned dependency listed below. *Source:* module docstring,
  `build_graph`, `render_svg`.

## 12. Nightly build candidates (`qbt_jira.py`)

- **R75** It shall read the latest build and the `--history` (default 7, at least 1)
  builds before it of every Hadoop qbt job matching the profile's pattern, or of the
  HBase Nightly pipeline's stages; `--job` picks jobs and `--build` (with exactly one
  job) a build. *Source:* `parse_args`, `main`, `staged_runs`.
- **R76** Candidates shall be failing test classes, failing plugin goals and fork
  timeouts per module, spotbugs warnings grouped by the module whose source holds them
  (from the clone), and with `--include-lint` tree-wide lint -1 votes.
  *Source:* `build_candidates`.
- **R77** Each candidate's effect on the open PRs updated in the last `--pr-days`
  (default 90) shall be measured: it clears their latest -1, is part of it, is only one
  of a whole-repo spotbugs run's warnings (no help), was in an earlier run, or the PR
  changes the module. *Source:* `pr_impact`, `helps`.
- **R78** Each candidate shall be scored with the `POINTS` weights, every point printed
  with its reason, and tiered P1 (≥ 40), P2 (≥ 20), P3. *Source:* `score`, `TIERS`.
- **R79** A candidate shall not be proposed when an open PR (or one merged since the
  failing build) fixes it by the matching of `analyze_pr.py`, or an unresolved JIRA
  (or one resolved since) names it; each PR lead shall be checked against its Yetus
  reports as verified, partial, refuted or not verified, and a refuted one does not
  count. *Source:* `tracking`, `existing_fixes`, `yetus_verdict`.
- **R80** The output shall start with every candidate ranked by the open PRs it helps,
  then score, saying who works on it, whatever its score; `--show-discarded` adds the
  tracked ones. *Source:* `rank_key`, `render_text`, `render_markdown`, `main`.
- **R81** It shall never write: `--save-dir` writes each description to a file and
  prints the `create_jira.py` command that would file it. *Source:* `save`, `main`.
- **R82** Without `--min-score` every untracked candidate shall be a proposal, whatever
  its score; `--min-score N` shall hide proposals scoring below N, and `--limit` cap their
  number. Formats text, markdown and JSON, with progress on stderr outside text.
  *Source:* `main`.
- **R83** Under `hbase`, a failed goal shall be put on the project its unit log names,
  and tests that failed before a rerun passed them shall be listed.
  *Source:* `staged_runs`, `flaky_tests`, README.

## 13. Review queue (`review_queue.py`)

- **R84** It shall list every open PR into the base branch (or any with `--base '*'`),
  grouped by component: the Maven module (nearest `pom.xml` in the clone's base branch)
  with most of the PR's changed lines, else the path before `src/`.
  *Source:* `maven_modules`, `module_of`, `classify`.
- **R85** Each row shall show topic, author, files, changed lines, age, review state and
  the Yetus verdict of the latest commit (`stale` when older); your PRs marked `*`.
  *Source:* `classify`, `render_components`.
- **R86** Each PR shall be scored with the `POINTS` weights, each point with its reason
  shown under `--explain`. *Source:* `score`.
- **R87** Your own PRs shall never be recommended; drafts only with `--include-drafts`.
  *Source:* `recommend`.
- **R88** A committer shall be whoever merged a PR in the last `--committer-days`
  (default 365), shows as OWNER/MEMBER/COLLABORATOR on a review or comment, or is given
  with `--committer`; the PR's author and you do not count. *Source:* `fetch_mergers`,
  `associated_committers`, `committers_engaged`.
- **R89** Your area shall be the components with at least 2 main-code changes: your
  commits (by the clone's `user.email`) in the last `--area-days` plus your open PRs;
  `--focus` replaces it. *Source:* `your_history`, `your_area`, `score`.
- **R90** The top `--top` (10) shall hold at most `--max-per-component` (3) PRs of one
  component; `--top` must be at least 1. *Source:* `parse_args`, `recommend`.
- **R91** `--view`, `--mine`/`--others` (exclusive), `--component`, `--format`
  (table, markdown, JSON) and `--from-json` (offline input) shall shape the output.
  *Source:* `parse_args`, `main`.
- **R92** It shall only read: GraphQL queries, git read commands and, under `hbase`,
  read-only GitHub Actions REST calls for the Yetus artifacts. *Source:* README,
  `fetch_open_prs`, `analyze_pr.add_actions_yetus`.

## 14. Fork branches

### `list_stale_branches.py`

- **R93** Each fork branch shall be ACTIVE (head of an open PR, or protected), CANDIDATE
  (related to an open PR, an earlier PR closed unmerged, ahead of or diverging from the
  upstream branch of the same name, recent work, or anything uncertain) or STALE
  (earlier PR merged, mirror or outdated copy of upstream, nothing of its own, or old
  and unrelated). *Source:* `classify`.
- **R94** Relation to an open PR shall come from shared history (up to
  `--max-pr-commits`, 25), a shared JIRA key, or earlier PRs of the same branch.
  *Source:* `compute_relations`, `apply_jira_relations`.
- **R95** Mirrors of upstream shall be hidden unless `--include-mirrors`, ACTIVE unless
  `--all`; `--only` filters statuses; `--protect` (default: the base branch plus `trunk`,
  `main` and `master`) is never stale; a truncated comparison makes a branch CANDIDATE.
  *Source:* `parse_args`, `classify`.
- **R96** `--no-fetch`, `--offline` and `--fetch-upstream-branches` shall control git
  network access; output as table, markdown or JSON; nothing is deleted.
  *Source:* `parse_args`, `main`.

### `list_feat_branches.py`, `merge_preparation.py`, `build_generated.py`

- **R97** `list_feat_branches.py` shall compare, for each `feat/<name>`, the generated
  `<name>` branch with it and `<name>-upstream` with the generated one, as "same code"
  when they change the same lines outside `.github/` over their own trunk merge-base,
  with how far each base is behind, and name the PR. *Source:* `read_tip`, `report`.
- **R98** It shall then list the other fork branches by PR state, and note staged tags
  behind or with other code, fork-only `.github/` changes, PR heads out of date and
  local branches ahead, behind or local only. *Source:* `report`.
- **R99** It shall fetch `staged/*` tags into `refs/prtracker/staged/`, leaving the
  clone's own tags alone. *Source:* `STAGED_REFS`, `main`.
- **R100** `merge_preparation.py` shall merge `origin/PREPARATION` into each `feat/`
  branch whose generated branch is on an older trunk, using `git merge-tree` and
  `git commit-tree` (no worktree or local branch touched), report conflicts and leave
  those branches alone, flag `.github/`-only merges, and push only with `--apply`.
  *Source:* `merge_branch`, `behind_branches`.
- **R101** `build_generated.py` shall push `PREPARATION` to `build/<name>` for those
  same branches, only with `--apply`, and skip, saying why, when a request is pending,
  `staged/<name>` is missing, holds other code than `feat/<name>` or is no newer, or
  `<name>` holds work `feat/<name>` lacks. *Source:* `request_build`.

## 15. Your JIRA issues (`list_my_jiras.py`)

- **R102** It shall list, as a markdown table, the unresolved issues reported by
  `--reporter` in the profile's searched projects (paged by 100), each with status,
  summary and its PR (open, else latest; the first JIRA key of the title decides, any
  author) with Yetus result and date, Build check and approvals; a run still waiting for
  approval, skipped, neutral or stale is reported as such. Read-only. *Source:*
  `my_issues`, `prs_of`, `row`, `fork_verdict`.
- **R111** It shall also show the fork's latest Build run on the generated branch
  `<KEY>` and latest Yetus run on `yetus/<KEY>`, "older than tip" when the run did not
  test the branch's current commit (Build: `head_sha` differs from the tip; Yetus: the
  tip's "Yetus" status does not point at the run), "No branch" when the branch is not on
  the fork, `--fork` (default `joseluisll/<repo name>`), and the verdict labels of R102.
  *Source:* `report`, `fork_verdict`, `fork_cell`.

## 16. Automation (GitHub Actions)

- **R112** `pr_status.py` shall report, for one `<name>`: the `feat/<name>` tip, its
  trunk base and whether PREPARATION is ahead; what `staged/<name>` holds against it; the
  branch the PR is opened from against it; the fork's newest Build run on that branch and
  Yetus run on `yetus/<name>`, saying when they did not test the branch tip; and the PR.
  It shall end with one next action in workflow order: push `feat/`, publish
  (`build/<head>`), open the PR, fix a red Build, run Yetus (`yetus/<head>`), or done, and
  say instead when `build/<name>` cannot publish the head (the `jetty-phase-*` PRs). `--all`
  shall print that line for every `feat/` branch; `feat/HARNESS` has nothing to publish.
  Read-only. *Source:* `status`, `next_step`, `feat_names`.

- **R103** CI shall run on pull requests and pushes to `main`, on Ubuntu and Windows
  with Python 3.12: compile every script, run each script's `--help`, and run the
  offline smoke tests (`plan`, `graph`, `qbt`, `review`, `branches`, `merge`, `build`,
  `jiras` and `create`). *Source:* ci.yml.
- **R104** `dependencies.yml` shall run daily at 05:17 UTC and on demand (PR list,
  apply switch), running `fix_dependencies.py --pr-only --peers all` with
  `--apply --force` when applying, against a checkout of the project's base branch; it
  shall fail when `PREPARATION_PUSH_TOKEN` is missing and never touch JIRA.
  *Source:* dependencies.yml.
- **R105** `qbt-report.yml` shall run `qbt_jira.py --show-discarded` on odd days at
  06:23 UTC for both profiles one after the other, publish the report as the summary
  and keep report, log and proposals as an artifact for 30 days. *Source:* qbt-report.yml.
- **R106** `review-queue.yml` shall run `review_queue.py --explain` on odd days at
  06:41 UTC for both profiles one after the other, with `PRTRACKER_USER` and
  `PRTRACKER_EMAIL` as optional variables, and keep the report for 30 days.
  *Source:* review-queue.yml.
- **R107** The three scheduled workflows shall share the concurrency group `prtracker`
  without cancelling a running job, and hold only `contents: read` permissions.
  *Source:* the three workflows.
- **R108** Workflow inputs shall be split into words without glob expansion.
  *Source:* `set -f` in dependencies.yml and review-queue.yml.

## 17. Quality

- **R109** Offline smoke tests shall cover the dependency plan, the graph, qbt parsing
  and scoring, the review queue, the feat/ branch report, the PREPARATION merge, the
  build requests, the JIRA list and `create_jira.py`; `tests/tui_smoke.py` drives the UI
  against live PRs with every write stubbed. *Source:* tests/, README "Tests".
- **R110** The scripts shall work on Linux and Windows, and print titles the console
  cannot encode without crashing. *Source:* ci.yml matrix, `sys.stdout.reconfigure`.
