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
| `list_upstream_prs.py` | Lists your open PRs into `apache/hadoop:trunk` and why each one is not merged. |
| `analyze_pr.py` | Full report for one PR or JIRA id: CI (GitHub Actions and Yetus), reviews, JIRA state, dependencies with a verdict each. |
| `fix_dependencies.py` | Writes the dependencies `analyze_pr.py` finds as JIRA "Blocker" links and as a managed block in the PR description. Proposes removing UNSUPPORTED and STALE ones. |
| `create_jira.py` | For CI/Yetus failures no PR or JIRA addresses, proposes (and on `--apply`, creates) a new JIRA. |
| `qbt_jira.py` | Reads the nightly trunk qbt reports, ranks what they show by the open PRs fixing it would help, and proposes JIRA issues for the failures nobody tracks yet. Dry run only. |
| `list_stale_branches.py` | Lists fork branches not related to any open PR (by history, JIRA key or earlier PRs) as STALE or CANDIDATE for cleanup. Read-only. |

`analyze_pr.py` is the shared core; the others import it.

### qbt JIRA candidates

`qbt_jira.py` reads the latest build of every `hadoop-qbt-trunk-javaNN-linux-x86_64`
job on [ci-hadoop.apache.org](https://ci-hadoop.apache.org) (today JDK 17 and JDK 21), plus
the builds before it (`--history`, default 7), and turns what they show into candidates:
failing test classes, plugin goals that fail on a module (its unit vote is -1 with no test
failing), and trunk spotbugs warnings grouped by the module whose source has them
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

### Dependency verdicts

`CONFIRMED`, `CI-FIX`, `LIKELY`, `WEAK`, `UNSUPPORTED`, `UNVERIFIED`, `DISCOVERED`, `STALE`.

A `CI-FIX` dependency becomes `STALE` when the failure it cleared is absent from the
latest CI run, has not been seen for `--stale-days` (default 30) days, and at least one
green run of the same check has happened since.

## Setup

Python 3.12 or later.

```bash
pip install -r requirements.txt
```

Credentials come only from the environment, never from files in this repository:

- **GitHub**: `gh auth login`, or `GITHUB_TOKEN`. `--token` overrides both.
- **JIRA** (only for writes): a personal access token for issues.apache.org in
  `JIRA_TOKEN` or `JIRA_PAT`. Reads work without it.

Some commands look at a local Hadoop checkout, with `origin` your fork and `upstream`
apache/hadoop. They use `--repo-path` if given, else `$HADOOP_REPO_PATH`, else
`C:\dev\hadoop` on Windows and `~/code/hadoop` elsewhere.

## Examples

```bash
python pr_manager.py
python list_upstream_prs.py
python analyze_pr.py 8704
python analyze_pr.py HADOOP-19972 --format markdown
python fix_dependencies.py --all-open
python fix_dependencies.py 8704 --apply
python fix_dependencies.py --add-link MAPREDUCE-7545:HADOOP-19972 --apply
python create_jira.py 8704
python create_jira.py 8704 --apply --assign-me
python list_stale_branches.py
python qbt_jira.py
python qbt_jira.py --show-discarded --show-description
python qbt_jira.py --job hadoop-qbt-trunk-java21-linux-x86_64 --build 113 --format markdown
python qbt_jira.py --save-dir proposals
```

## Safety model

- **Dry run by default.** Without `--apply` the scripts only print what they would change.
- **`--apply`** asks for a yes/no on every JIRA link, PR-body edit and new issue.
- **`--force`** (only together with `--apply`) answers yes instead of asking; each change is
  still printed before it is written. In `create_jira.py` an issue that may already be filed
  is skipped rather than created.
- Missing JIRAs are reported, never created implicitly by the dependency tools.
- `qbt_jira.py` has no write mode: it only proposes, and leaves filing to `create_jira.py`.

## Tests

```bash
python tests/plan_smoke.py
python tests/qbt_smoke.py
python tests/tui_smoke.py
```

`plan_smoke.py` and `qbt_smoke.py` are offline. `tui_smoke.py` drives the UI headlessly against the live PRs
(needs GitHub access) with every write stubbed out.

GitHub Actions ([ci.yml](.github/workflows/ci.yml)) compiles every script, runs each one's
`--help` and runs the offline smoke tests on Ubuntu and Windows for every pull request into `main`.
`main` is protected: changes go in through a pull request, and only once both checks pass.

## License

Apache License 2.0, see [LICENSE](LICENSE).
