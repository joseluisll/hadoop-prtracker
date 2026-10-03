"""Offline smoke test of review_queue.py: classify, score and rank a few made-up open PRs."""
import contextlib, io, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import review_queue as rq

YETUS_PASS = ":confetti_ball: **+1 overall**\n| +1 :green_heart: | unit | 1m | | passed |"
YETUS_FAIL = ":broken_heart: **-1 overall**\n| -1 :x: | unit | 1m | | failed |"


def pr(number, title, files, author="someone", created="2026-01-01T00:00:00Z", yetus=YETUS_PASS,
       yetus_at="2026-09-02T00:00:00Z", commit_at="2026-09-01T00:00:00Z", reviews=(),
       decision=None, draft=False, mergeable="MERGEABLE", requested=(), commenters=()):
    nodes = [{"path": p, "additions": a, "deletions": d} for p, a, d in files]
    return {
        "number": number, "title": title, "url": f"https://github.com/apache/hadoop/pull/{number}",
        "isDraft": draft, "createdAt": created, "updatedAt": commit_at, "baseRefName": "trunk",
        "additions": sum(a for _, a, _ in files), "deletions": sum(d for _, _, d in files),
        "changedFiles": len(files), "mergeable": mergeable, "reviewDecision": decision,
        "author": {"login": author}, "labels": {"nodes": []}, "files": {"nodes": nodes},
        "latestReviews": {"nodes": [{"author": {"login": who}, "authorAssociation": "CONTRIBUTOR",
                                     "state": state, "submittedAt": at}
                                    for who, state, at in reviews]},
        "participants": {"nodes": [{"author": {"login": who}, "authorAssociation": assoc}
                                   for who, assoc in commenters]},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": who}}
                                     for who in requested]},
        "comments": {"nodes": [{"author": {"login": "hadoop-yetus"}, "createdAt": yetus_at,
                                "body": yetus}] if yetus else []},
        "commits": {"nodes": [{"commit": {"committedDate": commit_at}}]},
    }


HDFS = "hadoop-hdfs-project/hadoop-hdfs"
RBF = "hadoop-hdfs-project/hadoop-hdfs-rbf"
YARN_NM = "hadoop-yarn-project/hadoop-yarn/hadoop-yarn-server/hadoop-yarn-server-nodemanager"
MODULES = {"", "hadoop-project", "hadoop-hdfs-project", HDFS, RBF, "hadoop-yarn-project", YARN_NM}

PRS = [
    pr(1, "HDFS-1. Fix NPE in BlockManager", [(f"{HDFS}/src/main/java/a/B.java", 10, 2),
                                             (f"{HDFS}/src/test/java/a/TestB.java", 8, 0)]),
    pr(2, "HDFS-2. Fix flaky TestRouterRpc", [(f"{RBF}/src/test/java/a/TestRouterRpc.java", 5, 5)],
       author="me"),
    pr(3, "YARN-3. Add a huge feature", [(f"{YARN_NM}/src/main/java/x/F{i}.java", 100, 0)
                                         for i in range(25)], yetus=YETUS_FAIL),
    pr(4, "HDFS-4. Fix race in DataNode", [(f"{HDFS}/src/main/java/a/D.java", 3, 1)],
       reviews=[("me", "COMMENTED", "2026-09-05T00:00:00Z")]),
    pr(5, "HDFS-5. Fix leak in RBF", [(f"{RBF}/src/main/java/a/R.java", 3, 1)],
       decision="CHANGES_REQUESTED", reviews=[("reviewer", "CHANGES_REQUESTED", "2026-09-10T00:00:00Z")]),
    pr(6, "HDFS-6. Draft fix", [(f"{HDFS}/src/main/java/a/E.java", 1, 1)], draft=True),
    pr(7, "Bump jackson from 2.18.10 to 2.18.11", [("hadoop-project/pom.xml", 1, 1)],
       author="dependabot", yetus=None, mergeable="CONFLICTING"),
    pr(8, "HADOOP-8. Fix CVE-2026-1234 in token handling", [(f"{HDFS}/src/main/java/a/T.java", 30, 4)],
       requested=["me"]),
]

PRS.append(pr(9, "HDFS-9. Fix wrong quota", [(f"{HDFS}/src/main/java/a/Q.java", 3, 1)],
              author="member", commenters=[("member", "MEMBER"), ("someone", "NONE")]))
