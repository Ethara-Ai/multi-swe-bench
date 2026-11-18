import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22-bookworm"
BASE_TAG = "base-26884_to_27847"

_ENV = """\
export CI=true
export HUSKY=0
export NX_DAEMON=false
export NX_NO_CLOUD=true
export NX_TUI=false
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_GLOBAL_CACHE=1
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export NODE_OPTIONS=--max-old-space-size=6144
"""

_SELECT = """\
ROOT=$(pwd)
declare -A TGROUPS=()
VITEST=()
for f in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u); do
  [ -f "$f" ] || continue
  case "$f" in
    */node_modules/*|*/dist/*|*/__tests__/resources/*|*/tests/resources/*|*.d.ts|*.testdata.*|*.test.utils.*) continue ;;
  esac
  if ! grep -qE '\\.(test|spec)\\.[cm]?[jt]sx?$' <<< "$f"; then
    if ! grep -qE '/__tests__/.*\\.[jt]sx?$' <<< "$f"; then
      continue
    fi
  fi
  if [[ "$f" == *.vitest.test.* ]]; then
    VITEST+=("$f")
    continue
  fi
  want=jest.config.js
  if [[ "$f" != */__tests__/* && "$f" == */tests/* ]]; then
    want=jest.config.front.js
  fi
  cfg=""
  for name in "$want" jest.config.js; do
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
      if [ -f "$d/$name" ]; then
        cfg="$d/$name"
        break
      fi
      d=$(dirname "$d")
    done
    [ -n "$cfg" ] && break
  done
  [ -n "$cfg" ] || continue
  TGROUPS["$cfg"]+=" $f"
done"""

_PRE_TEST = """\
CHANGED=$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u )
if grep -qE '(^|/)(package\\.json|yarn\\.lock)$' <<< "$CHANGED"; then
  yarn install || echo 'MSB_INSTALL_FAILED'
fi
SRC=$(grep -E '^packages/' <<< "$CHANGED" | grep -vE '(/__tests__/|/tests/|\\.test\\.|\\.spec\\.)' || true)
if [ -n "$SRC" ]; then
  yarn build --parallel=2 || echo 'MSB_BUILD_FAILED'
fi"""

_EMIT_RESULTS = """\
python3 - "$ROOT" /tmp/msb-results <<'PY'
import glob
import json
import os
import sys

root = sys.argv[1]
status_map = {"passed": "PASS", "failed": "FAIL", "pending": "SKIP", "skipped": "SKIP", "todo": "SKIP", "disabled": "SKIP"}
for path in sorted(glob.glob(os.path.join(sys.argv[2], "*.json"))):
    try:
        data = json.load(open(path))
    except Exception:
        continue
    for suite in data.get("testResults", []):
        rel = os.path.relpath(suite.get("name", ""), root)
        cases = suite.get("assertionResults", [])
        if not cases:
            if suite.get("status") == "failed":
                print(f"MSB_TEST|FAIL|{rel}", flush=True)
            continue
        for case in cases:
            title = " > ".join(list(case.get("ancestorTitles") or []) + [case.get("title", "")])
            status = status_map.get(case.get("status"), "FAIL")
            print(f"MSB_TEST|{status}|{rel}::{title}", flush=True)
PY"""

_TEST_CMD = f"""\
RC=0
{_PRE_TEST}
{_SELECT}
if [ "${{#TGROUPS[@]}}" -eq 0 ] && [ "${{#VITEST[@]}}" -eq 0 ]; then
  echo 'MSB_SUITE|none'
  exit 0
fi
rm -rf /tmp/msb-results
mkdir -p /tmp/msb-results
i=0
for cfg in "${{!TGROUPS[@]}}"; do
  i=$((i+1))
  FILES=()
  for f in ${{TGROUPS[$cfg]}}; do
    FILES+=("$ROOT/$f")
  done
  echo "MSB_SUITE|jest|$cfg"
  EE=false
  if [ "$(basename "$cfg")" = "jest.config.front.js" ]; then EE=true; fi
  (cd "$(dirname "$cfg")" && IS_EE=$EE yarn run -T jest --config "./$(basename "$cfg")" --ci --maxWorkers=2 --json --outputFile="/tmp/msb-results/jest-$i.json" --runTestsByPath "${{FILES[@]}}") 2>&1 || RC=$?
done
if [ "${{#VITEST[@]}}" -gt 0 ]; then
  echo 'MSB_SUITE|vitest'
  yarn vitest run --reporter=default --reporter=json --outputFile=/tmp/msb-results/vitest.json "${{VITEST[@]}}" 2>&1 || RC=$?
fi
{_EMIT_RESULTS}
exit $RC"""

_EXCLUDES = """\
EXCL=()
for p in $(grep -hE '^Binary files .* differ$' PATCH_FILES | sed -E 's#^Binary files .* and b/(.*) differ$#\\1#' | sort -u); do
  EXCL+=("--exclude=$p")
done"""

_PREPARE_CMD = """\
corepack enable
yarn --version
yarn install
yarn build --parallel=2
node -e "require('fs').accessSync(require.resolve('@strapi/utils'))"
node -e "require('fs').accessSync(require.resolve('@strapi/admin-test-utils'))"
yarn run -T jest --version
yarn vitest --version
echo DEPS_OK"""


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


class Strapi26884To27847ImageBase(Image):
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
        return NODE_IMAGE

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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_GLOBAL_CACHE=1 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    HUSKY=0 \\
    NX_DAEMON=false \\
    NX_NO_CLOUD=true \\
    NX_TUI=false \\
    FORCE_COLOR=0 \\
    NODE_OPTIONS=--max-old-space-size=6144 \\
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
        git ca-certificates python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class Strapi26884To27847ImageDefault(Image):
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
        return Strapi26884To27847ImageBase(self.pr, self._config)

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
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK
node --version

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
TEST_EXCLUDES
git apply --whitespace=nowarn "${EXCL[@]}" /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
FIX_EXCLUDES
git apply --whitespace=nowarn "${EXCL[@]}" /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_EXCLUDES", _EXCLUDES.replace("PATCH_FILES", "/home/test.patch"))
                .replace("FIX_EXCLUDES", _EXCLUDES.replace("PATCH_FILES", "/home/test.patch /home/fix.patch"))
                .replace("TEST_CMD", _TEST_CMD)
                .replace("PREPARE_CMD", _PREPARE_CMD)
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


@Instance.register("strapi", "strapi_26884_to_27847")
class STRAPI_26884_TO_27847(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Strapi26884To27847ImageDefault(self.pr, self._config)

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
