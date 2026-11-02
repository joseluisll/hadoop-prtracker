"""Offline smoke test of create_jira: the duplicate search and the proposal helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import create_jira as cj

assert cj.split_suggestion("HADOOP: TestX fails") == ("HADOOP", "TestX fails")
assert cj.split_suggestion("TestX fails") == (cj.core.DEFAULT_PROJECT, "TestX fails")

# A search that returns no issues: an empty list, not None.
cj.core.jira_get = lambda base, path, params: {"issues": []}
assert cj.already_filed("https://j", {"project": "HADOOP", "summary": "TestX fails",
                                      "record": {}}) == []

# Issues found.
cj.core.jira_get = lambda base, path, params: {
    "issues": [{"key": "HADOOP-1", "fields": {"summary": "TestX fails"}}]}
assert cj.already_filed("https://j", {"project": "HADOOP", "summary": "TestX fails",
                                      "record": {}}) == [
    {"key": "HADOOP-1", "summary": "TestX fails"}]

# A failed search: None, so --force treats it as "may be filed".
cj.core.jira_get = lambda base, path, params: None
assert cj.already_filed("https://j", {"project": "HADOOP", "summary": "TestX fails",
                                      "record": {}}) is None

record = {"test": "org.apache.TestX", "first_seen": "2026-11-01", "last_seen": "2026-11-02",
          "seen_in": ["precommit"]}
seen_by = [{"pr": {"number": 1, "url": "https://github.com/o/r/pull/1"}, "jira": "HADOOP-1",
            "record": record}]
description = cj.describe(record, seen_by)
assert "TestX" in description and "PR #1" in description and "HADOOP-1" in description

# --force with a failed duplicate search: skipped, nothing created.
cj.core.jira_get = lambda base, path, params: None
cj.fd.jira_token = lambda: "token"
cj.fd.confirm = lambda question: True
created = []
cj.create_issue = lambda base, proposal, args: (created.append(proposal),
                                                (True, "TEST-1 created"))[1]
rc = cj.main(["--apply", "--force", "--token", "fake", "--project", "HADOOP",
             "--summary", "Test failure"])
assert rc == 0 and not created, (rc, created)

# --force with an empty duplicate search: created.
cj.core.jira_get = lambda base, path, params: {"issues": []}
created.clear()
rc = cj.main(["--apply", "--force", "--token", "fake", "--project", "HADOOP",
             "--summary", "Test failure"])
assert rc == 0 and len(created) == 1, (rc, created)

# --force with a found duplicate: skipped.
cj.core.jira_get = lambda base, path, params: {
    "issues": [{"key": "HADOOP-1", "fields": {"summary": "Test failure"}}]}
created.clear()
rc = cj.main(["--apply", "--force", "--token", "fake", "--project", "HADOOP",
             "--summary", "Test failure"])
assert rc == 0 and not created, (rc, created)

print("create_smoke: OK")