PRS.append(pr(10, "HDFS-10. Fix wrong quota", [(f"{HDFS}/src/main/java/a/Q.java", 3, 1)],
              commenters=[("member", "MEMBER")]))
PRS.append(pr(11, "HDFS-11. Fix wrong quota", [(f"{HDFS}/src/main/java/a/Q.java", 3, 1)],
              reviews=[("merger", "COMMENTED", "2026-08-01T00:00:00Z")]))
PRS.append(pr(12, "HDFS-12. Fix wrong quota", [(f"{HDFS}/src/main/java/a/Q.java", 3, 1)],
              requested=["merger"]))
PRS.append(pr(13, "HDFS-13. Fix wrong quota", [(f"{HDFS}/src/main/java/a/Q.java", 3, 1)]))

# Write access: GitHub's association, or a merge (most committers show as CONTRIBUTOR).
assert rq.associated_committers(PRS) == {"member"}
committers = {"member", "merger"}
assert rq.committers_engaged(PRS[8], "me", committers) == []  # the author does not count
assert rq.committers_engaged(PRS[9], "me", committers) == ["member"]
assert rq.committers_engaged(PRS[10], "me", committers) == ["merger"]
assert rq.committers_engaged(PRS[11], "me", committers) == ["merger"]  # requested reviewer
assert rq.committers_engaged(PRS[9], "member", committers) == []  # nor do you
scored = {}
for p in PRS[9:13]:
    e = rq.classify(p, "me", MODULES)
    rq.score(e, p, "me", set(), set(), committers)
    scored[e.number] = e
assert any("committer involved: member" in r for r in scored[10].reasons), scored[10].reasons
assert scored[10].committers == ["member"]
assert scored[12].score - scored[13].score == rq.POINTS["committer_engaged"], \
    (scored[12].reasons, scored[13].reasons)

entries = {e.number: e for e in (rq.classify(p, "me", MODULES) for p in PRS)}
assert entries[1].component == "hadoop-hdfs" and entries[1].project == "HDFS", entries[1]
assert entries[1].topic == "bug" and entries[1].yetus == "+1"
assert entries[2].mine and entries[2].topic == "test" and entries[2].component == "hadoop-hdfs-rbf"
assert entries[3].component == "hadoop-yarn-server-nodemanager" and entries[3].yetus == "-1"
assert entries[4].reviews == "commented"
assert entries[5].reviews == "changes"
assert entries[7].topic == "dependency" and entries[7].component == "hadoop-project"
assert entries[7].yetus == "none" and entries[7].conflict
assert entries[8].topic == "security"
# Without a clone the module is the path before src/.
assert rq.module_of(f"{RBF}/src/main/java/a/R.java", set()) == RBF
assert rq.module_of("pom.xml", MODULES) == ""
assert rq.component_name("") == rq.ROOT

by_number = {p["number"]: p for p in PRS}
for e in entries.values():
    rq.score(e, by_number[e.number], "me", set(), {"hadoop-hdfs"})
assert any("you reviewed it" in r for r in entries[4].reasons), entries[4].reasons
assert any("waiting for the author" in r for r in entries[5].reasons), entries[5].reasons
assert any("your review was requested" in r for r in entries[8].reasons)
assert any("merge conflict" in r for r in entries[7].reasons)
assert entries[1].score > entries[3].score > 0, (entries[1].score, entries[3].score)
assert entries[1].score > entries[4].score  # already reviewed by me, nothing new
assert entries[8].score > entries[1].score  # security, review requested from me

picked = rq.recommend(list(entries.values()), 10, 3, include_drafts=False)
numbers = [e.number for e in picked]
assert 2 not in numbers and 6 not in numbers, numbers  # mine, draft
assert numbers[0] == 8, numbers
assert len(rq.recommend(list(entries.values()), 10, 1, include_drafts=False)) == 4  # one per component

# Your area: 2+ changes of main code per component, from commits and your open PRs.
assert rq.main_components([f"{HDFS}/src/test/java/a/T.java", f"{RBF}/src/main/java/R.java"],
                          MODULES) == {"hadoop-hdfs-rbf"}
# Build files do not make a component yours: pom.xml files and root files.
assert rq.main_components([f"{HDFS}/pom.xml", "hadoop-project/pom.xml", "LICENSE-binary",
                           ".github/workflows/x.yml"], MODULES) == set()
