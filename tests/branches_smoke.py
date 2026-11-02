"""Offline smoke test of list_feat_branches.py: compare made-up branches in a throwaway clone."""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import list_feat_branches as lfb
from list_stale_branches import Git


def pr(number, head, title, state="OPEN", sha=None, draft=False):
    return {"number": number, "title": title, "url": f"https://example.org/{number}", "state": state,
            "isDraft": draft, "headRefName": head, "headRefOid": sha or "0",
            "headRepositoryOwner": {"login": "me"}}


with tempfile.TemporaryDirectory() as repo:
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(path, text):
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        git("add", "-A")
        git("commit", "-qm", path)
        return git("rev-parse", "HEAD")

    def on_remote(name, sha):
        git("update-ref", f"refs/remotes/{name}", sha)

    git("init", "-q")
    old_trunk = commit("a.txt", "1\n")
    trunk = commit("b.txt", "2\n")
    on_remote("upstream/trunk", trunk)

    # feat/ is on trunk; generated has the same change on the older trunk, plus .github/.
    git("checkout", "-q", "-b", "work", trunk)
    feat = commit("c.txt", "same\n")
    git("checkout", "-q", "-b", "gen", old_trunk)
    commit("c.txt", "same\n")
    gen = commit(".github/w.yml", "fork\n")
    git("checkout", "-q", "-b", "up", trunk)
    up = commit("c.txt", "same\n")
    git("checkout", "-q", "-b", "other", trunk)
    other = commit("c.txt", "other\n")

    on_remote("origin/feat/HADOOP-1", feat)
    on_remote("origin/HADOOP-1", gen)
    on_remote("origin/HADOOP-1-upstream", up)
    on_remote("origin/feat/HDFS-2", feat)
    on_remote("origin/HDFS-2", other)
    on_remote("origin/feat/HARNESS", feat)
    on_remote("origin/feat/YARN-3", feat)
    on_remote("origin/YARN-3", up)
    on_remote("origin/phase-a", up)
    on_remote("origin/YARN-4", up)
    on_remote("origin/YARN-5", up)
    on_remote("origin/stall-repro", up)
    git("update-ref", f"{lfb.STAGED_REFS}/HADOOP-1", up)
    git("update-ref", f"{lfb.STAGED_REFS}/YARN-3", up)
    git("update-ref", "refs/heads/feat/HADOOP-9", feat)

    text = lfb.report(Git(repo), [
        pr(1, "HADOOP-1-upstream", "HADOOP-1. Fix", sha=up),
        pr(3, "phase-a", "YARN-3. Fix", sha=up, draft=True),
        pr(4, "YARN-4", "YARN-44. Fix", state="MERGED", sha=up),
        pr(5, "gone", "YARN-6. Fix", state="CLOSED"),
    ], "me")

rows = {line.split(" | ")[0].strip("| `"): line for line in text.splitlines() if line.startswith("|")}
assert "| Same code, base 1 behind | 2 | Same code, base current, 1 commit | [#1](https://example.org/1), open | `-upstream` |" in rows["feat/HADOOP-1"], text
assert "Code differs, same base" in rows["feat/HDFS-2"], text
assert "| none | – |" in rows["feat/HDFS-2"], text
assert "No generated branch" in rows["feat/HARNESS"], text
assert "open draft | `phase-a` |" in rows["feat/YARN-3"], text
assert "phase-a" not in rows, text  # shown as the PR head of feat/YARN-3
assert "[#4](https://example.org/4) (YARN-44)" in rows["YARN-4"], text
assert "- `YARN-5`" in text and "stall-repro" not in text, text
assert "4 `feat/` branches on origin: 2 with a PR, 2 without" in text, text
assert "not at `staged/<name>`: `HADOOP-1`." in text, text
assert "by file count:** `HADOOP-1` (1)." in text, text
assert "every open PR matches" in text, text
assert "**Local only:** `feat/HADOOP-9`." in text, text
assert "1 merged or closed PR whose" in text, text
print("branches smoke ok")
