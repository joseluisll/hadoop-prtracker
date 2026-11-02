"""Offline smoke test of merge_preparation.py: merge into made-up branches and push to a local bare fork."""
import contextlib, io, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import merge_preparation as mp
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

    def fork_tip(branch):
        return git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]

    def run(apply):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fine = mp.merge_all(Git(repo), apply)
        return fine, out.getvalue()

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("remote", "add", "origin", fork)
    old_trunk = commit("a.txt", "1\n")
    trunk = commit("b.txt", "2\n")
    git("update-ref", "refs/remotes/upstream/trunk", trunk)

    git("checkout", "-q", "-b", "prep", trunk)
    commit(".github/w.yml", "prep\n")
    prep = commit(".github/x.yml", "prep\n")

    git("checkout", "-q", "-b", "clean", trunk)
    clean = commit("c.txt", "clean\n")
    git("checkout", "-q", "-b", "clash", trunk)
    clash = commit(".github/w.yml", "mine\n")
    git("checkout", "-q", "-b", "done", prep)
    done = commit("d.txt", "done\n")

    git("push", "-q", "origin", f"{prep}:refs/heads/PREPARATION",
        f"{clean}:refs/heads/feat/HADOOP-1", f"{old_trunk}:refs/heads/HADOOP-1",
        f"{clash}:refs/heads/feat/HADOOP-2", f"{old_trunk}:refs/heads/HADOOP-2",
        f"{done}:refs/heads/feat/HADOOP-3", f"{old_trunk}:refs/heads/HADOOP-3",
        f"{clean}:refs/heads/feat/HADOOP-4", f"{trunk}:refs/heads/HADOOP-4",  # generated is current
        f"{clean}:refs/heads/feat/HARNESS")  # nothing generated
    git("fetch", "-q", "origin")

    fine, text = run(False)
    assert not fine, text  # the conflict
    assert "feat/HADOOP-1 (generated base 1 behind): would merge 2 commits, .github/ only" in text, text
    assert "feat/HADOOP-2 (generated base 1 behind): CONFLICT in .github/w.yml" in text, text
    assert "feat/HADOOP-3 (generated base 1 behind): already has PREPARATION" in text, text
    assert "HADOOP-4" not in text and "HARNESS" not in text, text
    assert fork_tip("feat/HADOOP-1") == clean, "a dry run pushed"

    fine, text = run(True)
    assert not fine, text
    assert "feat/HADOOP-1 (generated base 1 behind): merged 2 commits" in text, text
    merged = fork_tip("feat/HADOOP-1")
    assert git("rev-parse", f"{merged}^1", f"{merged}^2").split() == [clean, prep], text
    assert git("log", "-1", "--format=%s", merged) == "Merge PREPARATION into feat/HADOOP-1", text
    assert fork_tip("feat/HADOOP-2") == clash, "a conflict was pushed"
    assert fork_tip("feat/HADOOP-3") == done, text
print("merge smoke ok")
