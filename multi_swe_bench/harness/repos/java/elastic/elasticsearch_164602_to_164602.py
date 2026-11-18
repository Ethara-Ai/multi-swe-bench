import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

JDK_IMAGE = "eclipse-temurin:21-jdk-jammy"
BASE_TAG = "base-164602_to_164602"
USER = "msb"

_ENV = """\
export CI=false
export TERM=dumb
export RUNTIME_JAVA_HOME="$JAVA_HOME"
export GRADLE_OPTS="-Djava.net.preferIPv4Stack=true"
"""

_AS_USER = f"""\
if [ "$(id -u)" = "0" ]; then
  find /home/REPO \\! -user {USER} -exec chown -h {USER}:{USER} {{}} +
  exec runuser -u {USER} -- env HOME=/home/{USER} bash "$0" "$@"
fi
"""

_WRAPPER_DIST = """\
DIST_URL="$(sed -n 's/^distributionUrl=//p' gradle/wrapper/gradle-wrapper.properties | sed 's#\\\\:#:#g')"
DIST_SHA="$(sed -n 's/^distributionSha256Sum=//p' gradle/wrapper/gradle-wrapper.properties)"
DIST_ZIP="$(basename "$DIST_URL")"
DIST_HASH="$(python3 - "$DIST_URL" <<'PY'
import hashlib
import sys

n = int(hashlib.md5(sys.argv[1].encode()).hexdigest(), 16)
digits = "0123456789abcdefghijklmnopqrstuvwxyz"
out = ""
while n:
    n, r = divmod(n, 36)
    out = digits[r] + out
print(out or "0")
PY
)"
DIST_DIR="$HOME/.gradle/wrapper/dists/${DIST_ZIP%.zip}/$DIST_HASH"
if [ ! -f "$DIST_DIR/$DIST_ZIP.ok" ]; then
  mkdir -p "$DIST_DIR"
  curl -fsSL --retry 5 --retry-all-errors --connect-timeout 20 -o "$DIST_DIR/$DIST_ZIP" "$DIST_URL"
  if [ -n "$DIST_SHA" ]; then echo "$DIST_SHA  $DIST_DIR/$DIST_ZIP" | sha256sum -c -; fi
  unzip -q -o "$DIST_DIR/$DIST_ZIP" -d "$DIST_DIR"
  touch "$DIST_DIR/$DIST_ZIP.ok"
fi"""

_GRADLE_FLAGS = (
    "--console=plain --continue --no-scan -Dtests.seed=DEADBEEFCAFEBABE -Dtests.jvms=2 "
    '-Porg.elasticsearch.build.branches-file-location="$PWD/branches.json"'
)

_GRADLE_PROPS = """\
mkdir -p "$HOME/.gradle"
JVMARGS="$(sed -n 's/^org.gradle.jvmargs=//p' gradle.properties)"
printf 'org.gradle.jvmargs=-Xmx3g -Djava.net.preferIPv4Stack=true %s\norg.gradle.workers.max=3\n' "$JVMARGS" > "$HOME/.gradle/gradle.properties\""""

_EMIT_RESULTS = """\
python3 - "${MODS[@]}" <<'PY'
import glob
import os
import sys
import xml.etree.ElementTree as ET

for mod in sys.argv[1:]:
    pattern = os.path.join(mod, "build", "test-results", "test", "TEST-*.xml")
    for path in sorted(glob.glob(pattern)):
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        for case in root.iter("testcase"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            tags = {child.tag for child in case}
            if tags & {"failure", "error"}:
                status = "FAIL"
            elif "skipped" in tags:
                status = "SKIP"
            else:
                status = "PASS"
            print(f"MSB_TEST|{status}|{name}", flush=True)
PY"""

_TEST_SOURCE = re.compile(r"^(.+?)/src/test/(?:java|kotlin|groovy|resources)/.+$")


