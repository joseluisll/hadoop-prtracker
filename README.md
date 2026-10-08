# prtracker

Tools for tracking pull requests from a personal fork into
[apache/hadoop](https://github.com/apache/hadoop) `trunk`: why each PR is not merged yet,
what it depends on, which CI failures block it, and whether a JIRA exists for them.

Nothing here writes to JIRA or GitHub unless you pass `--apply`, and even then every
change is shown and confirmed one by one.

## Scripts

| Script | What it does |
| --- | --- |
| `pr_manager.py` | Textual terminal UI over all the scripts below: PR list, analysis, dependency plan, fork branches. |
| `pr_graph.py` | Draws the dependencies *Plan all* writes down (CONFIRMED, CI-FIX, DISCOVERED), between your open PRs and others' PRs or JIRAs they need, as `pr-graph.svg` in the working directory. `pr_manager.py` rewrites it on every *Plan all* (`D`). |
| `list_upstream_prs.py` | Lists your open PRs into `apache/hadoop:trunk` and why each one is not merged. |
| `analyze_pr.py` | Full report for one PR or JIRA id: CI (GitHub Actions and Yetus), reviews, JIRA state, dependencies with a verdict each. |
| `fix_dependencies.py` | Writes the dependencies `analyze_pr.py` finds as JIRA "Blocker" links and as a managed block in the PR description. Proposes removing UNSUPPORTED and STALE ones. |
| `create_jira.py` | For CI/Yetus failures no PR or JIRA addresses, proposes (and on `--apply`, creates) a new JIRA. |
| `qbt_jira.py` | Reads the nightly trunk qbt reports, ranks what they show by the open PRs fixing it would help, and proposes JIRA issues for the failures nobody tracks yet. Dry run only. |
| `review_queue.py` | Lists every open PR into `apache/hadoop` per component (Maven module), yours and others', and recommends the top 10 to review. Read-only. |
| `list_stale_branches.py` | Lists fork branches not related to any open PR (by history, JIRA key or earlier PRs) as STALE or CANDIDATE for cleanup. Read-only. |

`analyze_pr.py` is the shared core; the others import it.

### qbt JIRA candidates

`qbt_jira.py` reads the latest build of every `hadoop-qbt-trunk-javaNN-linux-x86_64`
job on [ci-hadoop.apache.org](https://ci-hadoop.apache.org) (today JDK 17 and JDK 21), plus
the builds before it (`--history`, default 7), and turns what they show into candidates:
failing test classes, plugin goals that fail on a module (its unit vote is -1 with no test
failing) or test forks that time out on one, and trunk spotbugs warnings grouped by the module whose source has them
(`--include-lint` adds tree-wide -1 votes such as xml or pathlen).

The output starts with every candidate **ranked by the open PRs it would help**: those whose
latest precommit has a -1 that fixing it clears, or is part of. Each line names those PRs and
their authors, and says who works on it: nobody (a JIRA is proposed), an open PR or JIRA, or a
merged PR (a rebase of the PRs it helps picks the fix up). A PR that changes a root file (a
LICENSE, `hadoop-project/pom.xml`, `.github/`) gets spotbugs run over the whole repo and a -1
for its ~90 old warnings; one of those warnings is no help to that PR, so it does not count.

No JIRA is proposed for a candidate somebody already works on: an open PR, or one merged since
the failing build, that fixes it, or a JIRA issue unresolved (or resolved since that build) that
names it in its summary. The ranking still lists it when it helps open PRs; `--show-discarded`
lists the others as well.

What a PR says is only a lead. It is one when the matching of `analyze_pr.py` says it fixes the
failure (it changes the failing test, the class with the warning, the module whose plugin
fails); for spotbugs its title or description must also name the class (outer or inner) or the
bug type. Every lead is then checked against its Yetus reports, the newest one that checked it:

| Failure | Verified | Refuted |
| --- | --- | --- |
| Spotbugs | `<module> generated N new + U unchanged - F fixed = T total (was W)` with F > 0 (`partial` when it fixes only some) | F = 0, or trunk's warnings of the module and no change in the patch |
| Unit test | the tests of its module ran and it did not fail | it is among the failed tests |
| Plugin goal | the unit run of the module passed | the unit run of the module failed |

A refuted PR does not count (it is listed as `related`); one with no report that checked it
counts, marked `not verified`. Each line of the ranking shows the check of every PR it relies on.

The proposals follow in the same order, with a priority. Every point is printed with its reason:

| Criterion | Points |
| --- | --- |
| Open PR whose latest precommit has it, and fixing it clears that -1 | +10 each |
| ... it is part of the -1 on its own module | +5 each |
| ... it is one of the ~90 warnings of a whole-repo spotbugs run | 0 |
| Open PR that had it only in an earlier precommit | +3 each |
| (all PR points together are capped at 50) | |
| Open PR that changes the module, so its next precommit runs into it | +1 each, max 10 |
| Plugin goal fails (the module's tests never run) | +10 |
| Unit test fails | +6 |
| Spotbugs warning in a bug category (correctness, MT correctness, security) | +5 |
| Other spotbugs warnings | +2 |
| Nightly builds it failed in (tests and builds only) | +2 each, max 14 |
| Failed in every build read, 3 or more (tests and builds: deterministic, not flaky) | +5 |
| Fails with more than one JDK (not for lint) | +5 |
| New: absent from an earlier build read; the commits of the build it appeared in are named | +4 |
| Only aggregate runs report it; its own module run is clean | -5 |

P1 is 40 or more, P2 20 or more, P3 below. The weights are `POINTS` at the top of the script.

Nothing is created: `--save-dir DIR` writes each JIRA description (wiki markup) to a file and
prints the `create_jira.py --project ... --summary ... --description-file ...` command that
would file it once you have reviewed it.

### Review queue

`review_queue.py` lists every open PR into `apache/hadoop:trunk`, yours (`*`) and everybody
else's, grouped by component: the Maven module (nearest `pom.xml` in the clone's trunk) with
most of the PR's changed lines. Each row shows topic, author, files, changed lines, age, review
state and the Yetus verdict of the latest commit (`stale` when it predates that commit).

It then recommends the PRs where a review helps most. Your own PRs and drafts are never
recommended. Every point is printed with its reason under `--explain`:

| Criterion | Points |
| --- | --- |
| Size: <= 20 changed lines / <= 100 / <= 300 / <= 1000 / larger | +20 / +15 / +10 / +4 / 0 |
| More than 20 files | -5 |
| Waiting: days since opened | +1 per 3 days, max 20 |
| Untouched for 180 days (likely abandoned) | -15 |
| Reviews: none yet / comments only / approved | +20 / +12 / +5 |
| Changes requested: new commits since / nothing new (author's turn) | +10 / 0 |
| You reviewed it and nothing changed since | -20 |
| Your review was requested | +10 |
| A committer (write access) commented on it, reviewed it or was asked to review it | +10 |
| Yetus on the latest commit: +1 / -1 / none or stale | +15 / +5 / +3 |
| Merge conflict or Yetus "rebase required" | -10 |
| Topic: security / bug / flaky-test fix / build / feature, docs / dependency bump | +10 / +8 / +7 / +5 / +4 / +3 |
| Changes main code together with its tests | +3 |
| Area: its main component is in your area | +10 |
| ... only another component it touches | +5 |

A committer is somebody who merged a PR into the repository in the last `--committer-days`
(default 365, the last year; the list is cached for the day), whom GitHub shows as `OWNER`, `MEMBER` or
`COLLABORATOR` on a comment or review, or a `--committer LOGIN`. The merges carry it: GitHub
shows most Hadoop committers as `CONTRIBUTOR`, since their apache organization membership is
private, and only collaborators may ask for anyone's permission level. The PR's author and you do
not count. The committers involved are printed on each recommendation.

Your area is the components with at least 2 changes of yours: your commits in the clone's trunk
in the last `--area-days` (default 365) plus your open PRs, each counted once per component.
Only main code counts: files under `src/test/`, `pom.xml` files and root files (outside every
module) are left out, so a test-only or build-only change adds nothing.

`--focus NAME` (component or JIRA project, repeatable) replaces the learned area. The top N
(`--top`, default 10) holds at most `--max-per-component` (default 3) PRs of one component, so
one busy module does not fill the list. The weights are `POINTS` at the top of the script.

Topic comes from the title, labels and paths (keywords such as CVE, fix, NPE, leak, race,
flaky; dependabot or "Bump"/"Upgrade"; only test files; only docs; only poms or `dev-support/`),
so treat it as a hint.

### Dependency verdicts

`CONFIRMED`, `CI-FIX`, `LIKELY`, `WEAK`, `UNSUPPORTED`, `UNVERIFIED`, `DISCOVERED`, `STALE`, `MERGED`, `CLOSED`.

A `CI-FIX` dependency becomes `STALE` when the failure it cleared is absent from the
latest CI run, has not been seen for `--stale-days` (default 30) days, and at least one
green run of the same check has happened since. A dependency on a PR that is no longer
open is `MERGED` or `CLOSED`, whatever its evidence: it leaves the description block, and
its JIRA link is kept as history.

## Setup

Python 3.12 or later.

```bash
pip install -r requirements.txt
```

Credentials come only from the environment, never from files in this repository:

- **GitHub**: `gh auth login`, or `GITHUB_TOKEN`. `--token` overrides both.
- **JIRA** (only for writes): a personal access token for issues.apache.org in
  `JIRA_TOKEN` or `JIRA_PAT`. Reads work without it.

Some commands look at a local checkout of the project, with `origin` your fork and
`upstream` the Apache repository. They use `--repo-path` if given, else `$HADOOP_REPO_PATH`
(`$HBASE_REPO_PATH`), else `C:\dev\hadoop` (`C:\dev\hbase`) on Windows and `~/code/hadoop`
(`~/code/hbase`) elsewhere.

## Profiles

Every script works on Hadoop by default. `--profile hbase`, or `PRTRACKER_PROFILE=hbase` in
the environment, points it at HBase instead; `pr_manager.py` passes its profile on to the
scripts it starts. A profile is one entry of `PROFILES` at the top of `analyze_pr.py`:

| | `hadoop` | `hbase` |
| --- | --- | --- |
| Repository, base branch | `apache/hadoop`, `trunk` | `apache/hbase`, `master` |
| JIRA keys recognised | HADOOP, HDFS, YARN, MAPREDUCE, HDDS, OZONE, SUBMARINE | HBASE |
| New issues go to | HDFS, YARN or MAPREDUCE by source tree, else HADOOP | HBASE |
| PR title convention | `HADOOP-1. Summary` | `HBASE-1 Summary` |
| Precommit | Yetus comments by `hadoop-yetus` | Yetus run in GitHub Actions; its reports are run artifacts |
| Nightly build (`qbt_jira.py`) | `hadoop-qbt-trunk-javaNN-linux-x86_64` jobs on ci-hadoop.apache.org | `HBase Nightly/master` on ci-hbase.apache.org, its stages read as jobs |

HBase posts no Yetus comment on a PR: its Yetus General Check, JDK17 Compile and Unit Check
workflows keep their reports as run artifacts (the zip at the end of the run's summary page).
For each PR the scripts download those zips into `prtracker-yetus` in the temp directory, read
`console.txt` and the failed tests of the `patch-unit-*.txt` logs, and treat each head commit's
reports as one Yetus comment, so the status, the precommit history and the CI-fix dependencies
work as for Hadoop. One PR is read over its last 3 commits, a list of PRs over the latest only.
A zip is downloaded once (cached by artifact id); a passing unit wave's zip (up to 60 MB, every
test's output) is not downloaded at all. The GitHub API calls still take a few seconds per PR,
so `review_queue.py` and `qbt_jira.py` over all open HBase PRs take a few minutes.

HBase's nightly runs the unit tests of the whole tree in one go, so `qbt_jira.py` puts a failed
plugin goal or fork timeout on the project its unit log names (`hbase-server`), and also lists
the tests that log shows failing before a rerun passed them.

## Examples

```bash
python pr_manager.py
python list_upstream_prs.py
python analyze_pr.py 8704
python analyze_pr.py HADOOP-19972 --format markdown
python fix_dependencies.py --all-open
python fix_dependencies.py 8704 --apply
python fix_dependencies.py --all-open --peers all --pr-only
python fix_dependencies.py --add-link MAPREDUCE-7545:HADOOP-19972 --apply
python create_jira.py 8704
python create_jira.py 8704 --apply --assign-me
python list_stale_branches.py
python review_queue.py
python review_queue.py --view recommend --explain
python review_queue.py --view components --others --component hadoop-hdfs-rbf
python review_queue.py --focus YARN --top 15 --format markdown > review.md
python qbt_jira.py
python qbt_jira.py --show-discarded --show-description
python qbt_jira.py --job hadoop-qbt-trunk-java21-linux-x86_64 --build 113 --format markdown
python qbt_jira.py --save-dir proposals
python analyze_pr.py --profile hbase 8730
python qbt_jira.py --profile hbase
```

`--peers all` (`analyze_pr.py`, `fix_dependencies.py`) compares a PR with every open PR of
the repository instead of only the author's: shared files, the symbols one diff introduces and
the other uses, and lines both edit. A CI fix by somebody else's PR is still only proposed when
it passes the stricter search for other people's fixes (it names the spotbugs class or bug
type, and its Yetus reports do not refute it).

## Dependencies workflow

[dependencies-workflow.yml](dependencies-workflow.yml) runs
`fix_dependencies.py --all-open --peers all --pr-only --apply --force` every day, and on demand
(Actions → PR dependencies → Run workflow, with PR numbers, and unchecking *apply* for a dry
run). It rewrites the dependency block of your open PRs; the plan it applied is the run's
summary. Copy it to `.github/workflows/` on the default branch of your fork of the project
(scheduled workflows run only from there). It checks out this repository for the scripts and
the project's base branch (latest commit only) as the clone, without which a symbol already on
trunk looks like one another PR introduces. It needs:

- the secret `PRTRACKER_TOKEN`: a classic personal access token with the `public_repo` scope.
  Only PRs opened by its owner are edited, as with every write of `fix_dependencies.py`.
- optionally the variables `PRTRACKER_PROFILE` (`hbase`; default `hadoop`) and `PRTRACKER_REPO`
  (default `<fork owner>/hadoop-prtracker`).

JIRA is not touched.

## Safety model

- **Dry run by default.** Without `--apply` the scripts only print what they would change.
- **`--apply`** asks for a yes/no on every JIRA link, PR-body edit and new issue.
- **`--force`** (only together with `--apply`) answers yes instead of asking; each change is
  still printed before it is written. In `create_jira.py` an issue that may already be filed
  is skipped rather than created.
- Missing JIRAs are reported, never created implicitly by the dependency tools.
- `review_queue.py` has no write mode: it only runs GraphQL queries and `git ls-tree`/`git log`.
- `qbt_jira.py` has no write mode: it only proposes, and leaves filing to `create_jira.py`.

## Tests

```bash
python tests/plan_smoke.py
python tests/qbt_smoke.py
python tests/review_smoke.py
python tests/tui_smoke.py
```

`plan_smoke.py`, `qbt_smoke.py` and `review_smoke.py` are offline. `tui_smoke.py` drives the UI headlessly against the live PRs
(needs GitHub access) with every write stubbed out.

GitHub Actions ([ci.yml](.github/workflows/ci.yml)) compiles every script, runs each one's
`--help` and runs the offline smoke tests on Ubuntu and Windows for every pull request into `main`.
`main` is protected: changes go in through a pull request, and only once both checks pass.

## License

Apache License 2.0, see [LICENSE](LICENSE).
