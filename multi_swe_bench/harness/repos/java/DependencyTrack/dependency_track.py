import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Toolchain (identical at both base shas, main f3857060 and 5.1.x 103443c7):
# the root pom.xml compiles with <release>25</release>, and maven-enforcer
# requires Java [25,) and Maven [3.9,). CI (.github/workflows/ci-test.yaml)
# runs `make test` on Temurin 25. The repo has no Maven wrapper, and Ubuntu
# noble ships Maven 3.8, so Maven 3.9.11 comes from Maven Central as a
# platform-independent tarball with a pinned sha512. The official
# eclipse-temurin image is multi-arch (amd64 + arm64).
_JDK_IMAGE = "eclipse-temurin:25-jdk-noble"
_MAVEN_VERSION = "3.9.11"
_MAVEN_SHA512 = (
    "bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516"
    "b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978"
)

# The build cache extension (.mvn/extensions.xml) is turned off so that every
# stage really compiles the patched tree; spotless runs at `validate` and is
# a formatting check, not a test.
_ENV = """export CI=true
export NO_COLOR=1
export MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
export MAVEN_ARGS="-B -ntp -Dstyle.color=never -T 4 -Djacoco.skip=true -Dspotless.check.skip=true -Dmaven.build.cache.enabled=false"
export TESTCONTAINERS_RYUK_DISABLED=true"""

# Every persistence test (PersistenceCapableTest, ResourceTest, ...) gets its
# database from support/test-database, whose PostgresTestContainer starts
# postgres:14 through Testcontainers, which needs a Docker daemon. No Docker
# daemon is available in the image, so prepare.sh puts this drop-in in its
# place. It has the same public surface TestDatabaseManager uses (start,
# getJdbcUrl, getUsername, getPassword) and serves the same "dtrack" template
# database (full schema migrated with MigrationExecutor) from a local
# PostgreSQL server. Tests that use Testcontainers directly are still
# excluded.
PG_CONTAINER_JAVA = r"""/*
 * This file is part of Dependency-Track.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (c) OWASP Foundation. All Rights Reserved.
 */
package org.dependencytrack.testing.database;

import org.dependencytrack.migration.MigrationExecutor;
import org.postgresql.ds.PGSimpleDataSource;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

/// DT_LOCAL_POSTGRES: local PostgreSQL server in place of the Testcontainers
/// container (no Docker daemon in the benchmark image). The "dtrack" template
/// database is created and migrated once, the same way the container did it.
public final class PostgresTestContainer {

    private static final String HOST_PORT = "127.0.0.1:5432";
    private static final String USERNAME = "dtrack";
    private static final String PASSWORD = "dtrack";

    public PostgresTestContainer() {}

    public void start() {
        try (final Connection connection = DriverManager.getConnection(
                        "jdbc:postgresql://" + HOST_PORT + "/postgres", USERNAME, PASSWORD);
                final Statement statement = connection.createStatement()) {
            try (final ResultSet rs = statement.executeQuery(
                    "SELECT 1 FROM pg_database WHERE datname = 'dtrack'")) {
                if (rs.next()) {
                    return;
                }
            }
            statement.execute("CREATE DATABASE dtrack");
        } catch (SQLException e) {
            throw new IllegalStateException("Failed to prepare local PostgreSQL template database", e);
        }

        final var dataSource = new PGSimpleDataSource();
        dataSource.setUrl(getJdbcUrl());
        dataSource.setUser(USERNAME);
        dataSource.setPassword(PASSWORD);
        new MigrationExecutor(dataSource).execute();
    }

    public String getJdbcUrl() {
        return "jdbc:postgresql://" + HOST_PORT + "/dtrack?loggerLevel=OFF";
    }

    public String getUsername() {
        return USERNAME;
    }

    public String getPassword() {
        return PASSWORD;
    }
}
"""

