"""Offline smoke test of qbt_jira.py: parse a small qbt report, build and score its candidates."""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
# spotbugs nm: +10 clears #1, +1 aggregate #2, +5 correctness
assert tiers[("spotbugs", NM)] == ("P3", 16), tiers[("spotbugs", NM)]
assert tiers[("lint", "")][0] == "P3"
assert "{{hadoop.yarn.server.nodemanager.TestLogAggregationService}}" in test["description"]
assert "HDFS-2. change" in test["description"]  # the suspect commit
print("qbt_smoke: OK")
