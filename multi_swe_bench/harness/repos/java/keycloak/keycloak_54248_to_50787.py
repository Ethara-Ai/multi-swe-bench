import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_VERSION = "24.22.2"

# -DskipProtoLock: the infinispan proto-schema compatibility check needs a git
# remote it can resolve ("hostname can't be null" inside the build); upstream CI
# skips it the same way for test builds.
_MVN_COMMON_FLAGS = (
    "-DskipExamples -DskipProtoLock=true "
    "-Dmaven.javadoc.skip=true -Dgpg.skip=true -Dspotless.check.skip=true"
)

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


def _test_files(test_patch: str) -> list[str]:
    """`src/test` java files the test patch touches (they decide the test modules)."""
    return [
        p
        for p in _patched_paths(test_patch)
        if "/src/test/" in p and p.endswith(".java")
    ]


def _test_classes(test_patch: str) -> list[str]:
    """Simple class names of the `src/test` java files the test patch touches.

    Every name is passed to surefire, helpers included:
    `-Dsurefire.failIfNoSpecifiedTests=false` makes a name that matches no test
    a no-op, whereas filtering by naming convention would silently drop a real
    test class that does not follow it.

    Suite aggregators (`*/suites/*`, `*Suite.java`) are the exception: naming
    one makes surefire run every class it lists (hundreds of server tests), and
    the classes the patch actually adds or edits are already named directly.
    """
    return sorted({
        p.rsplit("/", 1)[1][:-5]
        for p in _test_files(test_patch)
        if "/suites/" not in p and not p.endswith("Suite.java")
    })


def _js_test_files(test_patch: str) -> list[str]:
    """Front-end spec files the test patch touches (run by Playwright, not Maven)."""
    return [
        p
        for p in _patched_paths(test_patch)
        if p.startswith("js/") and p.endswith((".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx"))
    ]


class Keycloak54248To50787ImageBase(Image):
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
        return "maven:3.9.16-eclipse-temurin-25"

    def image_tag(self) -> str:
        return "base-54248_to_50787"

    def workdir(self) -> str:
        return "base-54248_to_50787"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        # Proxy/cert ARGs, REPO_URL/BASE_COMMIT ARGs, labels and CA symlinks are
        # derived from pr.org/pr.repo by the shared harness helper. Rendering the
        # syntax directive here makes DockerfileEnhancer.enhance() leave this
        # file untouched, so the base stays a plain clone: the commit checkout
        # and history hardening belong to the per-PR image, not the shared base.
        infra = DockerfileEnhancer._infrastructure_block(self, image_name)

        dockerfile = f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}
FROM {image_name}

{infra}
{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates unzip xz-utils libicu-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""
        return re.sub(r"\n{3,}", "\n\n", dockerfile)


class Keycloak54248To50787ImageDefault(Image):
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
        return Keycloak54248To50787ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        classes = _test_classes(self.pr.test_patch)
        dtest = ("-Dtest=" + ",".join(classes)) if classes else ""
        js_tests = _js_test_files(self.pr.test_patch)
        test_files = "\n".join(_test_files(self.pr.test_patch))
        patched_paths = "\n".join(
            _patched_paths(self.pr.fix_patch, self.pr.test_patch)
        )

        mvn_env = """#!/bin/bash
export MAVEN_OPTS="-Xmx3g -Dfile.encoding=UTF-8"

export MAVEN_ARGS="-Daether.connector.connectTimeout=15000 -Daether.connector.requestTimeout=120000 -Daether.connector.http.retryHandler.count=5 -Dmaven.wagon.http.pool=false -Dhttp.keepAlive=false -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Dmaven.wagon.http.retryHandler.count=5 -Dmaven.wagon.rto=120000"

export _JAVA_OPTIONS="-XX:MaxRAMPercentage=50.0"

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
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

        # Sourced by prepare.sh and run_tests.sh. Everything here is derived from
        # paths: the test modules from the test patch's src/test files (static),
        # and the modules to re-install from what the applied patches changed in
        # the working tree (dynamic, so run.sh rebuilds nothing).
        kc_lanes = """#!/bin/bash
# shellcheck disable=SC2034
cd /home/__REPO__

kc_pom_dir() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ] && [ ! -f "$d/pom.xml" ]; do
    d=$(dirname "$d")
  done
  if [ "$d" != "." ] && [ "$d" != "/" ]; then printf '%s' "$d"; fi
}

kc_add() {  # kc_add VAR value  -- comma-list append without duplicates
  local cur="${!1}"
  case ",$cur," in *",$2,"*) return 0;; esac
  printf -v "$1" '%s' "${cur:+$cur,}$2"
}

