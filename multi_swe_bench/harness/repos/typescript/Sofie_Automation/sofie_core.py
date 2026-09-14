import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-1396_to_1396"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
"""

_EMIT_TESTCASES_JS = """const path = require('path')

const repoDir = '/home/__REPO__'
const clean = (v) => String(v === undefined || v === null ? '' : v).replace(/\\s+/g, ' ').trim()

class TestcaseReporter {
    onTestResult(_test, result) {
        const rel = path.relative(repoDir, result.testFilePath || '').split(path.sep).join('/')
        for (const tc of result.testResults || []) {
            const parts = [rel, ...(tc.ancestorTitles || []).map(clean), clean(tc.title)].filter(Boolean)
            const status =
                tc.status === 'passed' ? 'PASSED' : tc.status === 'failed' ? 'FAILED' : 'SKIPPED'
            process.stdout.write('TESTCASE ' + status + ' ' + parts.join(' > ') + '\\n')
        }
    }
}

module.exports = TestcaseReporter
"""

_JEST_CONFIG_JS = """const base = require('/home/__REPO__/packages/job-worker/jest.config.js')

const transform = {}
for (const [pattern, entry] of Object.entries(base.transform || {})) {
    transform[pattern] = Array.isArray(entry)
        ? [entry[0], { ...(entry[1] || {}), diagnostics: false }]
        : entry
}

module.exports = { ...base, rootDir: '/home/__REPO__/packages/job-worker', transform }
"""

_PREPARE_SH = """#!/bin/bash
set -e

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NODE_OPTIONS=--max-old-space-size=4096
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

corepack enable
cd packages
yarn install --no-immutable
yarn lerna run build --scope @sofie-automation/job-worker --include-dependencies
node -e "require('@sofie-automation/corelib/dist/dataModel/Piece'); console.log('DEPS_OK')"
"""

_RUN_SH = """#!/bin/bash
set -uo pipefail

export CI=true TZ=UTC LC_ALL=C.UTF-8 FORCE_COLOR=0 NO_COLOR=1
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/__REPO__/packages/job-worker
../node_modules/.bin/jest --ci --coverage=false --maxWorkers=2 \\
    --config /home/jest.config.js --reporters=/home/emit_testcases.js
exit 0
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

bash /home/run.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
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
    ca-certificates git python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
    )


class SofieCoreImageBase(Image):
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
        return _render(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency()), self.pr
        )


class SofieCoreImageDefault(Image):
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
        return SofieCoreImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_testcases.js", _render(_EMIT_TESTCASES_JS, self.pr)),
            File(".", "jest.config.js", _render(_JEST_CONFIG_JS, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        prepare_copy = "".join(f"COPY {f.name} /home/\n" for f in self.prepare_files())
        graded_copy = "".join(f"COPY {f.name} /home/\n" for f in self.graded_files())

        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("Sofie-Automation", "sofie-core")
class SOFIE_CORE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SofieCoreImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in test_log.split("\n"):
            match = _TESTCASE_RE.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("Sofie-Automation", "sofie-core_1396_to_1396")(SOFIE_CORE)
Instance.register("Sofie-Automation", "sofie-core_1396")(SOFIE_CORE)
