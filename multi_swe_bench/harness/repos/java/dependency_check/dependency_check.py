import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_VERSION = "24.22.2"

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

def _test_classes(test_patch: str) -> tuple[list[str], list[str]]:
    """Simple class names of the `src/test` java files the test patch touches,
    split into the surefire set and the failsafe set.

    ODC runs `*IT` through failsafe and everything else through surefire, and the
    two plugins take their selection from different properties (`-Dtest` vs
    `-Dit.test`). A class handed to the wrong one is simply never run, so the
    split happens here, off the file names, rather than being guessed at run time.
    """
    unit: set[str] = set()
    integration: set[str] = set()
    for path in _patched_paths(test_patch):
        if "/src/test/" not in path or not path.endswith(".java"):
            continue
        cls = path.rsplit("/", 1)[1][:-5]
        if cls.endswith(("IT", "ITCase")) or cls.startswith("IT"):
            integration.add(cls)
        else:
            unit.add(cls)
    return sorted(unit), sorted(integration)

_NO_UNIT_TESTS = "OdcNoUnitTestSelected"

_MVN_COMMON_FLAGS = (
    "-Dcheckstyle.skip=true -Dspotbugs.skip=true "
    "-Dmaven.javadoc.skip=true -Dgpg.skip=true"
)


class DependencyCheckImageBase(Image):
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
        return "maven:3.9.12-eclipse-temurin-25"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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
    git curl ca-certificates xz-utils unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""
        return re.sub(r"\n{3,}", "\n\n", dockerfile)

