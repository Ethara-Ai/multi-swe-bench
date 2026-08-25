import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# thingsboard/thingsboard — the ThingsBoard IoT platform: a 55-module Maven
# reactor (Java 17, Spring Boot 3.2, JUnit 4 + Spring Test), 34 of whose modules
# sit upstream of `application`, with an Angular front end (`ui-ngx`) hanging off
# the same reactor.
#
# Discovery (verified in Docker, maven:3.9.6-eclipse-temurin-17, at PR #10454's
# base sha eb7653f7; the three stages measured 9 passed / 9 passed + 2 failed /
# 11 passed, and Report.check() validates the instance with those two new tests
# as f2p and the nine pre-existing ones as p2p):
#
#  - No Maven wrapper ships with the repo (`./mvnw` is absent at this sha), so
#    the toolchain image must carry Maven itself. The root pom pins
#    maven.compiler.source/target 17 -> a JDK 17 image.
#
#  - `application` (where the controller/service tests live) depends on
#    `ui-ngx` at runtime scope, so a plain `-pl application -am` drags the
#    Angular build -- `install-node-and-yarn` + `yarn install` + `yarn run
#    build:prod` -- into every stage. frontend-maven-plugin 1.12.0 honours
#    `-Dskip.installnodeyarn=true -Dskip.yarn=true`, which reduces ui-ngx to an
#    empty jar in ~30s. The tests never read the UI bundle, so nothing is lost.
#
#  - The tests do NOT use an embedded database. `dao/src/test/resources/
#    sql-test.properties` points Spring at
#        jdbc:tc:postgresql:12.8:///thingsboard?...TC_INITFUNCTION=...
#    i.e. Testcontainers, which needs a Docker daemon *inside* the evaluation
#    container. The harness runs instances with no `--privileged` and no
#    docker.sock mount (see docker_util.run), so Testcontainers cannot work
#    here. run_tests.sh therefore installs the real thing: a PostgreSQL server
#    in the image, and it repoints every `jdbc:tc:postgresql` datasource at it.
#    The URL has to be rewritten in the checked-out file rather than passed as
#    `-Dspring.datasource.url`, because Spring gives `@TestPropertySource`
#    locations *higher* precedence than Java system properties -- a `-D` would
#    be silently ignored.
#
#  - Nothing then calls `PostgreSqlInitializer::initDb` (that was the
#    TC_INITFUNCTION), so run_tests.sh replays the same schema/data files
#    against the local server, in the same order -- and reads that order out of
#    PostgreSqlInitializer.java at run time rather than hardcoding it, so a PR
#    at a sha that adds or reorders a schema file still initialises correctly.
#
#  - Only the PR's own patched test classes are run (`-Dtest=`), and only in the
#    modules the PR's patches touch (`-pl <modules> -am`). ThingsBoard's
#    `application` suite is ~700 Spring-Boot test classes, each with
#    `@DirtiesContext(AFTER_CLASS)` -- a full-module run is hours per stage,
#    times three stages. run_tests.sh says out loud, in the stage log, what it
#    is not running.
#
#  - parse_log reads surefire's per-class JUnit XML, dumped to stdout by
#    run_tests.sh behind a `##### FILE:` marker. The marker carries the report's
#    path, which is what lets parse_log name the module a test came from --
#    the XML itself only knows the fully-qualified class name.
#
# Structure -- two levels, the cute_animals layout:
#   Level 1 (`ThingsboardImageBase`) returns a *string* dependency and carries
#   the `RUN git clone ... /home/thingsboard` line. That is what engages
#   DockerfileEnhancer: it rewrites the clone into
#   `git clone "${REPO_URL}"` + `git checkout ${BASE_COMMIT}` + the hardening
#   block, and prepends the syntax directive, ARG TARGETARCH, the proxy ARG/ENV
#   block, the OCI labels and the CA-cert symlinks. build_dataset.py passes
#   REPO_URL and BASE_COMMIT as build args for string-dependency images ONLY
#   (build_dataset.py:623-629), so putting the clone anywhere else would forfeit
#   both the build args and that canonical Dockerfile shape.
#   Because the enhancer bakes this PR's BASE_COMMIT into the base, the base tag
#   is per-PR: a PR-agnostic "base" tag would let a second PR of this repo
#   inherit the first PR's pinned tree. The expensive apt layer above the clone
#   is identical across PRs, so Docker's layer cache still shares it.
#   Level 2 (`ThingsboardImageDefault`) depends on that Image, so the enhancer
#   returns its dockerfile verbatim; it only COPYs the scripts and runs
#   prepare.sh, which lands after the base's hardening block.
#
# Multi-arch: `maven:3.9.6-eclipse-temurin-17` publishes linux/amd64 and
# linux/arm64 (verified with `docker manifest inspect`), and every apt package
# used here exists on both. Nothing below is arch-conditional, so ARG TARGETARCH
# is available but unused. Two caveats for an arm64 build:
#   * `application/pom.xml` hardcodes `<classifier>linux-x86_64</classifier>` on
#     io.netty:netty-transport-native-epoll. On arm64 that jar resolves but
#     carries no aarch64 native library. The test profile disables every
#     transport (`transport.*.enabled=false` in application-test.properties), so
#     the tests here do not load it -- but this has only been exercised on
#     amd64.
#   * The non-native platform of a multi-arch build runs under QEMU emulation,
#     where prepare.sh's Maven build (16m44s native) is an order of magnitude
#     slower. Its timeouts are sized for that; see TB_MVN_TIMEOUT below.