# Local PostgreSQL control. `setup` (prepare.sh) tunes the cluster for tests
# and creates the superuser role the test database code logs in with;
# `start` brings the server up (dropping a stale pid file left in the image
# layer); `reset` additionally drops every test database so each stage
# migrates the template from its own tree; `check` asserts the template
# exists and carries the migrated schema; `stop` shuts down cleanly.
PG_SH = r"""#!/bin/bash
set -eo pipefail
VER=$(ls /usr/lib/postgresql | sort -V | tail -n 1)
DATA=/var/lib/postgresql/$VER/main
CONF=/etc/postgresql/$VER/main
PSQL=(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -h /var/run/postgresql -d postgres -tAX)

start() {
    if ! pg_isready -q -h 127.0.0.1 -p 5432; then
        rm -f "$DATA/postmaster.pid"
        mkdir -p /var/run/postgresql
        chown postgres:postgres /var/run/postgresql
        pg_ctlcluster "$VER" main start
    fi
    for _ in $(seq 1 60); do
        pg_isready -q -h 127.0.0.1 -p 5432 && return 0
        sleep 1
    done
    echo "local PostgreSQL did not come up"
    return 1
}

case "$1" in
setup)
    if [ ! -d "$DATA" ] || [ ! -d "$CONF" ]; then
        pg_createcluster "$VER" main
    fi
    mkdir -p "$CONF/conf.d"
    printf '%s\n' "listen_addresses = '127.0.0.1'" "port = 5432" "max_connections = 300" \
        "fsync = off" "full_page_writes = off" "synchronous_commit = off" \
        > "$CONF/conf.d/zz-dependency-track-tests.conf"
    start
    if [ "$("${PSQL[@]}" -c "SELECT 1 FROM pg_roles WHERE rolname = 'dtrack'")" != 1 ]; then
        "${PSQL[@]}" -c "CREATE ROLE dtrack WITH LOGIN SUPERUSER PASSWORD 'dtrack'"
    fi
    ;;
start)
    start
    ;;
reset)
    start
    for db in $("${PSQL[@]}" -c "SELECT datname FROM pg_database WHERE datname ~ '^dtrack(_[0-9]+)?$'"); do
        "${PSQL[@]}" -c "DROP DATABASE IF EXISTS $db WITH (FORCE)"
    done
    ;;
check)
    start
    test "$("${PSQL[@]}" -c "SELECT 1 FROM pg_database WHERE datname = 'dtrack'")" = 1
    test "$(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -h /var/run/postgresql -d dtrack -tAX \
        -c "SELECT count(*) > 20 FROM pg_tables WHERE schemaname = 'public'")" = t
    ;;
stop)
    pg_ctlcluster "$VER" main stop -m fast
    ;;
*)
    echo "usage: $0 setup|start|reset|check|stop"
    exit 2
    ;;
esac
"""

