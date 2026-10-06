"""Offline smoke test of qbt_jira.py: parse a small qbt report, build and score its candidates."""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("PRTRACKER_PROFILE", None)  # the fixtures are hadoop ones
import qbt_jira as q

EMAIL = """

-1 overall


The following subsystems voted -1:
    spotbugs unit xml


The following subsystems voted -1 but
were configured to be filtered/ignored:
    cc javadoc


Specific tests:

    spotbugs :

       module:hadoop-tools/hadoop-rumen
       Unknown bug pattern FS_BAD_DATE_FORMAT_FLAG_COMBO in org.apache.hadoop.tools.rumen.state.StatePool.initialize(Configuration) At StatePool.java:[line 1]

    spotbugs :

       module:root
       Possible null pointer dereference of assoc in org.apache.hadoop.yarn.server.nodemanager.Loc.run()

    Failed junit tests :

       hadoop.yarn.server.nodemanager.TestLogAggregationService


   unit:

      https://ci/job/q/9/artifact/out/patch-unit-hadoop-yarn-project_hadoop-yarn_hadoop-yarn-server_hadoop-yarn-server-nodemanager.txt [132K]

Powered by Apache Yetus 0.14.0-SNAPSHOT   https://yetus.apache.org
"""
CONSOLE = """
|  -1  |             xml  |   0m 46s   | The source tree has 554 ill-formed XML
|      |                  |            | file(s).
|  +1  |            unit  |   0m 16s   | hadoop-build-tools in the source
|      |                  |            | passed.
| git revision | trunk / 997fa7c12a407c441855f1a409767f198db68cd3 |
"""
report = q.parse_email_report(EMAIL)
assert report["voted"] == ["spotbugs", "unit", "xml"], report["voted"]
assert report["filtered"] == ["cc", "javadoc"]
assert report["tests"] == ["hadoop.yarn.server.nodemanager.TestLogAggregationService"]
assert list(report["spotbugs"]) == ["hadoop-tools/hadoop-rumen", "root"]
assert report["unit_modules"] == ["hadoop-yarn-project/hadoop-yarn/hadoop-yarn-server/hadoop-yarn-server-nodemanager"]
console = q.parse_console_report(CONSOLE)
assert console["revision"].startswith("997fa7c1")
assert console["comments"] == {"xml": ["The source tree has 554 ill-formed XML file(s)."]}, console

XML = """<BugCollection><BugInstance type="%s" category="%s" rank="9"><Class classname="%s">
<SourceLine sourcepath="%s"/></Class><Method classname="%s" name="run"/><SourceLine start="7"/>
</BugInstance></BugCollection>"""
NM = "hadoop-yarn-project/hadoop-yarn/hadoop-yarn-server/hadoop-yarn-server-nodemanager"
np_bug = q.parse_spotbugs_xml(XML % ("NP_NULL_ON_SOME_PATH_EXCEPTION", "CORRECTNESS",
                                     "org.apache.hadoop.yarn.server.nodemanager.Loc",
                                     "org/apache/hadoop/yarn/server/nodemanager/Loc.java",
                                     "org.apache.hadoop.yarn.server.nodemanager.Loc"))
date_bug = q.parse_spotbugs_xml(XML % ("FS_BAD_DATE_FORMAT_FLAG_COMBO", "EXPERIMENTAL",
                                       "org.apache.hadoop.tools.rumen.state.StatePool",
                                       "org/apache/hadoop/tools/rumen/state/StatePool.java",
                                       "org.apache.hadoop.tools.rumen.state.StatePool"))
assert np_bug[0]["type"] == "NP_NULL_ON_SOME_PATH_EXCEPTION" and np_bug[0]["line"] == "7"
assert q.warning_text(np_bug[0]) == "NP_NULL_ON_SOME_PATH_EXCEPTION in Loc.run() line 7"


def build(number, tests, commits=(), latest=False):
    record = {"job": "hadoop-qbt-trunk-java17-linux-x86_64", "number": number,
              "url": f"https://ci/job/q/{number}/", "date": f"2026-10-0{number}",
              "commits": [{"sha": c, "msg": f"{c}. change"} for c in commits],
              **report, "tests": tests}
    if latest:
        record.update(console, logs={NM: {"tests": tests, "goals": [["jasmine-maven-plugin", "nm"]],
                                          "url": "https://ci/log"}},
                      cases={tests[0]: [{"name": "testDelete", "age": 1, "error": "boom"}]},
                      warnings={"hadoop-tools/hadoop-rumen": date_bug,
                                "hadoop-tools/hadoop-distcp": date_bug + date_bug,
                                NM: np_bug, "hadoop-yarn-project/hadoop-yarn": np_bug,
                                "root": np_bug + date_bug})
    return record


