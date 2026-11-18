import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

JDK_IMAGE = "eclipse-temurin:21-jdk-noble"
BASE_TAG = "base-1454_to_1454"
ROOT_DIR = "/home/supertokens-root"

_ENV = """\
export CI=true
export TERM=dumb
export GRADLE_USER_HOME=/root/.gradle
export GRADLE_OPTS="-Dorg.gradle.daemon=false"
export ST_PLUGIN_NAME=sqlite
"""

_GRADLE = "./gradlew --no-daemon --console=plain --max-workers=4"

_INIT = """\
mkdir -p "$GRADLE_USER_HOME/init.d"
echo "org.gradle.jvmargs=-Xmx3g" > "$GRADLE_USER_HOME/gradle.properties"
cat > "$GRADLE_USER_HOME/init.d/msb.gradle" <<'EOF'
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
        reports.junitXml.required = true
    }
}
EOF"""

_LAYOUT = """\
REPO_LINK=/home/REPO_NAME
CORE_DIR=ROOT_DIR/supertokens-core
if [ ! -L "$REPO_LINK" ]; then
CORE_DATE=$(git log -1 --format=%cI HEAD)
rm -rf ROOT_DIR
git clone --filter=blob:none https://github.com/supertokens/supertokens-root.git ROOT_DIR
ROOT_SHA=$(git -C ROOT_DIR rev-list -1 --before="$CORE_DATE" origin/master)
test -n "$ROOT_SHA"
git -C ROOT_DIR checkout --detach "$ROOT_SHA"
git clone --filter=blob:none https://github.com/supertokens/supertokens-plugin-interface.git ROOT_DIR/supertokens-plugin-interface
PI_SHA=$(git -C ROOT_DIR/supertokens-plugin-interface rev-list -1 --before="$CORE_DATE" origin/master)
test -n "$PI_SHA"
git -C ROOT_DIR/supertokens-plugin-interface checkout --detach "$PI_SHA"
rm -rf ROOT_DIR/.git ROOT_DIR/supertokens-plugin-interface/.git
cd /home
mv "$REPO_LINK" "$CORE_DIR"
ln -s "$CORE_DIR" "$REPO_LINK"
cd ROOT_DIR
printf 'core,master\\nplugin-interface,master\\nsqlite-plugin,master' > modules.txt
cat > settings.gradle <<'EOF'
include 'supertokens-core:cli'
include 'supertokens-core:downloader'
include 'supertokens-core:ee'
include 'supertokens-core'
include 'supertokens-plugin-interface'
EOF
fi
cd ROOT_DIR"""

_TESTS = """\
MODS=()
ARGS=()
LAST=""
for e in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed -n 's#^b/\\(\\(.*\\)/\\)\\{0,1\\}src/test/java/\\(.*Test[s]*\\)\\.java$#\\2|\\3#p' | sort -u); do
  m="${e%%|*}"
  c="${e#*|}"
  if [ -n "$m" ]; then
    dir="supertokens-core/$m"
    proj=":supertokens-core:${m//\\//:}"
  else
    dir="supertokens-core"
    proj=":supertokens-core"
  fi
  if [ -f "$dir/build.gradle" ] && [ -f "$dir/src/test/java/$c.java" ]; then
    if [ "$dir" != "$LAST" ]; then
      MODS+=("$dir")
      ARGS+=("$proj:test")
      LAST="$dir"
    fi
    ARGS+=("--tests" "${c//\\//.}")
  fi
done"""

_EMIT_RESULTS = """\
python3 - "${MODS[@]}" <<'PY'
import glob
import sys
import xml.etree.ElementTree as ET

for module in sys.argv[1:]:
    for path in sorted(glob.glob(f"{module}/build/test-results/test/TEST-*.xml")):
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

_PREPARE_CMD = f"""\
{_INIT}
{_LAYOUT}
if ! command -v docker > /dev/null; then
printf '#!/bin/sh\\necho "docker: not available in this environment" >&2\\nexit 1\\n' > /usr/local/bin/docker
chmod +x /usr/local/bin/docker
fi
command -v docker > /dev/null
./utils/setupTestEnv --local
{_GRADLE} :supertokens-core:testClasses
test -f .testEnvRunning
test -f version.yaml
test -f temp/config.yaml
ls core/*.jar > /dev/null
ls plugin-interface/*.jar > /dev/null
ls -d supertokens-core/build/classes/*/test > /dev/null
echo DEPS_OK"""

_TEST_CMD = f"""\
RC=0
cd ROOT_DIR
{_TESTS}
if [ "${{#MODS[@]}}" -eq 0 ]; then
  echo 'MSB_SUITE|none'
  exit 0
fi
for m in "${{MODS[@]}}"; do rm -rf "$m/build/test-results/test"; done
echo 'MSB_SUITE|gradle'
{_GRADLE} "${{ARGS[@]}}" --continue 2>&1 || RC=$?
{_EMIT_RESULTS}
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


class SupertokensCore1454To1454ImageBase(Image):
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
    GRADLE_USER_HOME=/root/.gradle \\
    GRADLE_OPTS="-Dorg.gradle.daemon=false" \\
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
        git ca-certificates curl unzip python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN java -version && javac -version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class SupertokensCore1454To1454ImageDefault(Image):
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
        return SupertokensCore1454To1454ImageBase(self.pr, self._config)

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
java -version

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
                .replace("ROOT_DIR", ROOT_DIR)
                .replace("REPO_NAME", repo)
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


@Instance.register("supertokens", "supertokens_core_1454_to_1454")
class SUPERTOKENS_CORE_1454_TO_1454(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SupertokensCore1454To1454ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", test_log)

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