# Helper. Multi-module Maven build: the scope is derived from test.patch.
#   modules  -> Maven modules (nearest pom.xml) holding the test sources that
#               test.patch touches; built with `-pl <modules> -am`.
#   classes  -> the test classes test.patch touches plus the concrete
#               surefire-pattern test classes in the same packages
#               (pass-to-pass scope). Abstract classes and classes that use
#               Testcontainers / Docker directly are left out.
#   broken   -> sources javac rejected that may be set aside: test sources,
#               or any source test.patch touches (test.patch also edits the
#               main-scope test fixture TestNotificationFactory). Printed as
#               "remove <file>" (test sources, files test.patch adds) or
#               "restore <file>" (non-test files test.patch modifies -> base
#               version). Any other compile error is fatal.
#   methods  -> @Test methods test.patch adds/rewrites in a removed test
#               source, reported FAILED under the surefire name.
#   junit    -> surefire XML -> "DT_TEST <STATUS> <module>:<class>#<method>".
TESTS_PY = r'''import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
TEST_PATCH = "/home/test.patch"
TEST_DIR = "src/test/java/"
INCLUDE = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase)\.java$")
TOUCHED = re.compile(r"^(Test\w*|\w*Test|\w*Tests|\w*TestCase|\w*IT)\.java$")
NEEDS_DOCKER = re.compile(r"org\.testcontainers|@Testcontainers|DockerClientFactory")
ERROR = re.compile(r"^\[ERROR\] (/\S+?\.java):\[\d+")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")
TEST_METHOD = re.compile(
    r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b(?:\s*\([^)]*\))?\s+"
    r"(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final)\s+)*void\s+(\w+)\s*\("
)
CLASS_DECL = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_]\w*)")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_files(patch_file):
    """{path: is_new_file} for every file the patch touches."""
    files = {}
    current = None
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                files[current] = False
                continue
            if current and line.startswith("new file mode"):
                files[current] = True
    return files


def module_of(rel):
    """Nearest directory (relative to the repo root) holding a pom.xml."""
    d = os.path.dirname(rel)
    while d:
        if os.path.isfile(os.path.join(REPO, d, "pom.xml")):
            return d
        d = os.path.dirname(d)
    return "."


def test_sources(patch_file):
    out = []
    for p in sorted(patch_files(patch_file)):
        if "/" + TEST_DIR in "/" + p and p.endswith(".java"):
            mod, _, rest = ("/" + p).partition("/" + TEST_DIR)
            out.append((mod.strip("/") or ".", rest))
    return out


def modules(patch_file):
    mods = set()
    for mod, rest in test_sources(patch_file):
        mods.add(module_of(os.path.join(mod, TEST_DIR, rest)))
    print(",".join(sorted(mods)))


def runnable(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    name = os.path.basename(path)[:-5]
    if re.search(r"\babstract\s+class\s+" + re.escape(name) + r"\b", text):
        return False
    return not NEEDS_DOCKER.search(text)


def classes(patch_file):
    out = set()
    for mod, rest in test_sources(patch_file):
        root = os.path.join(REPO, mod, TEST_DIR)
        pkg = os.path.dirname(rest)
        touched = os.path.join(root, rest)
        if TOUCHED.match(os.path.basename(rest)) and os.path.isfile(touched) and runnable(touched):
            out.add(rest[:-5].replace("/", "."))
        full = os.path.join(root, pkg)
        if not os.path.isdir(full):
            continue
        for f in sorted(os.listdir(full)):
            if INCLUDE.match(f) and runnable(os.path.join(full, f)):
                out.add(os.path.join(pkg, f[:-5]).replace("/", "."))
    print(",".join(sorted(out)))


def tracked_at_head(rel):
    return subprocess.run(
        ["git", "-C", REPO, "cat-file", "-e", "HEAD:" + rel],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def broken(log_file):
    touched = patch_files(TEST_PATCH)
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = ERROR.search(line.strip())
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        rel = os.path.relpath(f, REPO)
        if ("/" + TEST_DIR) in ("/" + rel):
            print("remove " + f)
        elif rel in touched:
            if touched[rel] or not tracked_at_head(rel):
                print("remove " + f)
            else:
                print("restore " + f)


def added_methods(patch_file, rel):
    """Method names declared on lines test.patch adds to file `rel`."""
    names = set()
    current = None
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                current = m.group(2)
                continue
            if current == rel and line.startswith("+") and not line.startswith("+++"):
                names.update(re.findall(r"\bvoid\s+(\w+)\s*\(", line))
    return names


def strip_java(text):
    """Blank out comments and string/char literals, keeping offsets."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def class_at(code, pos, outer):
    """Binary name (Outer$Inner) of the named class enclosing offset `pos`."""
    stack = []  # (depth, name or None)
    pending = None
    depth = 0
    for m in re.finditer(r"[{}]|" + CLASS_DECL.pattern, code[:pos]):
        tok = m.group(0)
        if tok == "{":
            depth += 1
            stack.append((depth, pending))
            pending = None
        elif tok == "}":
            if stack and stack[-1][0] == depth:
                stack.pop()
            depth -= 1
        else:
            pending = m.group(2)
    names = [name for _, name in stack if name]
    return "$".join(names) if names else outer


def methods(src_file, patch_file=TEST_PATCH):
    """Tests that test.patch adds or rewrites in a test source that no longer
    compiles -> FAILED (they cannot run). Untouched tests of that file are
    left unreported (NONE) rather than marked failed."""
    rel = os.path.relpath(src_file, REPO)
    if ("/" + TEST_DIR) not in ("/" + rel) or not runnable(src_file):
        return
    mod_dir, _, rest = ("/" + rel).partition("/" + TEST_DIR)
    module = module_of(rel)
    pkg = os.path.dirname(rest).replace("/", ".")
    outer = os.path.basename(rest)[:-5]
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        code = strip_java(fh.read())
    touched = added_methods(patch_file, rel)
    seen = set()
    for m in TEST_METHOD.finditer(code):
        name = m.group(1)
        if name not in touched:
            continue
        cls = class_at(code, m.start(), outer)
        tid = module + ":" + (pkg + "." if pkg else "") + cls + "#" + name
        if tid not in seen:
            seen.add(tid)
            print("DT_TEST FAILED " + tid + " (test source does not compile)")


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for module in [m for m in mods.split(",") if m]:
        rdir = os.path.join(REPO, module, "target", "surefire-reports")
        if not os.path.isdir(rdir):
            continue
        for f in sorted(os.listdir(rdir)):
            if not (f.startswith("TEST-") and f.endswith(".xml")):
                continue
            try:
                root = ET.parse(os.path.join(rdir, f)).getroot()
            except ET.ParseError:
                continue
            for tc in root.iter():
                if local(tc.tag) != "testcase":
                    continue
                cls = (tc.get("classname") or "").strip()
                name = method_name(tc.get("name") or "")
                if not cls or not name:
                    continue
                tid = re.sub(r"\s+", " ", module + ":" + cls + "#" + name).strip()
                tags = {local(c.tag) for c in tc}
                if tags & {"failure", "error"}:
                    status = "FAILED"
                elif "skipped" in tags:
                    status = "SKIPPED"
                else:
                    status = "PASSED"
                if tid not in results or rank[status] > rank[results[tid]]:
                    results[tid] = status
    for tid in sorted(results):
        print("DT_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {
        "modules": modules,
        "classes": classes,
        "broken": broken,
        "methods": methods,
        "junit": junit,
    }[cmd](arg)
'''

