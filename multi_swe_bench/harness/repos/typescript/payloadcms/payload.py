import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared parse_log for all epochs (handles both Jest and Vitest output)
# ---------------------------------------------------------------------------


def payload_parse_log(test_log: str) -> TestResult:
    """Parse Jest/Vitest test output for Payload CMS.

    Handles:
    - Jest verbose: checkmark/cross individual tests, suite PASS/FAIL
    - Vitest verbose: checkmark/cross individual tests
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    passed_res = [
        re.compile(r"^\[PASS\]:?\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"),
        re.compile(r"^\s*[✔✓√]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"),
    ]

    failed_res = [
        re.compile(r"^\[FAIL\]:?\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"),
        re.compile(r"^\s*[×✕✗✘✖]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"),
    ]

    skipped_res = [
        re.compile(r"^\[SKIP\]:?\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"),
        re.compile(
            r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
        ),
        re.compile(r"^\s*[-↓]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"),
    ]

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        matched = False

        for passed_re in passed_res:
            m = passed_re.match(line)
            if m:
                name = m.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                matched = True
                break
        if matched:
            continue

        for failed_re in failed_res:
            m = failed_re.match(line)
            if m:
                if m.lastindex == 2:
                    name = "{suite} > {test}".format(
                        suite=m.group(1).strip(), test=m.group(2).strip()
                    )
                else:
                    name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                matched = True
                break
        if matched:
            continue

        for skipped_re in skipped_res:
            m = skipped_re.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                break

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# Shared shell script templates
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

"""

_STRIP_BINARY_DIFFS_PY = """#!/usr/bin/env python3
\"\"\"Strip binary diffs from a patch file so git apply doesn't choke on them.\"\"\"
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()

    diffs = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
    text_diffs = []
    for diff in diffs:
        if not diff.strip():
            continue
        if 'Binary files' in diff or 'GIT binary patch' in diff:
            continue
        text_diffs.append(diff)

    with open(patch_path, 'w') as f:
        f.write('\\n'.join(text_diffs))

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
"""

_START_MONGO_SH = """#!/bin/bash
# Start MongoDB as a single-node replica set for integration tests.
set -e

MONGO_PORT=27018
MONGO_DB_PATH=/data/db
MONGO_LOG=/var/log/mongod.log

mkdir -p "$MONGO_DB_PATH"

mongod --replSet rs0 --port $MONGO_PORT --dbpath "$MONGO_DB_PATH" --fork --logpath "$MONGO_LOG" --bind_ip_all --noauth

for i in $(seq 1 30); do
    if mongosh --port $MONGO_PORT --eval "db.runCommand({ping: 1})" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

mongosh --port $MONGO_PORT --eval "
try {
    rs.initiate({_id: 'rs0', members: [{_id: 0, host: 'localhost:$MONGO_PORT'}]});
} catch(e) {
    print('Replica set already initiated or error: ' + e);
}
"

for i in $(seq 1 30); do
    if mongosh --port $MONGO_PORT --eval "rs.status().ok" 2>/dev/null | grep -q "1"; then
        break
    fi
    sleep 1
done

mongosh --port $MONGO_PORT --eval "
try {
    db.getSiblingDB('admin').createUser({
        user: 'payload',
        pwd: 'payload',
        roles: [{role: 'root', db: 'admin'}]
    });
} catch(e) {
    print('User creation: ' + e);
}
"

mongosh --port $MONGO_PORT admin --eval "db.shutdownServer({force: true})" 2>/dev/null || true
sleep 2

mongod --replSet rs0 --port $MONGO_PORT --dbpath "$MONGO_DB_PATH" --fork --logpath "$MONGO_LOG" --bind_ip_all --auth --keyFile /dev/null 2>/dev/null || \\
mongod --replSet rs0 --port $MONGO_PORT --dbpath "$MONGO_DB_PATH" --fork --logpath "$MONGO_LOG" --bind_ip_all

for i in $(seq 1 15); do
    if mongosh --port $MONGO_PORT -u payload -p payload --authenticationDatabase admin --eval "db.runCommand({ping: 1})" > /dev/null 2>&1; then
        echo "MongoDB ready with auth on port $MONGO_PORT"
        exit 0
    fi
    if mongosh --port $MONGO_PORT --eval "db.runCommand({ping: 1})" > /dev/null 2>&1; then
        echo "MongoDB ready (no auth) on port $MONGO_PORT"
        exit 0
    fi
    sleep 1
done

echo "MongoDB started on port $MONGO_PORT (auth may not be enforced)"
exit 0
"""
