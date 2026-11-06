"""Offline smoke test of stack_pr.py: replaying a stack's commits onto current trunk."""
import contextlib, io, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stack_pr
from list_stale_branches import Git


def run(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, check=check,
                          capture_output=True, text=True).stdout.strip()


def make(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
    with open(full, "w") as f:
        f.write(text)
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", path)
    return run(repo, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "repo")
    fork = os.path.join(tmp, "fork.git")
    os.mkdir(repo)
    run(tmp, "init", "-q", "--bare", fork)
    run(repo, "init", "-q")
    run(repo, "config", "user.name", "t")
    run(repo, "config", "user.email", "t@t")
    run(repo, "remote", "add", "origin", fork)
    run(repo, "remote", "add", "upstream", fork)

    trunk = make(repo, "a.txt", "trunk\n")
    # A stack of three commits in PR order, each applying on top of the previous one:
    # the second edits the file the first added, which is what a net-change squash loses.
    run(repo, "checkout", "-q", "-b", "stack", trunk)
    c1 = make(repo, "one.txt", "one\n")
    c2 = make(repo, "one.txt", "one\ntwo\n")
    c3 = make(repo, "own.txt", "the PR's own work\n")
    run(repo, "commit", "-q", "--allow-empty", "-m", "empty trigger")

    run(repo, "push", "-q", "origin", f"{trunk}:refs/heads/trunk", "HEAD:refs/heads/stack")
    run(repo, "fetch", "-q", "origin")
    run(repo, "update-ref", "refs/remotes/upstream/trunk", trunk)
    original = run(repo, "rev-parse", "origin/stack")

    ranges = {"stack": [f"{trunk}..origin/stack"]}

    git = Git(repo)
    # Dry run: prints each commit and touches no branch.
    out, _ = stack_pr.build(git, "stack", ranges["stack"], False)
    assert "would rebuild 4 commits" in out, out
    assert "empty trigger" in out, "the empty commit was not replayed"
    assert run(repo, "rev-parse", "origin/stack") == original, "the dry run pushed"

    # Trunk advances: the stack is rebuilt on top of it, in order.
    run(repo, "checkout", "-q", "-b", "trunk2", trunk)
    trunk2 = make(repo, "b.txt", "trunk moved\n")
    run(repo, "push", "-q", "origin", f"{trunk2}:refs/heads/trunk")
    run(repo, "fetch", "-q", "origin")
    run(repo, "update-ref", "refs/remotes/upstream/trunk", trunk2)

    out, ok = stack_pr.build(git, "stack", ranges["stack"], True)
    assert ok, out
    run(repo, "fetch", "-q", "origin")
    tip = run(repo, "rev-parse", "origin/stack")
    assert tip != original, "nothing was pushed"
    assert run(repo, "rev-list", "--count", f"{trunk2}..{tip}") == "4"
    assert "two" in run(repo, "show", f"{tip}:one.txt"), "the second commit's change was lost"
    assert "the PR's own work" in run(repo, "show", f"{tip}:own.txt")

    # A conflicting commit stops the stack and leaves the branch alone. This commit adds
    # b.txt on trunk; the stack so far already has trunk2's b.txt, so replaying it clashes.
    run(repo, "checkout", "-q", "-b", "clash", trunk)
    make(repo, "b.txt", "clash's own version\n")
    clash = run(repo, "rev-parse", "HEAD")
    run(repo, "update-ref", "refs/heads/clash2", clash)  # local only: no push
    before = run(repo, "rev-parse", "origin/stack")
    out, ok = stack_pr.build(git, "stack", [f"{trunk2}..clash2"], True)
    assert not ok and "CONFLICT" in out, out
    assert run(repo, "rev-parse", "origin/stack") == before, "a conflict was pushed"

    # A stale local branch of the same name must not become the lease's yardstick.
    run(repo, "checkout", "-q", "-b", "stack2", trunk2)
    make(repo, "local.txt", "local only\n")
    run(repo, "update-ref", "refs/remotes/origin/stack2", trunk2)
    local_sha = run(repo, "rev-parse", "stack2")
    tracked = "origin/stack2"
    ref = tracked if stack_pr.core.has_commit(repo, tracked) else "stack2"
    assert run(repo, "rev-parse", ref) == run(repo, "rev-parse", "origin/stack2"), \
        "a local branch won over the fork's"
print("stack smoke ok")