# Driver: reset the local test database, compile the scoped modules and their
# upstream reactor modules (so a fix in e.g. notification/api is picked up),
# set aside sources test.patch left uncompilable (reporting their new tests as
# FAILED), then run the scoped surefire suite. The exit code is Maven's,
# never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/dt_tests.py
cd "$REPO"

MODULES=$(python3 "$HELPER" modules "$1")
echo "DT_TEST_MODULES=$MODULES"
test -n "$MODULES"

# The local-PostgreSQL drop-in must still be in place after the patches.
if ! grep -q DT_LOCAL_POSTGRES "$REPO/[[PGFILE]]"; then
    echo "DT_LOCAL_POSTGRES drop-in missing from [[PGFILE]]"
    exit 1
fi
bash /home/dt_pg.sh reset

compile_tests() {
    local i action f rc
    : > /tmp/dt_removed
    for i in $(seq 1 10); do
        rc=0
        mvn test-compile -pl "$MODULES" -am > /tmp/dt_compile.log 2>&1 || rc=$?
        grep -E '^\[ERROR\]|BUILD (SUCCESS|FAILURE)' /tmp/dt_compile.log | head -n 100 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/dt_compile.log > /tmp/dt_broken
        if [ ! -s /tmp/dt_broken ]; then
            tail -n 80 /tmp/dt_compile.log
            echo "DT_COMPILE_FAILED (not a test-patch source error)"
            return 1
        fi
        while read -r action f; do
            if [ "$action" = restore ]; then
                echo "UNCOMPILABLE_TEST_PATCH_SOURCE_RESTORED ${f#$REPO/}"
                git checkout HEAD -- "${f#$REPO/}"
            else
                echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
                python3 "$HELPER" methods "$f" >> /tmp/dt_removed
                rm -f "$f"
            fi
        done < /tmp/dt_broken
    done
    echo "DT_COMPILE_FAILED (too many rounds)"
    return 1
}