def _patched_paths(*patches: str) -> list[str]:
    """Repo-relative paths touched by the given unified diffs."""
    paths: set[str] = set()
    for patch in patches:
        for line in (patch or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            for raw in (parts[2], parts[3]):
                if raw.startswith(("a/", "b/")):
                    raw = raw[2:]
                if raw and raw != "/dev/null":
                    paths.add(raw)
    return sorted(paths)


def _test_classes(test_patch: str) -> list[str]:
    """Simple class names of the *.java files the test patch touches under src/test."""
    classes: set[str] = set()
    for path in _patched_paths(test_patch):
        if "/src/test/" in path and path.endswith(".java"):
            classes.add(path.rsplit("/", 1)[1][:-5])
    return sorted(classes)


# Maven flags shared by the warm-up build and by all three stages. Keeping them
# in one place is what makes the stage builds incremental against the warm-up:
# a differing flag would re-resolve plugins and re-run work prepare.sh already did.
#   skip.installnodeyarn/skip.yarn - frontend-maven-plugin 1.12.0's own skips;
#       collapse the Angular build (see the module note above).
#   license.skip / enforcer.skip   - the license header check and the enforcer
#       are release gates, not test gates; a patch is not held to them here.
#   pkg.*                          - the `application` module builds .deb/.rpm
#       packages at process-resources/package. Nothing under test reads them.
#   spring-boot.repackage.skip     - the fat jar is only needed to *run* the
#       server, never to test it.
_MVN_COMMON_FLAGS = (
    "-Dskip.installnodeyarn=true -Dskip.yarn=true "
    "-Dlicense.skip=true -Denforcer.skip=true "
    "-Dpkg.disabled=true -Dpkg.process-resources.phase=none -Dpkg.package.phase=none "
    "-Dspring-boot.repackage.skip=true -Dmaven.javadoc.skip=true"
)


class ThingsboardImageBase(Image):
    # Level 1. dependency() is a string AND this dockerfile carries the clone,
    # so DockerfileEnhancer.enhance() rewrites the clone into the standard
    # REPO_URL/BASE_COMMIT form plus Image._HARDENING_BLOCK, and prepends the
    # syntax directive, TARGETARCH/proxy ARGs, ENV block, labels and cert
    # symlinks. Nothing of that is written by hand here -- writing any of it
    # would duplicate what the enhancer injects.
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # JDK 17 (root pom pins maven.compiler.source/target 17) and a bundled
        # Maven, because the repo ships no ./mvnw at these shas. Publishes
        # linux/amd64 and linux/arm64. The image is Ubuntu 22.04, whose
        # `postgresql` package is PostgreSQL 14 -- inside ThingsBoard 3.7's
        # supported 12-15 range, and a superset of the 12.8 the Testcontainers
        # URL asked for.
        return "maven:3.9.6-eclipse-temurin-17"

    def image_tag(self) -> str:
        # Per-PR, because the enhancer bakes `git checkout ${BASE_COMMIT}` into
        # this image. A shared "base" tag would let the second PR of this repo
        # either inherit the first PR's pinned tree or silently overwrite it.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates postgresql postgresql-client \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ThingsboardImageDefault(Image):
    # Level 2 -- per-PR scripts. dependency() is an Image, so the enhancer
    # returns this dockerfile verbatim and build_dataset.py passes it no build
    # args; everything that needs REPO_URL/BASE_COMMIT already happened in the
    # base. This layer only adds the scripts and the warm-up.
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ThingsboardImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        classes = _test_classes(self.pr.test_patch)
        dtest = ("-Dtest=" + ",".join(classes)) if classes else ""
        patched_paths = "\n".join(
            _patched_paths(self.pr.fix_patch, self.pr.test_patch)
        )

        # Sourced by prepare.sh and by run_tests.sh, so the warm-up build and the
        # three stages run Maven under exactly the same environment -- a value
        # that drifted between them would make the warm-up stop rehearsing what
        # the stages actually do. It lives in a script rather than as ENV in the
        # base image so that the base Dockerfile keeps precisely the shape
        # DockerfileEnhancer emits: FROM, the ARG/ENV/LABEL/cert infrastructure,
        # then the clone, checkout and hardening block.
        mvn_env = """#!/bin/bash
# Heap and encoding for the Maven JVM itself (not for the forked test JVM).
export MAVEN_OPTS="-Xmx2g -Dfile.encoding=UTF-8"

# Maven 3.9 resolves artifacts through maven-resolver's native HTTP transport,
# whose default request (socket read) timeout is 1_800_000 ms. A Maven Central
# connection that goes quiet mid-download therefore parks the build for THIRTY
# MINUTES before the first retry -- observed here as a warm-up that sat at 0%
# CPU in sun.nio.ch.Net.poll with 33 of 34 modules already built. Cap it at two
# minutes and retry instead; the wagon properties cover the plugins that still
# use the older transport.
export MAVEN_ARGS="-Daether.connector.connectTimeout=15000 -Daether.connector.requestTimeout=120000 -Daether.connector.http.retryHandler.count=5 -Dmaven.wagon.http.pool=false -Dhttp.keepAlive=false -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Dmaven.wagon.http.retryHandler.count=5 -Dmaven.wagon.rto=120000"

# Surefire forks a JVM to run the tests, and the root pom's <argLine> sets no
# -Xmx, so that fork would take the JVM default of 25% of container RAM -- too
# little for a Spring Boot context on a modest runner, and not overridable with
# -DargLine because the pom sets <argLine> literally rather than through the
# property. _JAVA_OPTIONS reaches the fork. MaxRAMPercentage applies only where
# no explicit -Xmx exists, so this sizes the fork and leaves MAVEN_OPTS's own
# -Xmx2g for the Maven JVM untouched, and it scales with whatever the runner
# gives the container instead of hardcoding a number.
export _JAVA_OPTIONS="-XX:MaxRAMPercentage=50.0"
"""

        check_git_changes = """#!/bin/bash
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

        # The single runner behind all three stages. Everything that differs
        # between stages happens before it is called (which patches are applied);
        # everything it does is identical, so the three logs are comparable.
        run_tests = """#!/bin/bash
# Runs this PR's tests and dumps surefire's JUnit XML for parse_log.
# `set -e` is deliberately absent HERE (unlike the three stage wrappers, which
# do use it): a failing test, a test that will not compile until fix.patch
# lands, and a module that fails to build are all expected outcomes of one stage
# or another, and each must still reach the report dump below.
set -uo pipefail
cd /home/__REPO__

source /home/mvn_env.sh

# The stage wrappers run natively, so the default is sized for a native run.
# prepare.sh raises it, because the non-native platform of a multi-arch build
# executes under QEMU emulation.
MVN_TIMEOUT="${TB_MVN_TIMEOUT:-5400}"

# ------------------------------------------------------------ 1. reactor
# Which Maven modules to build is derived from the paths this PR's own patches
# touch: for each path, walk up to the nearest directory that owns a pom.xml.
# That walk happens here, in the container, and not in Python, because only the
# checked-out tree knows which directories are real modules at this sha.
MODULES=""
add_mod() {
  case ",$MODULES," in *",$1,"*) return 0;; esac
  MODULES="${MODULES:+$MODULES,}$1"
}
while IFS= read -r p; do
  [ -n "$p" ] || continue
  d=$(dirname "$p")
  while [ "$d" != "." ] && [ "$d" != "/" ] && [ ! -f "$d/pom.xml" ]; do
    d=$(dirname "$d")
  done
  # "." is the aggregator pom. Selecting it would mean the whole reactor, and
  # -am already pulls it in as every module's parent, so it is skipped.
  if [ "$d" != "." ] && [ "$d" != "/" ]; then add_mod "$d"; fi
