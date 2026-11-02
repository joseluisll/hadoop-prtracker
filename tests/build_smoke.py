"""Offline smoke test of build_generated.py: request builds of made-up branches on a local bare fork."""
import contextlib, io, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import build_generated as bg
from list_feat_branches import STAGED_REFS
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

    def run(apply):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fine = bg.build_all(Git(repo), apply)
        return fine, out.getvalue()

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("remote", "add", "origin", fork)
    old_trunk = commit("a.txt", "1\n")
    trunk = commit("b.txt", "2\n")
    git("update-ref", "refs/remotes/upstream/trunk", trunk)
    git("checkout", "-q", "-b", "prep", trunk)
    prep = commit(".github/w.yml", "prep\n")

    feat = on("feat", prep, "new\n")
    old = on("old", old_trunk, "new\n")  # one commit on trunk, as the workflow publishes
    tag = on("tag", trunk, "new\n")
    other_tag = on("other-tag", trunk, "other\n")
    git("checkout", "-q", "old")
    extra = commit("d.txt", "not in feat\n")  # two commits, not in feat/

    push = [f"{prep}:refs/heads/PREPARATION", f"{feat}:refs/heads/feat/HADOOP-5", f"{tag}:refs/heads/HADOOP-5"]
    for name, generated in (("HADOOP-1", old), ("HADOOP-2", extra), ("HADOOP-3", old), ("HADOOP-4", old),
                            ("HADOOP-6", old)):
        push += [f"{feat}:refs/heads/feat/{name}", f"{generated}:refs/heads/{name}"]
    push.append(f"{prep}:refs/heads/build/HADOOP-6")  # already requested
    git("push", "-q", "origin", *push)
    git("fetch", "-q", "origin")
    for name, staged in (("HADOOP-1", tag), ("HADOOP-2", tag), ("HADOOP-3", other_tag), ("HADOOP-5", tag)):
        git("update-ref", f"{STAGED_REFS}/{name}", staged)

    def builds():
        return git("ls-remote", "origin", "refs/heads/build/*")

    fine, text = run(False)
    assert not fine, text
    assert "HADOOP-1 (base 1 behind): would request a build, staged base 0 behind" in text, text
    assert "HADOOP-2 (base 1 behind): SKIPPED, HADOOP-2 holds work" in text, text
    assert "HADOOP-3 (base 1 behind): SKIPPED, staged/HADOOP-3 has other code" in text, text
    assert "HADOOP-4 (base 1 behind): SKIPPED, no staged/HADOOP-4" in text, text
    assert "HADOOP-6 (base 1 behind): build/ request already pending" in text, text
    assert "HADOOP-5" not in text, text  # generated is current
    assert "refs/heads/build/HADOOP-1" not in builds(), "a dry run pushed"

    fine, text = run(True)
    assert "HADOOP-1 (base 1 behind): build requested" in text, text
    assert prep in builds() and "refs/heads/build/HADOOP-1" in builds(), text
    assert "refs/heads/build/HADOOP-2" not in builds(), text

    # A tag on the same trunk as the generated branch would publish nothing newer.
    git("update-ref", f"{STAGED_REFS}/HADOOP-4", old)
    bg.core.has_commit.cache_clear()
    fine, text = run(False)
    assert "HADOOP-4 (base 1 behind): SKIPPED, staged/HADOOP-4 is no newer (base 1 behind)" in text, text
print("build smoke ok")