TEST = "hadoop.yarn.server.nodemanager.TestLogAggregationService"
runs = {"hadoop-qbt-trunk-java17-linux-x86_64": [
    build(9, [TEST], ["YARN-1"], latest=True), build(8, [TEST], ["HDFS-2"]), build(7, [], ["HADOOP-3"])]}
candidates = {(c["kind"], c.get("test") or c["module"]): c
              for c in q.build_candidates(runs, {}, include_lint=True)}
assert set(candidates) == {("test", TEST), ("build", NM), ("spotbugs", NM),
                           ("spotbugs", "hadoop-tools/hadoop-rumen"), ("lint", "")}, set(candidates)
# Owners: the most specific report listing a warning, the smallest on a tie.
rumen = candidates[("spotbugs", "hadoop-tools/hadoop-rumen")]
assert sorted(rumen["reported_in"]) == ["hadoop-tools/hadoop-distcp", "hadoop-tools/hadoop-rumen", "root"]
nm = candidates[("spotbugs", NM)]
assert nm["clears"] == ["hadoop-yarn-project/hadoop-yarn", NM], nm["clears"]  # root has more
assert not nm["aggregate_only"]
# A clone puts the warning where its source is, whatever the reports say.
by_source = q.build_candidates(runs, {"org/apache/hadoop/tools/rumen/state/StatePool.java":
                                      "hadoop-tools/hadoop-distcp"}, include_lint=False)
assert any(c["kind"] == "spotbugs" and c["module"] == "hadoop-tools/hadoop-distcp" for c in by_source)

test = candidates[("test", TEST)]
assert test["module"] == NM and list(test["methods"]) == ["testDelete"]
for c in candidates.values():
    q.history(c, runs)
reg = test["history"]["hadoop-qbt-trunk-java17-linux-x86_64"]["regression"]
assert reg["build"] == 8 and reg["after"] == 7 and [s["sha"] for s in reg["suspects"]] == ["HDFS-2"], reg

yetus = lambda when, rows: {"author": {"login": "hadoop-yetus"}, "createdAt": when,
                            "body": ":broken_heart: **-1 overall**\n" + "\n".join(rows)}
SPOT = "| -1 :x: |  spotbugs  |   1m | [/x.html](https://ci/x.html) |  %s in trunk has %d extant spotbugs warnings.  |"
prs = [
    {"number": 1, "url": "u1", "files": {"nodes": [{"path": f"{NM}/src/main/java/A.java"}]},
     "comments": {"nodes": [yetus("2026-10-01", [
         SPOT % (NM, 1),
         "| -1 :x: |  unit  | 9m | [/p.txt](https://ci/patch-unit-" + NM.replace("/", "_") + ".txt) |  failed  |",
         "| Reason | Tests |", "|-------:|:------|",
         f"| Failed junit tests | {TEST} |"])]}},
    {"number": 2, "url": "u2", "files": {"nodes": [{"path": "pom.xml"}]},
     "comments": {"nodes": [yetus("2026-09-01", [SPOT % ("root", 2)]),
                            yetus("2026-10-01", [SPOT % ("root", 2)])]}},
    {"number": 3, "url": "u3", "files": {"nodes": [{"path": "hadoop-tools/hadoop-rumen/pom.xml"}]},
     "comments": {"nodes": []}},
]
for pr in prs:
    pr["_precommit"] = q.precommit_of(pr)
assert prs[0]["_precommit"]["latest"]["tests"] == {TEST}
assert prs[0]["_precommit"]["latest"]["unit"] == {NM}
assert prs[1]["_precommit"]["latest"]["spotbugs"] == {"root"} and not prs[1]["_precommit"]["earlier"]["spotbugs"]
for c in candidates.values():
    q.pr_impact(c, prs)
assert [(b["number"], b["share"]) for b in nm["blocked"]] == [(1, "clears"), (2, "aggregate")]
assert [(b["number"], b["share"]) for b in rumen["blocked"]] == [(2, "aggregate")]
assert [e["number"] for e in rumen["exposed"]] == [3]
assert [(b["number"], b["share"]) for b in test["blocked"]] == [(1, "clears")]
jasmine = candidates[("build", NM)]
# The unit -1 of #1 also has a failed test: fixing the plugin alone does not clear it.
assert [(b["number"], b["share"]) for b in jasmine["blocked"]] == [(1, "own")]