done <<'TB_PATCHED_PATHS'
__PATCHED_PATHS__
TB_PATCHED_PATHS

if [ -n "$MODULES" ]; then
  # -am so a patched upstream module (dao, common/*) is recompiled from the
  # patched source and the downstream test module links against it, rather than
  # against the jar prepare.sh installed at the base sha.
  PL="-pl $MODULES -am"
  echo "TB RUNNER: reactor = -pl $MODULES -am"
else
  PL=""
  echo "TB RUNNER: no Maven module could be resolved from the patched paths;"
  echo "TB RUNNER: falling back to the FULL reactor"
fi

# ------------------------------------------------------------ 2. test selection
DTEST="__DTEST__"
if [ -n "$DTEST" ]; then
  echo "TB RUNNER: running only the test classes this PR patches: ${DTEST#-Dtest=}"
  echo "TB RUNNER: NOT RUN: every other test class in the selected modules."
  echo "TB RUNNER: NOT RUN: reason - ThingsBoard's application module holds ~700"
  echo "TB RUNNER: NOT RUN: Spring Boot test classes, each rebuilding its own"
  echo "TB RUNNER: NOT RUN: application context (@DirtiesContext AFTER_CLASS);"
  echo "TB RUNNER: NOT RUN: a whole-module run is hours long, per stage."
