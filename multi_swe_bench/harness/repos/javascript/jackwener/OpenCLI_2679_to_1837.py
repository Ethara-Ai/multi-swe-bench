import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "2679_to_1837"
_INTERVAL_NAME = f"OpenCLI_{_INTERVAL}"
_BASE_TAG = f"base-{_INTERVAL}"
_NODE_IMAGE = "node:22-bookworm"

_RESULT_PREFIX = "###OPENCLI_TEST:"
_COUNT_PREFIX = "###OPENCLI_TEST_COUNT:"

_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_SUITE_FILE_RE = re.compile(
    r"^(?:src/.+\.test\.ts|extension/src/.+\.test\.ts|clis/.+\.test\.(?:ts|js))$"
)
_PATCH_TARGET_RE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(
    r"^.*?" + re.escape(_RESULT_PREFIX) + r"(PASS|FAIL|SKIP):(.+?)\s*$"
)


def _extra_test_files(test_patch: str) -> list[str]:
    return sorted(
        {
            path
            for path in _PATCH_TARGET_RE.findall(test_patch or "")
            if _TEST_FILE_RE.search(path) and not _SUITE_FILE_RE.match(path)
        }
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

_VITEST_REPORT_CJS = r"""'use strict'

const fs = require('fs')
const path = require('path')

const STATUS = {
  passed: 'PASS',
  failed: 'FAIL',
  skipped: 'SKIP',
  pending: 'SKIP',
  todo: 'SKIP',
  disabled: 'SKIP',
}

function clean(value) {
  return String(value == null ? '' : value).replace(/\s+/g, ' ').trim()
}

const root = fs.realpathSync(process.cwd())
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const lines = []
const seen = new Map()

for (const file of report.testResults || []) {
  const relative = path.relative(root, String(file.name || '')).split(path.sep).join('/')
  for (const assertion of file.assertionResults || []) {
    const status = STATUS[assertion.status]
    if (!status) {
      continue
    }
    const parts = [relative]
      .concat(assertion.ancestorTitles || [], [assertion.title])
      .map(clean)
      .filter(part => part.length > 0)
    let name = parts.join(' > ')
    const occurrence = (seen.get(name) || 0) + 1
    seen.set(name, occurrence)
    if (occurrence > 1) {
      name += ' #' + occurrence
    }
    lines.push('[[RESULT_PREFIX]]' + status + ':' + name)
  }
}

lines.push('[[COUNT_PREFIX]]' + lines.length)
process.stdout.write(lines.join('\n') + '\n')
"""

_PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

export CI=true
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1

cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

npm ci --ignore-scripts --no-audit --no-fund
npm run build

node --version
npm --version
test -x node_modules/.bin/vitest
test -x node_modules/.bin/tsc
test -f dist/src/main.js
node --input-type=module -e "await import('@jackwener/opencli/registry'); await import('@jackwener/opencli/errors'); console.log('IMPORTS_OK')"
node_modules/.bin/vitest list --filesOnly --project unit --project extension --project adapter > /tmp/opencli-vitest-files.txt
test -s /tmp/opencli-vitest-files.txt
rm -f /tmp/opencli-vitest-files.txt
echo DEPS_OK
"""

_TEST_CMD = r"""node_modules/.bin/tsc -p tsconfig.json --noCheck

rm -f /tmp/opencli-vitest-suite.json /tmp/opencli-vitest-extra.json
vitest_status=0

node_modules/.bin/vitest run --project unit --project extension --project adapter --retry=2 \
    --reporter=default --reporter=json --outputFile.json=/tmp/opencli-vitest-suite.json \
    || vitest_status=$?
node /home/vitest_report.cjs /tmp/opencli-vitest-suite.json

extra_candidates=([[EXTRA_TEST_FILES]])
extra_files=()
for test_file in "${extra_candidates[@]}"; do
    if [ -f "$test_file" ]; then
        extra_files+=("$test_file")
    fi
done

if [ "${#extra_files[@]}" -gt 0 ]; then
    node_modules/.bin/vitest run --passWithNoTests --retry=2 \
        --reporter=default --reporter=json --outputFile.json=/tmp/opencli-vitest-extra.json \
        "${extra_files[@]}" \
        || vitest_status=$?
    node /home/vitest_report.cjs /tmp/opencli-vitest-extra.json
fi

exit "$vitest_status"
"""

_STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1

cd [[REPO_DIR]]

[[PATCH_STEP]][[TEST_CMD]]"""

_TEST_PATCH_STEP = "git apply --whitespace=nowarn /home/test.patch\n\n"
_FIX_PATCH_STEP = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n\n"


class OPENCLI_2679_TO_1837_ImageBase(Image):
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
    npm_config_audit=false \\
    npm_config_fund=false \\
    npm_config_update_notifier=false \\
    FORCE_COLOR=0 \\
    NO_COLOR=1 \\
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
{self.global_env}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null
{self.clear_env}
CMD ["/bin/bash"]
"""


class OPENCLI_2679_TO_1837_ImageDefault(Image):
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
        return OPENCLI_2679_TO_1837_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        extra = " ".join(shlex.quote(path) for path in _extra_test_files(self.pr.test_patch))
        return (
            template.replace("[[TEST_CMD]]", _TEST_CMD)
            .replace("[[EXTRA_TEST_FILES]]", extra)
            .replace("[[REPO_DIR]]", f"/home/{self.pr.repo}")
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[RESULT_PREFIX]]", _RESULT_PREFIX)
            .replace("[[COUNT_PREFIX]]", _COUNT_PREFIX)
        )

    def _stage(self, patch_step: str) -> str:
        return self._expand(_STAGE_SH.replace("[[PATCH_STEP]]", patch_step))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "vitest_report.cjs", self._expand(_VITEST_REPORT_CJS)),
            File(".", "prepare.sh", self._expand(_PREPARE_SH)),
            File(".", "run.sh", self._stage("")),
            File(".", "test-run.sh", self._stage(_TEST_PATCH_STEP)),
            File(".", "fix-run.sh", self._stage(_FIX_PATCH_STEP)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copies = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}
{self.global_env}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copies}
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
{self.clear_env}
"""


@Instance.register("jackwener", _INTERVAL_NAME)
class OPENCLI_2679_TO_1837(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OPENCLI_2679_TO_1837_ImageDefault(self.pr, self._config)

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

        for line in log.splitlines():
            match = _RESULT_RE.match(line)
            if not match:
                continue
            name = re.sub(r"\s+", " ", match.group(2)).strip()
            if not name:
                continue
            status = match.group(1)
            if status == "FAIL":
                failed.add(name)
            elif status == "SKIP":
                skipped.add(name)
            else:
                passed.add(name)

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


Instance.register("jackwener", "OpenCLI")(OPENCLI_2679_TO_1837)