import collections
history = collections.Counter({"hadoop-hdfs": 1, "hadoop-yarn-server-nodemanager": 3})
own = [p for p in PRS if p["author"]["login"] == "me"]  # #2 changes only RBF tests
assert rq.your_area(history, own, MODULES) == {"hadoop-yarn-server-nodemanager"}
own_main = pr(99, "HDFS-99. x", [(f"{HDFS}/src/main/java/a/Z.java", 1, 1)], author="me")
assert rq.your_area(history, own + [own_main], MODULES) == {"hadoop-hdfs",
                                                            "hadoop-yarn-server-nodemanager"}
area = rq.classify(PRS[0], "me", MODULES)
rq.score(area, PRS[0], "me", set(), {"hadoop-hdfs"})
assert any(r == "+10 your area: hadoop-hdfs" for r in area.reasons), area.reasons
other = rq.classify(PRS[0], "me", MODULES)
rq.score(other, PRS[0], "me", set(), {"hadoop-hdfs-rbf"})
assert not any("area" in r or "project" in r for r in other.reasons), other.reasons

# Your history, read from a small git clone: test-only commits add nothing.
with tempfile.TemporaryDirectory() as clone:
    def git(*args):
        assert rq.core.git_run(clone, *args)[0] == 0, args
    git("init", "-q", "-b", "trunk")
    git("config", "user.email", "me@example.org")
    git("config", "user.name", "Me")
    for module in (HDFS, RBF, YARN_NM):
        os.makedirs(os.path.join(clone, module), exist_ok=True)
        open(os.path.join(clone, module, "pom.xml"), "w").close()
    git("add", "-A")
    git("-c", "user.email=other@example.org", "commit", "-q", "-m", "modules")
    for i, path in enumerate([f"{HDFS}/src/main/java/A.java", f"{HDFS}/src/main/java/B.java",
                              f"{RBF}/src/test/java/T.java", f"{YARN_NM}/src/main/java/N.java"]):
        os.makedirs(os.path.join(clone, os.path.dirname(path)), exist_ok=True)
        with open(os.path.join(clone, path), "w") as f:
            f.write(str(i))
        git("add", "-A")
        git("commit", "-q", "-m", f"change {i}")
    clone_modules = rq.maven_modules(clone)
    assert {HDFS, RBF, YARN_NM} <= clone_modules, clone_modules
    history = rq.your_history(clone, clone_modules, 365)
    assert history == {"hadoop-hdfs": 2, "hadoop-yarn-server-nodemanager": 1}, history
    assert rq.your_area(history, [], clone_modules) == {"hadoop-hdfs"}
    assert not rq.your_history(clone, clone_modules, 0)

# --focus replaces the learned area.
focused = rq.classify(PRS[2], "me", MODULES)
rq.score(focused, PRS[2], "me", {"YARN"}, set())
assert any("in your focus: YARN" in r for r in focused.reasons), focused.reasons

# End to end, offline, every format.
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "prs.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(PRS, f)
    base = ["--from-json", path, "--user", "me", "--repo-path", os.path.join(tmp, "none"),
            "--committer", "merger"]
    for extra in ([], ["--explain", "--width", "100"], ["--format", "markdown", "--explain"],
                  ["--view", "components", "--others"], ["--component", "HDFS"]):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert rq.main(base + extra) == 0
        text = out.getvalue()
        assert "HDFS-1. Fix NPE" in text, extra
        if extra == ["--component", "HDFS"]:
            assert "Bump jackson" not in text and "CVE" not in text, text
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rq.main(base + ["--format", "json", "--max-per-component", "20"])
    data = json.loads(out.getvalue())
    assert set(data) == {"components", "recommendations"}
    ranked = [r["number"] for r in data["recommendations"]]
    assert data["recommendations"][0]["committers"], ranked  # a committer is involved
    assert 8 in ranked and 2 not in ranked and 9 in ranked, ranked
    assert sum(len(v) for v in data["components"].values()) == len(PRS)
    assert any(r["committers"] == ["member"] for r in data["recommendations"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rq.main(base + ["--view", "components", "--mine", "--format", "json"])
    assert [e["number"] for v in json.loads(out.getvalue())["components"].values() for e in v] == [2]

print("review_smoke: OK")