else
  echo "TB RUNNER: the test patch names no test class; running EVERY test in the"
  echo "TB RUNNER: selected modules. This can take hours."
fi

# ------------------------------------------------------------ 3. database
# The checked-out sql-test.properties asks for a Testcontainers-managed
# PostgreSQL (jdbc:tc:...), which needs a Docker daemon inside this container;
# there is none. Repoint it at the server installed in the image. This is a sed
# on the working tree rather than a -Dspring.datasource.url because Spring ranks
# @TestPropertySource locations ABOVE Java system properties -- a -D would lose.
service postgresql start > /dev/null 2>&1
for _ in $(seq 1 60); do pg_isready -q -h 127.0.0.1 -p 5432 && break; sleep 1; done
if ! pg_isready -q -h 127.0.0.1 -p 5432; then
  echo "TB RUNNER: INFRASTRUCTURE FAILURE: PostgreSQL did not come up"
  exit 1
fi

# Give the postgres role the password sql-test.properties will authenticate
# with. Done here, per stage, rather than baked into the base image, so the base
# Dockerfile carries nothing but the apt install above its clone. Idempotent,
# and it costs well under a second. The heredoc avoids nesting quotes inside
# `su -c`.
su postgres -c "psql -v ON_ERROR_STOP=1 -q" > /dev/null <<'TB_SET_PASSWORD'
ALTER USER postgres WITH PASSWORD 'postgres';
TB_SET_PASSWORD

export PGPASSWORD=postgres
PSQL="psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -q"

for f in $(grep -rls '^spring.datasource.url=jdbc:tc:postgresql' \\
             --include='*-test.properties' . 2>/dev/null); do
  sed -i \\
    -e 's#^spring.datasource.url=.*#spring.datasource.url=jdbc:postgresql://127.0.0.1:5432/thingsboard#' \\
    -e 's#^spring.datasource.driverClassName=.*#spring.datasource.driverClassName=org.postgresql.Driver#' \\
    "$f"
  echo "TB RUNNER: repointed ${f#./} at the in-container PostgreSQL"
done

# A brand-new database per stage, so no stage can inherit rows another left
# behind. This subsumes PostgreSqlInitializer's own drop-all-tables.sql step,
# which only exists because a TC_DAEMON container outlives the JVM that made it.
$PSQL -d postgres -c "DROP DATABASE IF EXISTS thingsboard" > /dev/null
$PSQL -d postgres -c "CREATE DATABASE thingsboard" > /dev/null

# With Testcontainers out of the picture nothing invokes the TC_INITFUNCTION
# (PostgreSqlInitializer::initDb), so its work is replayed here. The file list
# is read out of that class rather than hardcoded, so a sha that adds, drops or
# reorders a schema file still initialises the way its own tests expect.
INIT_SRC=$(find . -path '*/dao/*' -name PostgreSqlInitializer.java | head -1)
if [ -z "$INIT_SRC" ]; then
  echo "TB RUNNER: INFRASTRUCTURE FAILURE: PostgreSqlInitializer.java not found;"
  echo "TB RUNNER: cannot determine the schema init order"
  exit 1