class DependencyCheckImageDefault(Image):
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
        return DependencyCheckImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        unit, integration = _test_classes(self.pr.test_patch)
        if unit:
            dtest = "-Dtest=" + ",".join(unit)
        elif integration:
            # Only *IT classes are patched: give surefire a pattern that matches
            # nothing, or it runs the whole unit suite of every selected module.
            dtest = "-Dtest=" + _NO_UNIT_TESTS
        else:
            dtest = ""
        dittest = ("-Dit.test=" + ",".join(integration)) if integration else ""
        patched_paths = "\n".join(
            _patched_paths(self.pr.fix_patch, self.pr.test_patch)
        )

        mvn_env = """#!/bin/bash
export MAVEN_OPTS="-Xmx2g -Dfile.encoding=UTF-8"

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

        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

source /home/mvn_env.sh

MVN_TIMEOUT="${ODC_MVN_TIMEOUT:-5400}"

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
  if [ "$d" != "." ] && [ "$d" != "/" ]; then add_mod "$d"; fi
done <<'ODC_PATCHED_PATHS'
__PATCHED_PATHS__
ODC_PATCHED_PATHS

if [ -n "$MODULES" ]; then
  PL="-pl $MODULES -am"
  echo "ODC RUNNER: reactor = -pl $MODULES -am"
else
  PL=""
  echo "ODC RUNNER: no Maven module could be resolved from the patched paths;"
  echo "ODC RUNNER: falling back to the FULL reactor"
fi

DTEST="__DTEST__"
DITTEST="__DITTEST__"
if [ -n "$DTEST$DITTEST" ]; then
  if [ "$DTEST" = "-Dtest=__NO_UNIT__" ]; then
    echo "ODC RUNNER: surefire classes: none (the test patch touches only *IT classes)"
  elif [ -n "$DTEST" ]; then
    echo "ODC RUNNER: surefire classes: ${DTEST#-Dtest=}"
  fi
  [ -n "$DITTEST" ] && echo "ODC RUNNER: failsafe classes: ${DITTEST#-Dit.test=}"
  echo "ODC RUNNER: NOT RUN: every other test class in the selected modules."
  echo "ODC RUNNER: NOT RUN: reason - core's full suite drives external toolchains"
  echo "ODC RUNNER: NOT RUN: this PR does not touch (dotnet, Go, Ruby"
  echo "ODC RUNNER: NOT RUN: bundler-audit) and downloads NVD data; running it"
  echo "ODC RUNNER: NOT RUN: would add hours per stage and a pile of"
  echo "ODC RUNNER: NOT RUN: environment-dependent noise to all three logs."
else
  echo "ODC RUNNER: the test patch names no test class; running EVERY test in the"
  echo "ODC RUNNER: selected modules. This can take hours."
fi

find . -type d \\( -name surefire-reports -o -name failsafe-reports \\) -prune \\
  -exec rm -rf {} + 2>/dev/null

MVN_LOG=/home/mvn-stage.log
rc=0
timeout --kill-after=60 "$MVN_TIMEOUT" \\
  mvn -B --no-transfer-progress -fae integration-test \\
    $PL $DTEST $DITTEST \\
    -DfailIfNoTests=false \\
    -Dsurefire.failIfNoSpecifiedTests=false \\
    -Dfailsafe.failIfNoSpecifiedTests=false \\
    -Dmaven.test.failure.ignore=true \\
    -Dsurefire.timeout=1800 \\
    __MVN_FLAGS__ 2>&1 | tee "$MVN_LOG"
rc=${PIPESTATUS[0]}

echo '===== BEGIN TEST RESULTS ====='
find . -path '*/target/surefire-reports/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
      echo "##### FILE: ${f#./}"
      cat "$f"
      echo
    done
find . -path '*/target/failsafe-reports/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
      echo "##### FILE: ${f#./}"
      cat "$f"
      echo
    done
echo '===== END TEST RESULTS ====='

if [ "$rc" -ne 0 ]; then
  echo "ODC RUNNER: maven exited $rc"
  if grep -qE "Could not resolve dependencies|Non-resolvable|Could not transfer|Connection refused|Failed to read artifact descriptor|Plugin .* or one of its dependencies could not be resolved" "$MVN_LOG"; then
    echo "ODC RUNNER: INFRASTRUCTURE FAILURE: dependency resolution failed;"
    echo "ODC RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "ODC RUNNER: INFRASTRUCTURE FAILURE: the run hit the ${MVN_TIMEOUT}s cap;"
    echo "ODC RUNNER: the results above are not trustworthy"
    exit "$rc"
  fi
  echo "ODC RUNNER: treating this as a test/compile failure, which is a result"
fi
exit 0
""".replace("__REPO__", repo).replace("__DTEST__", dtest).replace(
            "__DITTEST__", dittest
        ).replace("__NO_UNIT__", _NO_UNIT_TESTS).replace("__PATCHED_PATHS__", patched_paths).replace(
            "__MVN_FLAGS__", _MVN_COMMON_FLAGS
        )

        prepare = """#!/bin/bash
set -e
export CI=true
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

source /home/mvn_env.sh

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git rev-parse --verify --quiet "__SHA__^{commit}" > /dev/null 2>&1 \
  || git fetch --no-tags --quiet origin __SHA__
git checkout --detach __SHA__
bash /home/check_git_changes.sh

export ODC_MVN_TIMEOUT=14400

find core/src/test/resources -name package.json \\
    -exec grep -ho '"packageManager"[[:space:]]*:[[:space:]]*"[^"]*"' {} + 2>/dev/null \\
  | sed -E 's/.*:[[:space:]]*"([^"]*)".*/\\1/' | sort -u \\
  | while IFS= read -r pm; do
      [ -n "$pm" ] || continue
      echo "prepare: corepack prepare $pm"
      corepack prepare "$pm" || echo "prepare: corepack could not fetch $pm (may be intentional)"
    done

timeout --kill-after=60 14400 mvn -B --no-transfer-progress -fae install -DskipTests \\
  -pl core -am \\
  __MVN_FLAGS__ || true

bash /home/run_tests.sh > /home/prepare-warmup.log 2>&1 || true
tail -40 /home/prepare-warmup.log || true

find . -type d \\( -name surefire-reports -o -name failsafe-reports \\) -prune \\
  -exec rm -rf {} + 2>/dev/null || true

git reset --hard

mvn -B -o --no-transfer-progress test-compile \\
  -pl core -am \\
  __MVN_FLAGS__

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

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

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
    command -v yarn

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""
        return re.sub(r"\n{3,}", "\n\n", dockerfile)

@Instance.register("dependency-check", "DependencyCheck")
class DependencyCheck(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DependencyCheckImageDefault(self.pr, self._config)

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

        marker = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
        chunks = marker.split(clean)[1:]

        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')
        module_re = re.compile(r"(?:\./)?(.*?)/target/(?:surefire|failsafe)-reports/")

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

                rel = cn.group(1).split("$", 1)[0].replace(".", "/") + ".java"
                source_file = (
                    f"{module}/src/test/java/{rel}" if module else f"src/test/java/{rel}"
                )

                test_id = f"{source_file}::{nm.group(1)}"

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