kc_selectable() {  # module is part of the default reactor
  ./mvnw -B -q -pl "$1" validate > /dev/null 2>&1
}

# kc_reactor_ids <reactor selection args...>: groupId:artifactId of every module
# Maven puts in that reactor, one per line, sorted. Read from the per-project
# headers of an offline, fail-never validate; nothing is built.
kc_reactor_ids() {
  ./mvnw -B -o --no-transfer-progress -fn "$@" validate \\
    -Dmdep.skip=true -Denforcer.skip=true __MVN_FLAGS__ 2>/dev/null \\
    | sed -n 's/^\\[INFO\\] -*< \\([^ ]*\\) >-*$/\\1/p' | sort -u
}

# Written by prepare.sh: the install selection and the modules it pulled in.
KC_INSTALL_FILE=/home/kc_install.txt
KC_UPSTREAM_FILE=/home/kc_upstream.txt

# Server-backed lanes: the new test framework (tests/, */tests/) starts a
# distribution server from the keycloak-quarkus-dist zip; the arquillian
# testsuite starts auth-server-quarkus. Plain unit-test modules need neither.
kc_is_new_lane() { case "/$1/" in */tests/*) case "$1" in testsuite/*) return 1;; esac; return 0;; esac; return 1; }
kc_is_old_lane() { case "$1" in testsuite/integration-arquillian/*) return 0;; esac; return 1; }
kc_is_test_side() { case "/$1/" in */tests/*|/testsuite/*|/test-framework/*) return 0;; esac; return 1; }

TEST_MODULES=""
while IFS= read -r p; do
  [ -n "$p" ] || continue
  m=$(kc_pom_dir "$p")
  [ -n "$m" ] && kc_add TEST_MODULES "$m"
done <<'KC_TEST_FILES'
__TEST_FILES__
KC_TEST_FILES

PATCH_MODULES=""
while IFS= read -r p; do
  [ -n "$p" ] || continue
  m=$(kc_pom_dir "$p")
  [ -n "$m" ] && kc_add PATCH_MODULES "$m"
done <<'KC_PATCHED_PATHS'
__PATCHED_PATHS__
KC_PATCHED_PATHS

NEW_LANE=0; OLD_LANE=0; UNIT_MODULES=""; OLD_MODULES=""
for m in ${TEST_MODULES//,/ }; do
  if kc_is_old_lane "$m"; then OLD_LANE=1; kc_add OLD_MODULES "$m"
  else kc_is_new_lane "$m" && NEW_LANE=1; kc_add UNIT_MODULES "$m"
  fi
done

KC_AUTH_SERVER=testsuite/integration-arquillian/servers/auth-server/quarkus
KC_DIST_MODULES=quarkus/server,quarkus/dist
""".replace("__REPO__", repo).replace("__TEST_FILES__", test_files).replace(
            "__PATCHED_PATHS__", patched_paths
        ).replace("__MVN_FLAGS__", _MVN_COMMON_FLAGS)

        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

source /home/mvn_env.sh
source /home/kc_lanes.sh

MVN_TIMEOUT="${KC_MVN_TIMEOUT:-7200}"
MVN_LOG=/home/mvn-stage.log
: > "$MVN_LOG"

HOSTS_SRC=.github/actions/update-hosts/hosts
if [ -f "$HOSTS_SRC" ] && ! grep -q "localtest.me" /etc/hosts 2>/dev/null; then
  if printf '\\n' >> /etc/hosts 2>/dev/null && cat "$HOSTS_SRC" >> /etc/hosts 2>/dev/null; then
    echo "KC RUNNER: applied $(grep -c . "$HOSTS_SRC") /etc/hosts line(s) from $HOSTS_SRC"
  else
    echo "KC RUNNER: WARNING: could not write /etc/hosts; *.localtest.me will not resolve"
  fi
fi

empty_results() {
  echo '===== BEGIN TEST RESULTS ====='
  echo '===== END TEST RESULTS ====='
}

JS_TESTS="__JS_TESTS__"
DTEST="__DTEST__"
if [ -z "$DTEST" ]; then
  if [ -n "$JS_TESTS" ]; then
    echo "KC RUNNER: UNSUPPORTED LANE: this PR's only test changes are front-end"
    echo "KC RUNNER: UNSUPPORTED LANE: specs, which Playwright runs, not Maven:"
    for f in $JS_TESTS; do echo "KC RUNNER: UNSUPPORTED LANE:   $f"; done
    echo "KC RUNNER: UNSUPPORTED LANE: skip this instance (build_dataset.py --skips)."
  else
    echo "KC RUNNER: the test patch names no java test class; nothing to run."
  fi
  empty_results
  exit 0
fi

mvn_step() {  # mvn_step <label> <mvn args...>
  local label="$1"; shift
  echo "KC RUNNER: $label: ./mvnw $*"
  timeout --kill-after=60 "$MVN_TIMEOUT" ./mvnw -B --no-transfer-progress "$@" \\
    __MVN_FLAGS__ 2>&1 | tee -a "$MVN_LOG"
  return "${PIPESTATUS[0]}"
}

# 1. Re-install every module whose non-test sources the applied patches changed,
#    so test modules (run without -am, below) and the servers see the patched
#    code. -am is never used for a test run: it drags quarkus/dist into a
#    reactor that stops before `package`, and dist's validate-phase unpack then
#    fails with MDEP-98.
REBUILD=""
SERVER_CHANGED=0       # server code changed -> repackage the distribution
AUTH_SERVER_CHANGED=0  # ...or arquillian server-side providers -> repackage auth-server
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in */src/test/*) continue;; esac
  m=$(kc_pom_dir "$f")
  [ -n "$m" ] || continue
  case ",$REBUILD," in *",$m,"*) continue;; esac
  if kc_selectable "$m"; then
    kc_add REBUILD "$m"
    kc_is_test_side "$m" || { SERVER_CHANGED=1; AUTH_SERVER_CHANGED=1; }
    case "$m" in testsuite/integration-arquillian/servers/*) AUTH_SERVER_CHANGED=1;; esac
  else
    echo "KC RUNNER: changed path $f is outside the reactor ($m); not rebuilt"
  fi
done < <( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u )

rc=0
if [ -n "$REBUILD" ] && [ -s "$KC_UPSTREAM_FILE" ] && [ -s "$KC_INSTALL_FILE" ]; then
  # Rebuild what the tests actually consume: every module downstream of a
  # changed one that is also upstream of the test/server slice prepare.sh
  # installed. This catches jars that bundle a copy of a changed module (the
  # shaded keycloak-admin-cli carries keycloak-core's classes), which
  # re-installing only the changed modules leaves stale.
  KC_UP_NOW=/tmp/kc_upstream_now.txt
  if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | grep 'pom\\.xml$' > /dev/null; then
    # A patched pom can add modules to the slice's upstream; install those too.
    kc_reactor_ids -pl "$(cat "$KC_INSTALL_FILE")" -am > "$KC_UP_NOW"
  else
    cp "$KC_UPSTREAM_FILE" "$KC_UP_NOW"
  fi
  kc_reactor_ids -pl "$REBUILD" -amd > /tmp/kc_downstream.txt
  TARGETS=$( { comm -12 /tmp/kc_downstream.txt "$KC_UP_NOW"; comm -13 "$KC_UPSTREAM_FILE" "$KC_UP_NOW"; } | sort -u | paste -sd, -)
  if [ -n "$TARGETS" ]; then
    echo "KC RUNNER: changed modules: $REBUILD"
    echo "KC RUNNER: rebuilding $(echo "$TARGETS" | tr ',' '\\n' | grep -c .) module(s) between them and the test slice"
    mvn_step "re-install patched modules and their consumers" install -Dmaven.test.skip=true -pl "$TARGETS" || rc=$?
  else
    echo "KC RUNNER: changed modules ($REBUILD) feed nothing the tests use; nothing to re-install"
  fi
elif [ -n "$REBUILD" ]; then
  # Fallback when prepare.sh recorded no reactor listing.
  mvn_step "re-install patched modules" install -Dmaven.test.skip=true -pl "$REBUILD" || rc=$?
  if [ "$rc" -eq 0 ] && [ "$SERVER_CHANGED" = "1" ] && { [ "$NEW_LANE" = "1" ] || [ "$OLD_LANE" = "1" ]; }; then
    mvn_step "repackage the distribution" install -Dmaven.test.skip=true -pl "$KC_DIST_MODULES" || rc=$?
  fi
  if [ "$rc" -eq 0 ] && [ "$AUTH_SERVER_CHANGED" = "1" ] && [ "$OLD_LANE" = "1" ]; then
    mvn_step "repackage auth-server-quarkus" install -Dmaven.test.skip=true -pl "$KC_AUTH_SERVER" || rc=$?
  fi
fi
if [ -n "$REBUILD" ]; then
  if [ "$rc" -ne 0 ]; then
    echo "KC RUNNER: patched code does not build (maven exited $rc); no test can run"
    empty_results
    exit 0
  fi
else
  echo "KC RUNNER: no non-test source changed in the working tree; nothing to re-install"
fi

echo "KC RUNNER: running only the test classes this PR patches: ${DTEST#-Dtest=}"
echo "KC RUNNER: NOT RUN: every other test class in the selected modules (keycloak's"
echo "KC RUNNER: NOT RUN: suites boot a server per class; a module run takes hours)."

find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null

TEST_ARGS="-fae test $DTEST -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -Dmaven.test.failure.ignore=true -Dsurefire.timeout=3600"
if [ -n "$UNIT_MODULES" ]; then
  # shellcheck disable=SC2086
  mvn_step "tests ($UNIT_MODULES)" $TEST_ARGS -pl "$UNIT_MODULES" || rc=$?
fi
if [ -n "$OLD_MODULES" ]; then
  # shellcheck disable=SC2086
  mvn_step "arquillian tests ($OLD_MODULES)" $TEST_ARGS -Pauth-server-quarkus -pl "$OLD_MODULES" || rc=$?
fi

echo '===== BEGIN TEST RESULTS ====='
find . -path '*/target/surefire-reports/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
      echo "##### FILE: ${f#./}"
      cat "$f"
      echo
    done
echo '===== END TEST RESULTS ====='

if [ "$rc" -ne 0 ]; then
  echo "KC RUNNER: maven exited $rc"
  if grep -qE "Could not resolve dependencies|Non-resolvable|Could not transfer|Connection refused|Failed to read artifact descriptor|Plugin .* or one of its dependencies could not be resolved|Could not find the selected project" "$MVN_LOG"; then
    echo "KC RUNNER: INFRASTRUCTURE FAILURE: dependency or module resolution failed;"
    echo "KC RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "KC RUNNER: INFRASTRUCTURE FAILURE: the run hit the ${MVN_TIMEOUT}s cap;"
    echo "KC RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  echo "KC RUNNER: treating this as a test/compile failure, which is a result"
fi
exit 0
""".replace("__REPO__", repo).replace("__DTEST__", dtest).replace(
            "__JS_TESTS__", " ".join(js_tests)
        ).replace("__MVN_FLAGS__", _MVN_COMMON_FLAGS)

        prepare = """#!/bin/bash
set -e
export CI=true
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

source /home/mvn_env.sh

# --- Section 1: pin -------------------------------------------------------
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git rev-parse --verify --quiet "__SHA__^{commit}" > /dev/null 2>&1 \\
  || git fetch --no-tags --quiet origin __SHA__
git checkout --detach __SHA__
bash /home/check_git_changes.sh

# --- Section 2: provision -------------------------------------------------
export KC_MVN_TIMEOUT=28800

./mvnw -B --no-transfer-progress -v

source /home/kc_lanes.sh

# Install (not test) the modules every stage needs, with their upstream:
# the test modules, every module either patch touches, and the servers the
# test lanes boot. `install` packages each module before its dependants run, so
# quarkus/dist's validate-phase unpack finds packaged jars (no MDEP-98).
INSTALL=""
for m in ${TEST_MODULES//,/ } ${PATCH_MODULES//,/ }; do
  if kc_selectable "$m"; then kc_add INSTALL "$m"
  else echo "prepare: '$m' is not a selectable reactor module; skipped"
  fi
done
if [ "$NEW_LANE" = "1" ] || [ "$OLD_LANE" = "1" ]; then
  kc_add INSTALL quarkus/server
  kc_add INSTALL quarkus/dist
fi
[ "$OLD_LANE" = "1" ] && kc_add INSTALL "$KC_AUTH_SERVER"
echo "prepare: test modules   = ${TEST_MODULES:-<none>}"
echo "prepare: install slice  = ${INSTALL:-<none>}"

if [ -n "$INSTALL" ]; then
  timeout --kill-after=60 28800 ./mvnw -B --no-transfer-progress install -DskipTests \\
    -pl "$INSTALL" -am \\
    __MVN_FLAGS__
  # For run_tests.sh: which installed modules the test slice consumes, so a
  # stage can rebuild everything between a changed module and the tests.
  printf '%s\\n' "$INSTALL" > "$KC_INSTALL_FILE"
  kc_reactor_ids -pl "$INSTALL" -am > "$KC_UPSTREAM_FILE"
  echo "prepare: test slice consumes $(grep -c . "$KC_UPSTREAM_FILE") reactor module(s)"
fi

# Surefire resolves its provider (junit-platform / junit4 / junit47) and, for
# JUnit 4 tests on the platform, junit-vintage-engine only when the test run
# sees the matching test classes -- which exist only once test.patch is
# applied. Cache all of them now, strictly, so no stage needs the network.
SUREFIRE_VER=$(sed -n 's:.*<version.surefire.plugin>\\(.*\\)</version.surefire.plugin>.*:\\1:p' pom.xml | head -1)
test -n "$SUREFIRE_VER"
for p in surefire-junit-platform surefire-junit4 surefire-junit47; do
  echo "prepare: caching surefire provider $p:$SUREFIRE_VER"
  ./mvnw -B --no-transfer-progress dependency:get \\
    -Dartifact=org.apache.maven.surefire:$p:$SUREFIRE_VER
done
for v in $(ls "$HOME"/.m2/repository/org/junit/jupiter/junit-jupiter-engine/ 2>/dev/null); do
  echo "prepare: caching junit-vintage-engine:$v"
  ./mvnw -B --no-transfer-progress dependency:get \\
    -Dartifact=org.junit.vintage:junit-vintage-engine:$v
done

# Warm-up stage run: resolves test-scope plugins/providers and unpacks the
# distribution once. Tolerated -- the gate below re-checks what matters.
bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -40 /home/prepare-warmup.log || true
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true
git reset --hard

# --- Section 3: gate ------------------------------------------------------
if [ -n "$TEST_MODULES" ]; then
  ./mvnw -B -o --no-transfer-progress test-compile -pl "$TEST_MODULES" __MVN_FLAGS__
  if [ "$NEW_LANE" = "1" ] || [ "$OLD_LANE" = "1" ]; then
    ls "$HOME"/.m2/repository/org/keycloak/keycloak-quarkus-dist/*/keycloak-quarkus-dist-*.zip > /dev/null
  fi
else
  echo "prepare: no java test module in the test patch; gating on the aggregator only"
  ./mvnw -B -o --no-transfer-progress -N validate
fi

echo "prepare: DEPS_OK"
""".replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha).replace(
            "__MVN_FLAGS__", _MVN_COMMON_FLAGS
        )

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
            File(".", "kc_lanes.sh", kc_lanes),
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

        repo = self.pr.repo
        # Same history hardening the harness applies everywhere, minus
        # `--aggressive` (matches the reference PR image; an aggressive repack
        # of a large Maven monorepo costs a lot of time and memory for no gain).
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        dockerfile = f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{repo}

ARG TARGETARCH

RUN set -eux; \\
    arch="${{TARGETARCH:-}}"; \\
    if [ -z "$arch" ]; then arch="$(dpkg --print-architecture)"; fi; \\
    case "$arch" in \\
      amd64) node_arch=x64 ;; \\
      arm64) node_arch=arm64 ;; \\
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/node.tar.xz \\
      "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{node_arch}}.tar.xz"; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \\
      --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \\
    rm -f /tmp/node.tar.xz; \\
    node --version; \\
    corepack enable; \\
    command -v pnpm

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""
        return re.sub(r"\n{3,}", "\n\n", dockerfile)


@Instance.register("keycloak", "keycloak_54248_to_50787")
class KEYCLOAK_54248_TO_50787(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Keycloak54248To50787ImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        marker = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
        chunks = marker.split(clean)[1:]

        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')
        module_re = re.compile(r"(?:\./)?(.*?)/target/surefire-reports/")

        for report_path, body in zip(chunks[0::2], chunks[1::2]):
            m = module_re.match(report_path)
            module = m.group(1) if m else ""

            for tc in testcase_re.finditer(body):
                attrs = tc.group(1) or ""
                closing = tc.group(2)
                inner = tc.group(3) or ""

                nm = name_re.search(attrs)
                cn = classname_re.search(attrs)
                if not nm or not cn:
                    continue

                # Keep the nested-class suffix: @Nested classes reuse method
                # names, and dropping `$Inner` would merge distinct tests.
                outer, _, nested = cn.group(1).partition("$")
                rel = outer.replace(".", "/") + ".java"
                source_file = (
                    f"{module}/src/test/java/{rel}" if module else f"src/test/java/{rel}"
                )
                method = f"{nested}.{nm.group(1)}" if nested else nm.group(1)

                test_id = f"{source_file}::{method}"

                if closing == "/>":
                    passed_tests.add(test_id)
                elif "<failure" in inner or "<error" in inner:
                    failed_tests.add(test_id)
                elif "<skipped" in inner:
                    skipped_tests.add(test_id)
                else:
                    passed_tests.add(test_id)

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
