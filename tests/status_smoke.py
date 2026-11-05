"""Offline smoke test of pr_status.py: the next action for made-up branches on a local bare fork."""
import contextlib, io, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)
import pr_status
from list_stale_branches import Git


with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "repo")
    fork = os.path.join(tmp, "fork.git")
    os.mkdir(repo)
    subprocess.run(["git", "init", "-q", "--bare", fork], check=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    def commit(path, text):
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        git("add", "-A")
        git("commit", "-qm", path)
        return git("rev-parse", "HEAD")

    def on(branch, start, text):
        git("checkout", "-q", "-B", branch, start)
        return commit("c.txt", text)

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("remote", "add", "origin", fork)
    trunk = commit("a.txt", "1\n")
    git("update-ref", "refs/remotes/upstream/trunk", trunk)
    git("checkout", "-q", "-b", "prep", trunk)
    prep = commit("p.txt", "prep\n")

    feat = on("feat", prep, "new\n")
    stale = on("stale", prep, "old\n")  # what an older push staged
    other = on("other", trunk, "nothing yet\n")
    git("checkout", "-q", feat)
    push = [f"{prep}:refs/heads/PREPARATION",
            f"{feat}:refs/heads/feat/HADOOP-1",  # nothing staged yet
            f"{feat}:refs/heads/feat/HADOOP-2",  # staged as itself
            f"{feat}:refs/heads/feat/HADOOP-3",  # staged as older code
            f"{other}:refs/heads/feat/HADOOP-4"]  # feat predates its own staging
    git("push", "-q", "origin", *push)
    git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
    git("update-ref", "refs/remotes/origin/HADOOP-2", feat)  # the generated branch
    # The staged tags, which main() fetches into refs/prtracker/staged/:
    git("update-ref", "refs/prtracker/staged/HADOOP-2", feat)
    git("update-ref", "refs/prtracker/staged/HADOOP-3", stale)

    # No PRs and no Actions runs: the fork side is what these cases test.
    pr_status.fork_run = lambda *a, **k: None
    pr_status.prs_of = lambda *a, **k: []

    def next_of(name):
        lines = pr_status.status(Git(repo), name, [], "apache/hadoop", None)
        return lines[-1].removeprefix("NEXT: ")

    assert next_of("HADOOP-1").startswith("git push origin feat/HADOOP-1"), next_of("HADOOP-1")
    assert next_of("HADOOP-2").startswith("gh pr create"), next_of("HADOOP-2")
    assert next_of("HADOOP-3").startswith("git push origin feat/HADOOP-3"), next_of("HADOOP-3")
    assert next_of("HADOOP-4").startswith("git push origin feat/HADOOP-4"), next_of("HADOOP-4")
    assert next_of("HADOOP-9") == "git push origin feat/HADOOP-9", next_of("HADOOP-9")

    # HADOOP-2 has neither a Build nor a Yetus run, so one of the two comes next.
    assert pr_status.next_step("HADOOP-2", "HADOOP-2", ["yetus"]) == (
        "git push origin PREPARATION:yetus/HADOOP-2   (Yetus only)")
    assert pr_status.next_step("HADOOP-2", "HADOOP-2", ["build-red"]) == (
        "fix the Build failure, then push feat/ and publish again")
    assert pr_status.next_step("HADOOP-2", "HADOOP-2", ["publish"]) == (
        "git push origin PREPARATION:build/HADOOP-2   (publish + Build)")
    # A head build/<name> cannot publish, and feat/HARNESS, say so instead.
    assert "by hand" in pr_status.next_step("HADOOP-2", "jetty-phase-a", ["publish"], False)
    assert "HARNESS" in pr_status.next_step("HARNESS", "HARNESS", [])
print("pr_status smoke test passed")