fi
echo "TB RUNNER: schema init order read from ${INIT_SRC#./}"

resolve_sql() {
  # Mirrors Resources.getResource(): dao's test resources shadow its main
  # resources on the test classpath, so they are searched in that order.
  for root in dao/src/test/resources dao/src/main/resources; do
    if [ -f "$root/$1" ]; then echo "$root/$1"; return 0; fi
  done
  return 1
}

for name in $(sed -n '/sqlFiles/,/);/p' "$INIT_SRC" | grep -o '"[^"]*\\.sql"' | tr -d '"'); do
  path=$(resolve_sql "$name") || {
    echo "TB RUNNER: INFRASTRUCTURE FAILURE: schema file $name not found in dao resources"
    exit 1
  }
  echo "TB RUNNER: applying $path"
  if ! $PSQL -d thingsboard -f "$path"; then
    echo "TB RUNNER: INFRASTRUCTURE FAILURE: $path did not apply"
    exit 1
  fi
done

# ------------------------------------------------------------ 4. run
# Reports from an earlier run must never be replayed as if this run had produced
# them: a stage that collects nothing has to look like it collected nothing.
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null

MVN_LOG=/home/mvn-stage.log
rc=0
timeout --kill-after=60 "$MVN_TIMEOUT" \\
  mvn -B --no-transfer-progress -fae test \\
    $PL $DTEST \\
    -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false \\
    -Dmaven.test.failure.ignore=true -Dsurefire.timeout=1800 \\
    __MVN_FLAGS__ 2>&1 | tee "$MVN_LOG"
rc=${PIPESTATUS[0]}

# ------------------------------------------------------------ 5. report
echo '===== BEGIN TEST RESULTS ====='
find . -path '*/target/surefire-reports/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
      # The marker carries the report's path because the XML inside knows only
      # a class name -- the path is what tells parse_log which module, and so
      # which source file, the class belongs to.
      echo "##### FILE: ${f#./}"
      cat "$f"
      echo
    done
echo '===== END TEST RESULTS ====='

# ------------------------------------------------------------ 6. verdict
# Maven exits non-zero for two very different reasons here, and they must not be
# conflated. A test that fails, or a test that does not yet compile because
# fix.patch has not been applied, is the *expected* result of the baseline and
# test stages -- the empty or failing report above is the measurement. A build
# that never got as far as running tests (dependency resolution, a killed JVM,
# the timeout) produces the same empty report while measuring nothing, so it is
# called out explicitly and propagated.
if [ "$rc" -ne 0 ]; then
  echo "TB RUNNER: maven exited $rc"
  if grep -qE "Could not resolve dependencies|Non-resolvable|Could not transfer|Connection refused|Failed to read artifact descriptor|Plugin .* or one of its dependencies could not be resolved" "$MVN_LOG"; then
    echo "TB RUNNER: INFRASTRUCTURE FAILURE: dependency resolution failed;"
    echo "TB RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "TB RUNNER: INFRASTRUCTURE FAILURE: the run hit the ${MVN_TIMEOUT}s cap;"
    echo "TB RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  echo "TB RUNNER: treating this as a test/compile failure, which is a result"
fi
exit 0
""".replace("__REPO__", repo).replace("__DTEST__", dtest).replace(
            "__PATCHED_PATHS__", patched_paths
        ).replace("__MVN_FLAGS__", _MVN_COMMON_FLAGS)

        # Runs at image-build time, after the base image's clone, checkout and
        # hardening block. CI=true is exported here too, so the warm-up runs in
        # the same environment as the three stages it is rehearsing.
        prepare = """#!/bin/bash
set -e
export CI=true
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

source /home/mvn_env.sh

git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# QEMU emulates the non-native platform of a multi-arch build, where this build
# is an order of magnitude slower than the 16m44s it takes natively. Both the
# outer cap here and TB_MVN_TIMEOUT (which run_tests.sh reads for the warm-up
# below) are sized for that, not for the native case.
export TB_MVN_TIMEOUT=14400