tiers = {}
for key, c in candidates.items():
    c["project"], c["summary"] = q.summary_of(c, len(runs))
    c["score"], c["tier"], c["reasons"] = q.score(c)
    c["description"] = q.describe(c, runs)
    tiers[key] = (c["tier"], c["score"])
    print(f"{c['tier']} {c['score']:3d}  {c['project']}: {c['summary']}")
    for points, reason in c["reasons"]:
        print(f"      {points:+4d} {reason}")
assert test["summary"] == "TestLogAggregationService fails on trunk" and test["project"] == "YARN"
assert nm["project"] == "YARN" and "NP_NULL_ON_SOME_PATH_EXCEPTION" in nm["summary"]
# test: +10 PR, +6 test, +4 for 2 builds, +4 new = 24 (one JDK, so no JDK points)
assert tiers[("test", TEST)] == ("P2", 24), tiers[("test", TEST)]
# spotbugs nm: +10 clears #1, +5 correctness; one of the root warnings of #2 counts 0
assert tiers[("spotbugs", NM)] == ("P3", 15), tiers[("spotbugs", NM)]
assert any(p == 0 and "whole-repo" in r for p, r in nm["reasons"]), nm["reasons"]
assert tiers[("lint", "")][0] == "P3"
assert "{{hadoop.yarn.server.nodemanager.TestLogAggregationService}}" in test["description"]
assert "HDFS-2. change" in test["description"]  # the suspect commit

# Helped PRs: a whole-repo run (#2 on rumen and nm) helps nobody.
for c in candidates.values():
    c["helps"] = q.helps(c)
assert [b["number"] for b in nm["helps"]] == [1] and rumen["helps"] == []
assert [b["number"] for b in jasmine["helps"]] == [1]  # part of the -1 still counts
ranked = sorted(candidates.values(), key=q.rank_key)
assert [len(c["helps"]) for c in ranked] == sorted((len(c["helps"]) for c in ranked), reverse=True)

# Yetus decides whether a PR fixes it; its title or description is only the lead.
LEAF = NM.rsplit("/", 1)[-1]
DELTA = "| %s :x: |  spotbugs  |  1m |  |  " + NM + " generated %d new + %d unchanged - %d fixed = %d total (was %d)  |"
UNIT = "| %s |  unit  |  9m |  |  %s in the patch %s.  |"


def report(*rows, failed=()):
    body = list(rows)
    if failed:
        body += ["| Reason | Tests |", "|-------:|:------|"] + [f"| Failed junit tests | {t} |" for t in failed]
    return core.yetus_reports({"comments": {"nodes": [yetus("2026-10-02", body)]}})


core = q.core
spot = {"subsystem": "spotbugs", "module": NM, "warning_count": 2,
        "classes": {"org.apache.hadoop.yarn.server.nodemanager.Loc$Pub": ["NP_X"]}}
assert core.yetus_verdict(spot, report(DELTA % ("+1", 0, 0, 2, 0, 2)))[0] == "verified"
assert core.yetus_verdict(spot, report(DELTA % ("+1", 0, 1, 1, 1, 2)))[0] == "partial"
assert core.yetus_verdict(spot, report(DELTA % ("-1", 0, 2, 0, 2, 2)))[0] == "refuted"
verdict, why = core.yetus_verdict(spot, report(DELTA % ("-1", 1, 0, 2, 1, 2)))
assert verdict == "verified" and "adds 1 new" in why, why  # like #8753
assert core.yetus_verdict(spot, report(UNIT % ("+1", LEAF, "passed")))[0] == "unverified"
assert core.yetus_verdict(spot, [])[0] == "unverified"
EXTANT = "| -1 :x: |  spotbugs  | 1m | [/b.html](https://ci/b.html) |  " + NM + " in trunk has 2 extant spotbugs warnings.  |"
assert core.yetus_verdict(spot, report(EXTANT, "| +1 |  spotbugs  | 1m |  |  the patch passed  |"))[0] == "refuted"
# The newest report that checked it decides: a rebase on a clean trunk runs no spotbugs.
older = {"comments": {"nodes": [yetus("2026-09-25", [DELTA % ("+1", 0, 0, 2, 0, 2)]),
                                yetus("2026-10-03", [UNIT % ("+1", LEAF, "passed")])]}}
