import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-8177_to_7931"

_REPORTER_NAME = "jest-ci-reporter.cjs"
_REPORTER_PATH = f"/home/{_REPORTER_NAME}"
_RESULT_PREFIX = "###JEST_RESULT:"
_SUITE_ERROR_PREFIX = "###JEST_SUITE_ERROR:"
_RUN_COMPLETE_PREFIX = "###JEST_RUN_COMPLETE:"

_TIMEZONES = "Australia/Brisbane America/Los_Angeles Europe/Madrid Asia/Nagpur"
_TIMEZONE_TARGETS = (
    "src/js/components/Calendar/__tests__/Calendar-test.tsx "
    "src/js/components/DateInput/__tests__/DateInput-test.tsx"
)

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_REPORTER_CJS = r"""'use strict'

const fs = require('fs')
const path = require('path')

const RESULT_PREFIX = '[[RESULT_PREFIX]]'
const SUITE_ERROR_PREFIX = '[[SUITE_ERROR_PREFIX]]'
const RUN_COMPLETE_PREFIX = '[[RUN_COMPLETE_PREFIX]]'

const STATUS = {
  passed: 'PASS',
  failed: 'FAIL',
  pending: 'SKIP',
  skipped: 'SKIP',
  todo: 'SKIP',
  disabled: 'SKIP',
}

function write(text) {
  const buf = Buffer.from(text, 'utf8')
  let offset = 0
  while (offset < buf.length) {
    try {
      offset += fs.writeSync(1, buf, offset, buf.length - offset)
    } catch (err) {
      if (err.code === 'EAGAIN') {
        continue
      }
      throw err
    }
  }
}

function clean(value) {
  return String(value == null ? '' : value).replace(/\s+/g, ' ').trim()
}

class JestCiReporter {
  constructor(globalConfig) {
    this.rootDir = globalConfig.rootDir
    this.label = clean(process.env.JEST_RESULT_LABEL)
  }

  head(testFilePath) {
    const file = path
      .relative(this.rootDir, testFilePath)
      .split(path.sep)
      .join('/')
    return this.label ? file + ' > [' + this.label + ']' : file
  }

  onTestResult(test, testResult) {
    const head = this.head(testResult.testFilePath)
    if (testResult.testExecError) {
      write(SUITE_ERROR_PREFIX + head + '\n')
    }
    for (const result of testResult.testResults) {
      const status = STATUS[result.status]
      if (!status) {
        continue
      }
      const parts = [head]
        .concat(result.ancestorTitles.map(clean), [clean(result.title)])
        .filter(part => part.length > 0)
      write(RESULT_PREFIX + status + ':' + parts.join(' > ') + '\n')
    }
  }

  onRunComplete(contexts, results) {
    write(
      RUN_COMPLETE_PREFIX +
        results.numPassedTests +
        ':' +
        results.numFailedTests +
        ':' +
        (results.numPendingTests + results.numTodoTests) +
        ':' +
        results.numRuntimeErrorTestSuites +
        '\n',
    )
  }
}

module.exports = JestCiReporter
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export YARN_ENABLE_IMMUTABLE_INSTALLS=false

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

yarn install || yarn install

node --version
yarn --version
test -d node_modules
test -x node_modules/.bin/jest
node -e "for (const m of ['jest', 'jest-environment-jsdom', 'ts-jest', 'babel-jest', 'typescript', 'react', 'react-dom', 'styled-components', 'jest-styled-components', 'jest-axe', '@testing-library/react', '@testing-library/jest-dom', '@testing-library/user-event']) require.resolve(m); console.log('RESOLVE_OK')"
listed_tests=$(node_modules/.bin/jest --listTests)
test -n "$listed_tests"

mkdir -p src/js/__deps_gate__/__tests__
printf "describe('gate', () => {\n  test('works', () => {\n    const node: HTMLDivElement = document.createElement('div');\n    expect(node).toBeTruthy();\n  });\n});\n" > src/js/__deps_gate__/__tests__/gate-test.tsx
gate_out=$(node_modules/.bin/jest src/js/__deps_gate__ --ci --coverage=false --runInBand --reporters=[[REPORTER_PATH]])
rm -rf src/js/__deps_gate__
grep -Fqx '[[RESULT_PREFIX]]PASS:src/js/__deps_gate__/__tests__/gate-test.tsx > gate > works' <<< "$gate_out"
grep -Fqx '[[RUN_COMPLETE_PREFIX]]1:0:0:0' <<< "$gate_out"
echo DEPS_OK
"""

_STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export NODE_ENV=test
export FORCE_COLOR=0
export NO_COLOR=1
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/[[REPO]]
[[PATCH_STEP]]
node --version
test -x node_modules/.bin/jest

overall_status=0

run_jest() {
    label="$1"
    zone="$2"
    shift 2
    jest_status=0
    TZ="$zone" JEST_RESULT_LABEL="$label" node_modules/.bin/jest "$@" \
        --ci \
        --coverage=false \
        --runInBand \
        --testTimeout=30000 \
        --reporters=default \
        --reporters=[[REPORTER_PATH]] || jest_status=$?
    echo "###JEST_EXIT:${label:-main}:$jest_status"
    if [ "$jest_status" -ne 0 ]; then
        overall_status=1
    fi
}

run_jest "" UTC

for zone in [[TIMEZONES]]; do
    run_jest "TZ=$zone" "$zone" [[TIMEZONE_TARGETS]]
done

echo "###JEST_EXIT:all:$overall_status"
exit "$overall_status"
"""

_TEST_PATCH_STEP = "\ngit apply --whitespace=nowarn /home/test.patch\n"

_FIX_PATCH_STEP = "\ngit apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^.*?" + re.escape(_RESULT_PREFIX) + r"(PASS|FAIL|SKIP):(.+?)\s*$")
_SUITE_ERROR_RE = re.compile(r"^.*?" + re.escape(_SUITE_ERROR_PREFIX) + r"(.+?)\s*$")


class GROMMET_8177_TO_7931_ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_GLOBAL_CACHE=1 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_NODE_LINKER=node-modules \\
    FORCE_COLOR=1 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class GROMMET_8177_TO_7931_ImageDefault(Image):
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
        return GROMMET_8177_TO_7931_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[REPORTER_PATH]]", _REPORTER_PATH)
            .replace("[[RESULT_PREFIX]]", _RESULT_PREFIX)
            .replace("[[SUITE_ERROR_PREFIX]]", _SUITE_ERROR_PREFIX)
            .replace("[[RUN_COMPLETE_PREFIX]]", _RUN_COMPLETE_PREFIX)
            .replace("[[TIMEZONES]]", _TIMEZONES)
            .replace("[[TIMEZONE_TARGETS]]", _TIMEZONE_TARGETS)
        )

    def _stage(self, patch_step: str) -> str:
        return self._render(_STAGE_SH.replace("[[PATCH_STEP]]", patch_step))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", _REPORTER_NAME, self._render(_REPORTER_CJS)),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._stage("")),
            File(".", "test-run.sh", self._stage(_TEST_PATCH_STEP)),
            File(".", "fix-run.sh", self._stage(_FIX_PATCH_STEP)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
"""


@Instance.register("grommet", "grommet_8177_to_7931")
class GROMMET_8177_TO_7931(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GROMMET_8177_TO_7931_ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = _ANSI_RE.sub("", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        occurrences: dict[str, int] = {}

        for line in log.splitlines():
            line = line.rstrip()

            match = _RESULT_RE.match(line)
            if match:
                status = match.group(1)
                name = re.sub(r"\s+", " ", match.group(2)).strip()
                if not name:
                    continue
                occurrences[name] = occurrences.get(name, 0) + 1
                if occurrences[name] > 1:
                    name = f"{name} #{occurrences[name]}"
                if status == "FAIL":
                    failed.add(name)
                elif status == "SKIP":
                    skipped.add(name)
                else:
                    passed.add(name)
                continue

            match = _SUITE_ERROR_RE.match(line)
            if match:
                name = re.sub(r"\s+", " ", match.group(1)).strip()
                if name:
                    failed.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("grommet", "grommet")(GROMMET_8177_TO_7931)