# Warm ~/.m2 and install every module's jar AND test-jar. The test-jar matters:
# `application` depends on dao's test-jar, and a stage that runs the `test`
# lifecycle phase never packages one, so it has to already be in the local
# repository. -DskipTests (not -Dmaven.test.skip) so test sources still compile
# and the test-jars are real.
#
# `-pl application -am` rather than the whole reactor: the 34 modules it selects
# are exactly the ones under `application`, which is where the tests live, and
# they pull in every other Java module that holds tests (dao, common/*,
# rule-engine, transport/*, tools, rest-client). The 9 modules it leaves out are
# msa/*'s Docker-image projects, which consume the .deb/.rpm artifacts that
# -Dpkg.package.phase=none deliberately turns off -- a full-reactor warm-up
# fails on them for exactly that reason (verified), they carry no unit tests,
# and nothing under test depends on them.
timeout --kill-after=60 14400 mvn -B --no-transfer-progress -fae install -DskipTests \\
  -pl application -am \\
  __MVN_FLAGS__ || true

# Run this PR's own test selection once, at the base sha. This pulls the
# surefire provider jars into ~/.m2 -- they are resolved only when tests really
# execute, so a -DskipTests build does not fetch them and all three stages would
# otherwise hit Maven Central at the worst possible moment -- and it proves the
# PostgreSQL path works while the image is still being built.
bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -40 /home/prepare-warmup.log || true

# The warm-up's reports must not ship inside the image: a stage that collects
# nothing would otherwise replay them and look like a clean run.
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true
service postgresql stop || true

# run_tests.sh rewrote sql-test.properties in the work tree. Undo it, so the
# image ships a tree that is clean at the base sha and test.patch/fix.patch
# still apply. ~/.m2 and target/ are outside the work tree, so the warm-up
# survives.
git reset --hard
bash /home/check_git_changes.sh
""".replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha).replace(
            "__MVN_FLAGS__", _MVN_COMMON_FLAGS
        )

        # `set -e` in the three wrappers is load-bearing, and is the one thing
        # that must NOT be relaxed to match run_tests.sh: without it a failing
        # `git apply` does not stop the script, the stage runs against the wrong
        # tree, and the result is a plausible-looking log that misclassifies the
        # PR's own tests (f2p silently becomes n2p). Verified in-container: under
        # `set -uo pipefail` the script continued past a failed apply.
        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "mvn_env.sh", mvn_env),
            File(".", "run_tests.sh", run_tests),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("thingsboard", "thingsboard")
class Thingsboard(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ThingsboardImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        # run_tests.sh emits, per surefire report:
        #   ##### FILE: <module>/target/surefire-reports/TEST-<fqcn>.xml
        #   <?xml ...><testsuite ...>...</testsuite>
        # Splitting on the marker keeps each report bound to the path it came
        # from; the path is the only thing that says which module -- and so
        # which source tree -- a class lives in.
        marker = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
        chunks = marker.split(clean)[1:]  # drop everything before the first marker

        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        for report_path, body in zip(chunks[0::2], chunks[1::2]):
            # "application/target/surefire-reports/TEST-x.xml" -> "application"
            m = re.match(r"(?:\./)?(.*?)/target/surefire-reports/", report_path)
            module = m.group(1) if m else ""

            for tc in testcase_re.finditer(body):
                attrs = tc.group(1) or ""
                closing = tc.group(2)
                inner = tc.group(3) or ""

                nm = name_re.search(attrs)
                cn = classname_re.search(attrs)
                if not nm or not cn:
                    continue

                # The id is "<repo-relative source file>::<method>" -- the
                # path-embedded shape report.py's matchers expect, so that
                # _test_name_matches_files can split on the first "::" and
                # compare the head against a path the test patch touched.
                # Surefire reports only a fully-qualified class name, so the
                # file is reconstructed from it: Maven's standard test source
                # root under the module the report was written in, and a "$"
                # marks a nested class, declared in its outer class's file.
                rel = cn.group(1).split("$", 1)[0].replace(".", "/") + ".java"
                source_file = f"{module}/src/test/java/{rel}" if module else f"src/test/java/{rel}"

                # Only name= and classname= are read, never time= -- a duration
                # varies between stages, and a test id that carries one appears
                # as two different tests across the run/test/fix logs.
                test_id = f"{source_file}::{nm.group(1)}"

                if closing == "/>":
                    passed_tests.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed_tests.add(test_id)
                elif "<skipped" in inner:
                    skipped_tests.add(test_id)
                else:
                    # A <testcase> with only <system-out>/<flakyFailure> children
                    # is a test that ran and did not fail.
                    passed_tests.add(test_id)

        # Keep the buckets disjoint. A rerun can report the same id twice;
        # failure wins, because crediting a test that was ever seen failing as
        # passed is the unsafe direction.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