assert core.yetus_verdict(spot, core.yetus_reports(older))[0] == "verified"
unit_test = {"subsystem": "unit", "test": TEST, "module": NM}
assert core.yetus_verdict(unit_test, report(UNIT % ("+1", LEAF, "passed")))[0] == "verified"
assert core.yetus_verdict(unit_test, report(UNIT % ("-1", LEAF, "failed"), failed=[TEST]))[0] == "refuted"
assert core.yetus_verdict(unit_test, report(DELTA % ("+1", 0, 0, 2, 0, 2)))[0] == "unverified"
plugin = {"subsystem": "build", "project": "catalog-webapp", "plugin": "jasmine-maven-plugin"}
assert core.yetus_verdict(plugin, report(UNIT % ("+1", "catalog-webapp", "passed")))[0] == "verified"
assert core.yetus_verdict(plugin, report(UNIT % ("-1", "catalog-webapp", "failed")))[0] == "refuted"

# Leads: a PR naming the inner class (#1) or the bug type (#3, #4) and changing
# the class. #2 only has the type in a comment, so it is no lead. Then Yetus:
# #1 verified, #3 refuted (kept apart), #4 merged without a report: unverified.
LOC = f"{NM}/src/main/java/org/apache/hadoop/yarn/server/nodemanager/Loc.java"


def node(number, title, comments=(), state="OPEN", body=""):
    return {"number": number, "title": title, "body": body, "state": state, "url": "",
            "mergedAt": "2026-10-03T00:00:00Z" if state == "MERGED" else None,
            "author": {"login": "a"}, "files": {"nodes": [{"path": LOC}]},
            "comments": {"nodes": list(comments)}}


nodes = [node(1, "YARN-1. Fix the nullness warning in Pub.run()",
              [yetus("2026-10-02", [DELTA % ("+1", 0, 0, 2, 0, 2)])]),
         node(2, "YARN-2. Other", [yetus("2026-10-02", ["NP_X in Loc"])]),
         node(3, "YARN-3. Fix NP_X in Loc", [yetus("2026-10-02", [DELTA % ("-1", 0, 2, 0, 2, 2)])]),
         node(4, "YARN-4. Something", state="MERGED", body="SpotBugs reports NP_X.")]
searched = []


def fake_prs(repo, words, token):
    searched.append(words)
    return nodes


def fake_jiras(base, words, since=""):
    searched.append(["jira"] + words)
    return []


core.search_fixer_prs, core.search_jira_issues = fake_prs, fake_jiras
found = core.existing_fixes({**spot, "first_seen": "2026-10-01"}, "r", None, "j", set(), set())
assert [(p["number"], p["verified"]) for p in found["prs"]] == [(1, "verified"), (4, "unverified")], found["prs"]
assert [p["number"] for p in found["refuted"]] == [3]
assert ["Pub", "spotbugs"] in searched and ["NP_X"] in searched
assert ["jira", "NP_X"] not in searched and ["jira", "Pub", "spotbugs"] not in searched, searched

# Status: what the precommit showed decides the wording.
args = q.parse_args([])
merged = {"number": 7, "state": "MERGED", "merged": "2026-10-03", "jira": None,
          "verified": "verified", "evidence": "its precommit of 2026-10-02 on x: '0 total'"}
nm["tracking"] = {"prs": [merged], "jiras": [], "related": []}
assert q.status_of(nm, args).startswith("already fixed on trunk by #7 (merged 2026-10-03)")
nm["tracking"]["prs"] = [{**merged, "verified": "unverified"}]
assert q.status_of(nm, args).startswith("probably fixed (not verified) on trunk by #7")
nm["tracking"] = {"prs": [{**merged, "state": "OPEN", "verified": "unverified"}], "jiras": [],
                  "related": [{**found["refuted"][0]}]}
assert q.status_of(nm, args).startswith("maybe being fixed (not verified) by #7 (open, unverified)")
assert [c.split(":")[0] for c in q.checks_of(nm)] == ["#7 unverified", "#3 refuted"], q.checks_of(nm)
rumen["tracking"] = {"prs": [], "jiras": [], "related": []}
assert q.status_of(rumen, args, 2) == "nobody works on it: proposal [2] below"
# A tracked candidate is listed when it helps a PR; one that helps none is not.
shown, hidden = q.ranking([rumen], [nm, {**test, "helps": []}], args)
assert [e["summary"] for e, _ in shown] == [nm["summary"], rumen["summary"]] and hidden == 1

