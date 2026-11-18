import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

JDK_IMAGE = "eclipse-temurin:17-jdk-jammy"
NODE_IMAGE = "node:22.12.0-bookworm-slim"
BASE_TAG = "base-1557_to_1557"

_ENV = """\
export CI=true
export TERM=dumb
export NO_COLOR=1
export FORCE_COLOR=0
export HUSKY=0
export NODE_OPTIONS="--max-old-space-size=4096"
export GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.welcome=never -Dorg.gradle.console=plain"
"""

_GRADLE = "./gradlew --no-daemon --console=plain -PskipWebview=true"

_INIT_GRADLE = """\
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
    }
}
"""

_TARGETS = """\
JAVA_TESTS=()
NODE_FILES=()
VITEST_FILES=()
for p in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u); do
  [ -f "$p" ] || continue
  if [[ "$p" =~ ^src/test/java/(.+)\\.java$ ]]; then
    cls="${BASH_REMATCH[1]}"
    base="${cls##*/}"
    case "$base" in
      Test*|*Test|*Tests) JAVA_TESTS+=("${cls//\\//.}") ;;
    esac
  elif [[ "$p" =~ ^ai-bridge/.+\\.test\\.[cm]?js$ ]]; then
    NODE_FILES+=("$p")
  elif [[ "$p" =~ ^webview/src/.+\\.test\\.tsx?$ ]]; then
    VITEST_FILES+=("${p#webview/}")
  fi
done
test $(( ${#JAVA_TESTS[@]} + ${#NODE_FILES[@]} + ${#VITEST_FILES[@]} )) -gt 0"""

_RUN = f"""\
rm -rf /tmp/msb && mkdir -p /tmp/msb
for d in ai-bridge webview; do
  if [ -f "$d/package-lock.json" ] && ! git diff --quiet HEAD -- "$d/package.json" "$d/package-lock.json"; then
    (cd "$d" && npm ci --no-audit --no-fund) 2>&1 || RC=$?
  fi
done
i=0
for f in "${{NODE_FILES[@]}}"; do
  i=$((i+1))
  echo "MSB_SUITE|node-test|$f"
  echo "$f" > "/tmp/msb/node-$i.file"
  node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="/tmp/msb/node-$i.xml" "$f" 2>&1 || RC=$?
done
if [ "${{#VITEST_FILES[@]}}" -gt 0 ]; then
  echo "MSB_SUITE|vitest|webview"
  (cd webview && node scripts/extract-version.mjs && npx --no-install vitest run --reporter=default --reporter=json --outputFile.json=/tmp/msb/vitest.json "${{VITEST_FILES[@]}}") 2>&1 || RC=$?
fi
if [ "${{#JAVA_TESTS[@]}}" -gt 0 ]; then
  echo "MSB_SUITE|gradle"
  rm -rf build/test-results/test
  ARGS=()
  for c in "${{JAVA_TESTS[@]}}"; do ARGS+=(--tests "$c"); done
  {_GRADLE} --init-script /home/msb-init.gradle --continue test "${{ARGS[@]}}" 2>&1 || RC=$?
  mkdir -p /tmp/msb/gradle
  if [ -d build/test-results/test ]; then
    find build/test-results/test -maxdepth 1 -name 'TEST-*.xml' -exec cp {{}} /tmp/msb/gradle/ \\;
  fi
fi
python3 /home/msb_parse.py /tmp/msb"""

_PARSER = """\
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET

root_dir = sys.argv[1]


def emit(status, name):
    print(f"MSB_TEST|{status}|{name}", flush=True)


def case_status(case):
    tags = {x.tag for x in case}
    if tags & {"failure", "error"}:
        return "FAIL"
    if "skipped" in tags:
        return "SKIP"
    return "PASS"


for xf in sorted(glob.glob(os.path.join(root_dir, "node-*.xml"))):
    label_path = xf[:-4] + ".file"
    label = open(label_path).read().strip() if os.path.exists(label_path) else os.path.basename(xf)
    try:
        tree = ET.parse(xf).getroot()
    except Exception:
        emit("FAIL", label)
        continue

    def walk(node, prefix):
        for child in node:
            if child.tag == "testsuite":
                walk(child, prefix + [child.get("name", "")])
            elif child.tag == "testcase":
                emit(case_status(child), f"{label}::" + " > ".join(prefix + [child.get("name", "")]))

    walk(tree, [])

vf = os.path.join(root_dir, "vitest.json")
if os.path.exists(vf):
    status_map = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "pending": "SKIP", "todo": "SKIP", "disabled": "SKIP"}
    try:
        data = json.load(open(vf, encoding="utf-8"))
    except Exception:
        data = {}
    for f in data.get("testResults", []):
        rel = os.path.relpath(f.get("name", ""), os.getcwd()).replace(os.sep, "/")
        tests = f.get("assertionResults") or []
        if not tests:
            if f.get("status") == "failed":
                emit("FAIL", rel)
            continue
        for t in tests:
            name = " > ".join(list(t.get("ancestorTitles") or []) + [t.get("title", "")])
            emit(status_map.get(t.get("status"), "FAIL"), f"{rel}::{name}")

for xf in sorted(glob.glob(os.path.join(root_dir, "gradle", "TEST-*.xml"))):
    try:
        tree = ET.parse(xf).getroot()
    except Exception:
        continue
    for case in tree.iter("testcase"):
        emit(case_status(case), f"java::{case.get('classname', '')}::{case.get('name', '')}")
"""

_PREPARE_CMD = f"""\
java -version
node --version
npm --version
(cd ai-bridge && npm ci --no-audit --no-fund)
(cd webview && npm ci --no-audit --no-fund)
(cd webview && node scripts/extract-version.mjs)
{_GRADLE} --version
{_GRADLE} testClasses
test -d build/classes/java/test
(cd webview && npx --no-install vitest --version)
node -e "require.resolve('sql.js', {{ paths: ['ai-bridge'] }})"
echo DEPS_OK"""

_TEST_CMD = f"""\
RC=0
{_TARGETS}
{_RUN}
exit $RC"""


_CHECK_GIT_CHANGES = """\
#!/bin/bash
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


class JetbrainsCcGui1557To1557ImageBase(Image):
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
        return JDK_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.welcome=never" \\
    TERM=dumb \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl zip unzip python3 bash \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    java -version && node --version && npm --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class JetbrainsCcGui1557To1557ImageDefault(Image):
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
        return JetbrainsCcGui1557To1557ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git config core.ignorecase false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK

PREPARE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_CMD", _TEST_CMD)
                .replace("PREPARE_CMD", _PREPARE_CMD)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_parse.py", _PARSER),
            File(".", "msb-init.gradle", _INIT_GRADLE),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git config core.ignorecase false && git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
    git -c pack.threads=2 -c pack.windowMemory=512m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("zhukunpenglinyutong", "jetbrains_cc_gui_1557_to_1557")
class JETBRAINS_CC_GUI_1557_TO_1557(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return JetbrainsCcGui1557To1557ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }

        re_test = re.compile(r"^MSB_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")

        for line in log.splitlines():
            m = re_test.match(line.strip())
            if m:
                buckets[m.group(1)].add(m.group(2))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
