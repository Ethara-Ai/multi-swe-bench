import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RE_PATCH_BLOCK = re.compile(
    r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)",
    re.S | re.M,
)
_RE_BUILD_FAIL = re.compile(r"ninja: build stopped|^FAILED: ", re.M)
_RE_ADDED_TEST = re.compile(r"^\+\s*TEST(?:_F|_P)?\(\s*(\w+)\s*,\s*(\w+)\s*\)", re.M)

class RstudioImageBase(Image):
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
        return "ubuntu:24.04"

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

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    CI=true

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y build-essential cmake ninja-build git curl wget sudo lsb-release ca-certificates bzip2 unzip pkg-config python3 libacl1-dev libattr1-dev libbz2-dev libcap-dev libcurl4-openssl-dev libpam-dev libpq-dev libsqlite3-dev libssl-dev libuv1-dev libxslt1-dev uuid-dev zlib1g-dev r-base-dev && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class RstudioImageDefault(Image):
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
        return RstudioImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        build_cmd = 'su mswe -c "ninja -C /home/rs-build rsession rstudio-core-tests" 2>&1'
        test_cmd = 'su mswe -c "cd /home/rstudio && bash /home/gtests.sh"'
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
            File(
                ".",
                "gtests.sh",
                """#!/bin/bash
cd /home/{pr.repo}
FILES=$(grep -E '^diff --git a/' /home/test.patch | sed -E 's#^diff --git a/([^ ]+) b/.*#\\1#' | grep -E '^src/cpp/.+Tests?\\.cpp$' | sort -u)
SUITES=""
SCOPES=""
for f in $FILES; do
  [ -f "$f" ] || continue
  SUITES="$SUITES $(grep -ohE '^[[:space:]]*TEST(_F|_P)?\\([[:space:]]*[A-Za-z0-9_]+' "$f" | sed -E 's/.*\\([[:space:]]*//')"
  case "$f" in
    src/cpp/session/*|src/cpp/r/*) SCOPES="$SCOPES rsession" ;;
    src/cpp/server/*) SCOPES="$SCOPES rserver" ;;
    *) SCOPES="$SCOPES core" ;;
  esac
done
if [ -z "$SCOPES" ]; then
  SUITES=""
  for f in $(grep -E '^diff --git a/' /home/test.patch | sed -E 's#^diff --git a/([^ ]+) b/.*#\\1#' | sort -u); do
    case "$f" in
      src/cpp/session/*|src/cpp/r/*) SCOPES="$SCOPES rsession" ;;
      src/cpp/server/*) SCOPES="$SCOPES rserver" ;;
      src/cpp/*) SCOPES="$SCOPES core" ;;
    esac
  done
fi
if [ -z "$SCOPES" ]; then
  SCOPES="rsession"
fi
FILTER=$(echo $SUITES | tr ' ' '\\n' | sort -u | sed 's/$/.*/' | paste -sd: -)
if [ -n "$FILTER" ]; then
  export GTEST_FILTER="$FILTER"
fi
cd /home/rs-build
rc=0
for scope in $(echo $SCOPES | tr ' ' '\\n' | sort -u); do
  echo ">>> rstudio-tests --scope $scope"
  ./rstudio-tests --scope "$scope" 2>&1 || rc=$?
done
exit $rc
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch origin {pr.base.sha}
git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
bash /home/check_git_changes.sh
bash dependencies/common/install-boost || true
bash dependencies/common/install-soci || true
TOOLS=/opt/rstudio-tools/$(uname -m)
mkdir -p $TOOLS/dictionaries $TOOLS/mathjax-27 $TOOLS/mathjax-4 $TOOLS/quarto/bin/tools $TOOLS/copilot-language-server-js
LUA_DIR=/opt/rstudio-tools/src/gwt/lib/quarto/packages/editor-server/src/resources
mkdir -p $LUA_DIR
[ -f $LUA_DIR/md-writer.lua ] || touch $LUA_DIR/md-writer.lua
rm -rf /tmp/rstudio-build /home/rs-build
cmake -S src/cpp -B /home/rs-build -G Ninja -DRSTUDIO_TARGET=Server -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS_DEBUG="-O0 -g0" -DCMAKE_C_FLAGS_DEBUG="-O0 -g0" -DRSTUDIO_USE_CCACHE=OFF
ninja -C /home/rs-build rsession rstudio-core-tests
id mswe >/dev/null 2>&1 || useradd -m mswe
chown -R mswe /home/rs-build /home/rstudio
test -x /home/rs-build/rstudio-tests
test -x /home/rs-build/core/rstudio-core-tests
echo DEPS_OK
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
{build_cmd}
{test_cmd}
""".format(pr=self.pr, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
echo "MSWE_TEST_PATCH_BEGIN"
cat /home/test.patch
echo "MSWE_TEST_PATCH_END"
git apply --whitespace=nowarn /home/test.patch
{build_cmd}
{test_cmd}
""".format(pr=self.pr, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_cmd}
{test_cmd}
""".format(pr=self.pr, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("rstudio", "rstudio")
@Instance.register("rstudio", "rstudio_19363_to_19363")
class RSTUDIO_19363_TO_19363(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RstudioImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        m = _RE_PATCH_BLOCK.search(clean_log)
        block = m.group(1) if m else ""
        if m:
            clean_log = clean_log[: m.start()] + clean_log[m.end():]
        re_line = re.compile(r"^\[\s*(OK|FAILED|SKIPPED)\s*\] (\w+\.\w+)(?: \(\d+ ms\))?$")

        for line in clean_log.splitlines():
            match = re_line.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "OK":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        if _RE_BUILD_FAIL.search(clean_log):
            for m in _RE_ADDED_TEST.finditer(self.pr.test_patch or block):
                failed_tests.add(f"{m.group(1)}.{m.group(2)}")

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