run_tests() {
    local classes m rc=0
    for m in ${MODULES//,/ }; do
        rm -rf "$REPO/$m/target/surefire-reports"
    done
    compile_tests
    classes=$(python3 "$HELPER" classes "$1")
    echo "DT_TEST_CLASSES=$classes"
    test -n "$classes"
    mvn test -pl "$MODULES" -am -Dtest="$classes" -Dsurefire.failIfNoSpecifiedTests=false \
        > /tmp/dt_test.log 2>&1 || rc=$?
    grep -E 'Tests run:|FAIL|BUILD (SUCCESS|FAILURE)' /tmp/dt_test.log | tail -n 200 || true
    if [ "$rc" != 0 ] && ! grep -q 'There are test failures\|There are test errors\|Tests run:.*Failures' /tmp/dt_test.log; then
        tail -n 80 /tmp/dt_test.log
    fi
    cat /tmp/dt_removed
    python3 "$HELPER" junit "$MODULES"
    echo "DT_SUREFIRE_EXIT=$rc"
    return "$rc"
}

run_tests "$1"
'''

_PG_CONTAINER_PATH = (
    "support/test-database/src/main/java/org/dependencytrack/testing/database/"
    "PostgresTestContainer.java"
)

# Every stage script (re)installs the drop-in and (re)starts the local server
# itself, right after `cd` and before any patch, so it works from a freshly
# reset tree (`git reset --hard` / `git checkout -- .` revert the drop-in)
# relying only on installed packages and ~/.m2. Idempotent.
_LOCAL_PG = f"""# No Docker daemon: serve the shared test database from a local PostgreSQL
# server instead of a Testcontainers container (see dt_PostgresTestContainer.java).
test -f {_PG_CONTAINER_PATH}
cp /home/dt_PostgresTestContainer.java {_PG_CONTAINER_PATH}
bash /home/dt_pg.sh setup"""


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


class DependencyTrackImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["unzip", "postgresql"]

    def extra_setup(self) -> str:
        url = (
            "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
            f"{_MAVEN_VERSION}/apache-maven-{_MAVEN_VERSION}-bin.tar.gz"
        )
        return (
            f"RUN curl -fsSL -o /tmp/maven.tar.gz {url} && \\\n"
            f'    echo "{_MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c - && \\\n'
            "    mkdir -p /opt/maven && \\\n"
            "    tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1 && \\\n"
            "    rm -f /tmp/maven.tar.gz && \\\n"
            "    ln -sf /opt/maven/bin/mvn /usr/local/bin/mvn\n"
            "ENV CI=true \\\n"
            "    MAVEN_HOME=/opt/maven \\\n"
            '    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"\n'
            "RUN git config --global --add safe.directory '*' && java -version && mvn -v"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class DependencyTrackImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return DependencyTrackImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
        tests_py = TESTS_PY.replace("[[REPO]]", repo)
        tests_sh = TESTS_SH.replace("[[REPO]]", repo).replace(
            "[[PGFILE]]", _PG_CONTAINER_PATH
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(".", "dt_tests.py", tests_py),
            File(".", "dt_tests.sh", tests_sh),
            File(".", "dt_pg.sh", PG_SH),
            File(".", "dt_PostgresTestContainer.java", PG_CONTAINER_JAVA),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}

# No Docker daemon in the image: serve the shared test database from a local
# PostgreSQL server instead of a Testcontainers container (see
# dt_PostgresTestContainer.java). Tracked-file workaround, applied once here.
test -f {_PG_CONTAINER_PATH}
cp /home/dt_PostgresTestContainer.java {_PG_CONTAINER_PATH}
bash /home/dt_pg.sh setup

modules=$(python3 /home/dt_tests.py modules /home/test.patch)
echo "modules=$modules"
test -n "$modules"

# Resolve every build/test dependency and plugin of the scoped modules and
# their upstream reactor modules (protoc, Error Prone, DataNucleus enhancer,
# ...), compiling main and test sources at the base commit. Retries cover
# transient network failures.
ok=0
for attempt in 1 2 3; do
  if mvn test-compile -pl "$modules" -am; then
    ok=1
    break
  fi
  sleep 20
done
test "$ok" = 1

# Resolve the surefire JUnit Platform provider (and migrate the template
# database once) by running the base-commit test scope. Test failures at the
# base are not a provisioning error, so Maven itself ignores them; any other
# build failure still aborts.
classes=$(python3 /home/dt_tests.py classes /home/test.patch)
if [ -n "$classes" ]; then
  mvn test -pl "$modules" -am -Dtest="$classes" \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true
fi
for m in ${{modules//,/ }}; do
  rm -rf "$m/target/surefire-reports"
done

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
grep -q DT_LOCAL_POSTGRES {_PG_CONTAINER_PATH}
mvn -o -q test-compile -pl "$modules" -am
for m in ${{modules//,/ }}; do
  test -d "$m/target/test-classes"
done
ls -d ~/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/*/ > /dev/null
bash /home/dt_pg.sh check
bash /home/dt_pg.sh stop
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_LOCAL_PG}

bash /home/dt_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_LOCAL_PG}

git apply {apply} /home/test.patch
bash /home/dt_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_LOCAL_PG}

git apply {apply} /home/test.patch /home/fix.patch
bash /home/dt_tests.sh /home/test.patch
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("DependencyTrack", "dependency-track")
class DEPENDENCY_TRACK(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DependencyTrackImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        result_re = re.compile(
            r"^DT_TEST (PASSED|FAILED|SKIPPED) (\S+#\S+?)(?: \(test source does not compile\))?\s*$"
        )
        buckets = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }
        for raw in log.splitlines():
            found = result_re.match(raw.strip())
            if found:
                buckets[found.group(1)].add(found.group(2))

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