# The hbase profile: its keys, its base branch, and the HTML report of its staged nightly.
q.core.use_profile("hbase")
try:
    assert q.core.JIRA_IN_TEXT_RE.fullmatch("HBASE-30447") and not q.core.JIRA_IN_TEXT_RE.search("YARN-1")
    assert q.core.new_jira_summary({"test": "hadoop.hbase.TestX"}) == "HBASE: TestX fails on master"
    HTML = """<table><tr><th>-1 overall</th></tr></table><table>
<tr><th>Vote</th><th>Subsystem</th><th>Runtime</th><th>Log</th><th>Comment</th></tr>
<tr><td><font color="red">-1</font></td><td> spotbugs </td><td>1m</td><td><a href="x">/x.txt</a></td>
<td> hbase-server in master has 2 extant spotbugs warnings. </td></tr>
<tr><td>+1</td><td> unit </td><td>9m</td><td></td><td> root in the source passed. </td></tr>
<tr><td> git revision </td><td> master / 20c3d0110af40fba5b34166481e396d6b5a87a7c </td></tr></table>"""
    console = q.parse_console_report(q.html_report_text(HTML))
    assert console["revision"].startswith("20c3d011"), console
    assert console["comments"] == {"spotbugs": ["hbase-server in master has 2 extant spotbugs warnings."]}
    assert q.core.YETUS_EXTANT_RE.search(console["comments"]["spotbugs"][0]).group(1) == "hbase-server"
    assert q.jdk_of("jdk21-hadoop3") == "JDK 21"
    # console.txt of a GitHub Actions Yetus artifact: wrapped comments, section rows, -0.
    ACTIONS = """|      |                 |            | Patch Compile Tests
+---------------------------------------------------------------------------
|  -0  |     checkstyle  |   0m 48s   | hbase-server: The patch generated 3 new
|      |                 |            | + 0 unchanged - 0 fixed = 3 total (was
|      |                 |            | 0)
|  -1  |       spotbugs  |   1m 36s   | hbase-server generated 6 new + 0
|      |                 |            | unchanged - 0 fixed = 6 total (was 0)
|      |                 |  36m 22s   |"""
    assert q.core.console_rows(ACTIONS) == [
        ("-0", "checkstyle", "hbase-server: The patch generated 3 new + 0 unchanged - 0 fixed = 3 total (was 0)"),
        ("-1", "spotbugs", "hbase-server generated 6 new + 0 unchanged - 0 fixed = 6 total (was 0)")]
    # The whole-tree unit log of HBase Nightly: a flaky test the rerun passed, and a fork timeout.
    UNIT = """[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 780.1 s <<< FAILURE! -- in org.apache.hadoop.hbase.master.TestSCP
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.5.3:test (secondPartTestsExecution) on project hbase-server: There was a timeout in the fork -> [Help 1]"""
    log = q.core.log_failures(UNIT)
    assert log["tests"] == ["hadoop.hbase.master.TestSCP"] and log["goals"] == [], log
    assert log["timeouts"] == [["maven-surefire-plugin", "hbase-server"]], log
    stage = {"job": "jdk21-hadoop3", "number": 2, "url": "u", "date": "2026-10-03", "commits": [],
             "voted": ["unit"], "comments": {}, "cases": {}, "warnings": {}, "tests": log["tests"],
             "logs": {"root": log | {"url": "https://ci/patch-unit-root.txt"}}, "unit_modules": ["root"]}
    stage["flaky"] = q.flaky_tests(stage, [])
    assert stage["flaky"] == ["hadoop.hbase.master.TestSCP"]
    clean = {**stage, "number": 1, "voted": [], "tests": [], "logs": {}, "unit_modules": []}
    staged = {(c["kind"], c["module"]): c for c in q.build_candidates({"jdk21-hadoop3": [stage, clean]}, {}, False)}
    fork = staged[("build", "hbase-server")]
    assert fork["timeout"] and q.what_line(fork) == "maven-surefire-plugin times out on hbase-server"
    assert q.core.new_jira_summary(fork["record"]) == "HBASE: maven-surefire-plugin times out in a fork on hbase-server"
    assert q.core.failure_words(fork["record"]) == [["surefire", "timeout"], ["surefire", "timed out"]]
    q.history(fork, {"jdk21-hadoop3": [stage, clean]})
    assert fork["history"]["jdk21-hadoop3"]["failed"] == 1, fork["history"]
    scp = staged[("test", "root")]
    q.history(scp, {"jdk21-hadoop3": [stage, clean]})
    assert scp["flaky"] and q.what_line(scp).endswith("(flaky: passed on rerun)")
    assert q.summary_of(scp, 1) == ("HBASE", "TestSCP is flaky on master"), q.summary_of(scp, 1)
    assert q.vote_of(clean) == "+1" and q.vote_of(stage) == "-1 unit"
finally:
    q.core.use_profile("hadoop")
assert q.core.new_jira_summary({"test": "hadoop.yarn.TestX"}) == "YARN: TestX fails on trunk"
print("qbt_smoke: OK")
