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
| `list_stale_branches.py` | Lists fork branches not related to any open PR (by history, JIRA key or earlier PRs) as STALE or CANDIDATE for cleanup. Read-only. |

`analyze_pr.py` is the shared core; the others import it.

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

Some commands look at a local Hadoop checkout; point them at it with `--repo-path`.

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
```

## Safety model

- **Dry run by default.** Without `--apply` the scripts only print what they would change.
- **`--apply`** asks for a yes/no on every JIRA link, PR-body edit and new issue.
- **`--force`** (only together with `--apply`) answers yes instead of asking; each change is
  still printed before it is written. In `create_jira.py` an issue that may already be filed
  is skipped rather than created.
- Missing JIRAs are reported, never created implicitly by the dependency tools.

## Tests

```bash
python tests/plan_smoke.py
python tests/tui_smoke.py
```

`plan_smoke.py` is offline. `tui_smoke.py` drives the UI headlessly against the live PRs
(needs GitHub access) with every write stubbed out.

## License

Apache License 2.0, see [LICENSE](LICENSE).