def _patch_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        m = re.match(r"^(?:\+\+\+ b|--- a)/(.+?)\t?$", line)
        if m and m.group(1) not in paths:
            paths.append(m.group(1))
    return paths


def _modules(test_patch: str) -> list[str]:
    modules = set()
    for path in _patch_paths(test_patch):
        m = _TEST_SOURCE.match(path)
        if m:
            modules.add(m.group(1))
    return sorted(modules)


def _q(value: str) -> str:
    return shlex.quote(value)


def _module_array(test_patch: str) -> str:
    lines = ["MODS=()"]
    for module in _modules(test_patch):
        lines.append(f"if [ -f {_q(module + '/build.gradle')} ]; then MODS+=({_q(module)}); fi")
    return "\n".join(lines)


_TASKS = """\
TASKS=()
for m in "${MODS[@]}"; do TASKS+=(":${m//\\//:}:$1"); done"""


def _tasks(goal: str) -> str:
    return _TASKS.replace("$1", goal)


def _prepare_cmd(test_patch: str) -> str:
    return f"""\
{_GRADLE_PROPS}
{_module_array(test_patch)}
test "${{#MODS[@]}}" -gt 0
{_tasks("testClasses")}
./gradlew {_GRADLE_FLAGS} "${{TASKS[@]}}"
{_tasks("test")}
DRY_RUN="$(./gradlew {_GRADLE_FLAGS} "${{TASKS[@]}}" --dry-run)"
NATIVE=()
for t in $(echo "$DRY_RUN" | grep -oE '^:[^ ]+:buildNativeLibrary'); do NATIVE+=("$t"); done
if [ "${{#NATIVE[@]}}" -gt 0 ]; then
  ./gradlew {_GRADLE_FLAGS} "${{NATIVE[@]}}"
fi
echo DEPS_OK"""


def _test_cmd(test_patch: str) -> str:
    return f"""\
RC=0
{_module_array(test_patch)}
if [ "${{#MODS[@]}}" -gt 0 ]; then
{_tasks("test")}
  for m in "${{MODS[@]}}"; do rm -rf "$m/build/test-results/test"; done
  echo 'MSB_SUITE|gradle'
  ./gradlew {_GRADLE_FLAGS} "${{TASKS[@]}}" 2>&1 || RC=$?
{_EMIT_RESULTS}
fi

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


class Elasticsearch164602To164602ImageBase(Image):
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
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null && \\\n"
                f"    chown -R {USER}:{USER} /home/{self.pr.repo}"
            )
        else:
            code = f"COPY --chown={USER}:{USER} {self.pr.repo} /home/{self.pr.repo}"

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
    RUNTIME_JAVA_HOME=/opt/java/openjdk \\
    GRADLE_OPTS=-Djava.net.preferIPv4Stack=true \\
    TERM=dumb \\
    CI=false

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
        git ca-certificates curl unzip python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN java -version && javac -version

RUN git config --global --add safe.directory '*'

RUN useradd -m -u 1000 -s /bin/bash {USER}

{code}

CMD ["/bin/bash"]
"""


class Elasticsearch164602To164602ImageDefault(Image):
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
        return Elasticsearch164602To164602ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = _test_cmd(self.pr.test_patch)

        prepare_sh = """\
#!/bin/bash
set -euo pipefail
AS_USER
cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK
java -version
WRAPPER_PROPS
./gradlew --version

PREPARE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
AS_USER
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
AS_USER
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
AS_USER
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("AS_USER\n", _AS_USER)
                .replace("ENV_BLOCK\n", _ENV)
                .replace("WRAPPER_PROPS", _WRAPPER_DIST)
                .replace("TEST_CMD", test_cmd)
                .replace("PREPARE_CMD", _prepare_cmd(self.pr.test_patch))
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
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

RUN git reset --hard
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


@Instance.register("elastic", "elasticsearch_164602_to_164602")
class ELASTICSEARCH_164602_TO_164602(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Elasticsearch164602To164602ImageDefault(self.pr, self._config)

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
