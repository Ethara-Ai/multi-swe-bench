from __future__ import annotations

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "hospitalrun-frontend_2644_to_2108"
_BASE_TAG = "base-2644_to_2108"
_NODE_IMAGE = "node:16-bookworm"
_JEST_REPORT = "/home/jest-report.json"
_NODE_HEAP_MB = "2048"
_NPM_INSTALL_ATTEMPTS = "4"
_NPM_RETRY_PAUSE_SECONDS = "30"
_JEST_MAX_WORKERS = "4"
_RESULT_MARKER = "MSB-TEST-RESULT"
_SUMMARY_MARKER = "MSB-TEST-SUMMARY"
_EMPTY_SUITE_LABEL = "<suite reported no tests>"
_SPEC_SUFFIXES = (".ts", ".tsx")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(
    r"^" + re.escape(_RESULT_MARKER) + r"\|(PASSED|FAILED|SKIPPED)\|(.+)$"
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_SEED_DEPS_JS = """const fs = require('fs');

const MANIFEST_RE = / b\\/(?:[^ ]*\\/)?package\\.json$/;
const NAME_RE = /^(?:@[a-z0-9-*~][a-z0-9-*._~]*\\/)?[a-z0-9-~][a-z0-9-._~]*$/;
const RANGE_RE = /^[\\^~>=<]*\\d[0-9a-zA-Z.+\\-*|\\s]*$/;
const ENTRY_RE = /^\\s*"([^"]+)"\\s*:\\s*"([^"]+)"\\s*,?\\s*$/;

const specs = new Map();

for (const file of process.argv.slice(2)) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (e) {
    continue;
  }
  let inManifest = false;
  for (const raw of text.split('\\n')) {
    const line = raw.replace(/\\r$/, '');
    if (line.indexOf('diff --git ') === 0) {
      inManifest = MANIFEST_RE.test(line);
      continue;
    }
    if (!inManifest) continue;
    if (line.charAt(0) !== '+' || line.indexOf('+++') === 0) continue;
    const match = line.slice(1).match(ENTRY_RE);
    if (!match) continue;
    const name = match[1];
    const range = match[2];
    if (!NAME_RE.test(name) || !RANGE_RE.test(range)) continue;
    specs.set(name, name + '@' + range);
  }
}

for (const spec of specs.values()) console.log(spec);
"""

_EMIT_RESULTS_JS = (
    """const fs = require('fs');
const path = require('path');

const RESULT_MARKER = '"""
    + _RESULT_MARKER
    + """';
const SUMMARY_MARKER = '"""
    + _SUMMARY_MARKER
    + """';
const EMPTY_SUITE_LABEL = '"""
    + _EMPTY_SUITE_LABEL
    + """';

const reportFile = process.argv[2];
const repoRoot = String(process.argv[3] || '').replace(/\\/+$/, '');

let report;
try {
  report = JSON.parse(fs.readFileSync(reportFile, 'utf8'));
} catch (e) {
  console.log(SUMMARY_MARKER + '|error=' + String(e && e.message));
  process.exit(1);
}

const STATUS = {
  passed: 'PASSED',
  failed: 'FAILED',
  pending: 'SKIPPED',
  skipped: 'SKIPPED',
  todo: 'SKIPPED',
  disabled: 'SKIPPED'
};

function relative(name) {
  const value = String(name || '');
  if (value.indexOf(repoRoot + '/') === 0) return value.slice(repoRoot.length + 1);
  return path.relative(repoRoot, value).split(path.sep).join('/');
}

function clean(value) {
  return String(value == null ? '' : value).replace(/\\s+/g, ' ').trim();
}

let raw = 0;
let files = 0;
let emptySuites = 0;

for (const file of report.testResults || []) {
  files++;
  const rel = relative(file.name);
  const results = file.assertionResults || [];
  if (results.length === 0) {
    if (file.status !== 'passed') {
      emptySuites++;
      console.log(RESULT_MARKER + '|FAILED|' + rel + ' > ' + EMPTY_SUITE_LABEL);
    }
    continue;
  }
  for (const assertion of results) {
    raw++;
    const status = STATUS[assertion.status] || 'SKIPPED';
    const titles = (assertion.ancestorTitles || [])
      .concat([assertion.title || ''])
      .map(clean)
      .filter(Boolean);
    console.log(RESULT_MARKER + '|' + status + '|' + rel + ' > ' + titles.join(' > '));
  }
}

console.log(
  SUMMARY_MARKER +
    '|raw=' + raw +
    '|files=' + files +
    '|emptySuites=' + emptySuites +
    '|reported=' + (report.numTotalTests || 0)
);
"""
)

_SHELL_PRELUDE = (
    """export CI=true
export NODE_OPTIONS=--max-old-space-size="""
    + _NODE_HEAP_MB
    + """
export npm_config_audit=false
export npm_config_fund=false
export npm_config_progress=false
export npm_config_loglevel=error
export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=120000
export npm_config_fetch_timeout=600000
export npm_config_maxsockets=4

"""
)

_PREPARE_HELPERS = """
assert_detached_head() {
    git rev-parse --verify HEAD > /dev/null
    if git symbolic-ref -q HEAD > /dev/null; then
        echo "prepare: HEAD is attached to a branch after the pin" >&2
        exit 1
    fi
}

resolve_era() {
    BASE_DATE="$(git log -1 --format=%cI "$BASE_SHA")"
    test -n "$BASE_DATE"
    echo "prepare: resolving dependencies as of $BASE_DATE"
}

npm_install_with_retries() {
    attempt=1
    until npm install "$@"; do
        if [ "$attempt" -ge "$NPM_INSTALL_ATTEMPTS" ]; then
            echo "prepare: npm install failed after $attempt attempts" >&2
            return 1
        fi
        echo "prepare: npm install attempt $attempt failed; retrying in ${NPM_RETRY_PAUSE_SECONDS}s" >&2
        attempt=$((attempt + 1))
        sleep "$NPM_RETRY_PAUSE_SECONDS"
    done
}

install_deps() {
    npm_install_with_retries --before="$BASE_DATE" --legacy-peer-deps --ignore-scripts --no-audit --no-fund
}

seed_patch_deps() {
    PATCH_DEPS="$(node /home/seed-deps.js /home/fix.patch /home/test.patch)"
    if [ -n "$PATCH_DEPS" ]; then
        echo "prepare: seeding dependencies introduced by the patches: $PATCH_DEPS"
        npm_install_with_retries --no-save --before="$BASE_DATE" --legacy-peer-deps --ignore-scripts --no-audit --no-fund $PATCH_DEPS
    else
        echo "prepare: the patches introduce no new dependency"
    fi
}

deps_gate() {
    test -x ./node_modules/.bin/react-scripts
    test -x ./node_modules/.bin/cross-env
    node -e "require('./package.json'); require.resolve('react-scripts/scripts/test.js'); require.resolve('jest'); require.resolve('@testing-library/react'); console.log('DEPS_OK')"
}

"""

_RUN_HELPERS = (
    """
run_tests() {
    if [ ${#TEST_PATHS[@]} -eq 0 ]; then
        echo "stage: the test patch names no spec file" >&2
        exit 1
    fi
    PRESENT=()
    for spec in "${TEST_PATHS[@]}"; do
        if [ -f "$spec" ]; then
            PRESENT+=("$spec")
        else
            echo "stage: $spec does not exist at this stage; the test patch adds it"
        fi
    done
    if [ ${#PRESENT[@]} -eq 0 ]; then
        echo "stage: none of the test patch's spec files exist at this stage" >&2
        exit 1
    fi
    rm -f "$JEST_REPORT"
    set +e
    npm run test:ci -- --json --outputFile="$JEST_REPORT" --watchAll=false \\
        --maxWorkers="""
    + _JEST_MAX_WORKERS
    + """ --runTestsByPath "${PRESENT[@]}"
    JEST_STATUS=$?
    set -e
    if [ ! -s "$JEST_REPORT" ]; then
        echo "stage: the test runner wrote no JSON report (exit $JEST_STATUS)" >&2
        exit 1
    fi
    node /home/emit-results.js "$JEST_REPORT" "$REPO_ROOT"
}

"""
)

_PREPARE_BODY = """cd "$REPO_ROOT"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "$BASE_SHA"
assert_detached_head
bash /home/check_git_changes.sh

resolve_era
install_deps
seed_patch_deps

deps_gate
"""

_RUN_BODY = """cd "$REPO_ROOT"
run_tests
"""

_TEST_RUN_BODY = """cd "$REPO_ROOT"
git apply --whitespace=nowarn /home/test.patch
run_tests
"""

_FIX_RUN_BODY = """cd "$REPO_ROOT"
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
run_tests
"""


def _test_paths(pr: PullRequest) -> list[str]:
    paths: list[str] = []
    for line in (pr.test_patch or "").splitlines():
        if not line.startswith("diff --git "):
            continue
        _, separator, path = line.partition(" b/")
        path = path.strip()
        if not separator or not path.endswith(_SPEC_SUFFIXES):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def _script(pr: PullRequest, helpers: str, body: str) -> str:
    quoted = " ".join(shlex.quote(path) for path in _test_paths(pr))
    return (
        "#!/bin/bash\nset -eo pipefail\n\n"
        + _SHELL_PRELUDE
        + f"REPO_ROOT=/home/{pr.repo}\n"
        + f"BASE_SHA={pr.base.sha}\n"
        + f"NPM_INSTALL_ATTEMPTS={_NPM_INSTALL_ATTEMPTS}\n"
        + f"NPM_RETRY_PAUSE_SECONDS={_NPM_RETRY_PAUSE_SECONDS}\n"
        + f"JEST_REPORT={_JEST_REPORT}\n"
        + f"TEST_PATHS=({quoted})\n"
        + helpers
        + body
    )


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for line in _ANSI_RE.sub("", test_log).splitlines():
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        name = match.group(2).strip()
        if not name:
            continue
        status = match.group(1)
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


class HospitalRunFrontendIntervalImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org, repo = self.pr.org, self.pr.repo

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
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

RUN set -eux; \\
    command -v git; \\
    command -v curl; \\
    command -v patch; \\
    node --version; \\
    npm --version; \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class HospitalRunFrontendIntervalImageDefault(Image):
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
        return HospitalRunFrontendIntervalImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "seed-deps.js", _SEED_DEPS_JS),
            File(".", "emit-results.js", _EMIT_RESULTS_JS),
            File(".", "prepare.sh", _script(self.pr, _PREPARE_HELPERS, _PREPARE_BODY)),
            File(".", "run.sh", _script(self.pr, _RUN_HELPERS, _RUN_BODY)),
            File(".", "test-run.sh", _script(self.pr, _RUN_HELPERS, _TEST_RUN_BODY)),
            File(".", "fix-run.sh", _script(self.pr, _RUN_HELPERS, _FIX_RUN_BODY)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

"""


@Instance.register("HospitalRun", _INTERVAL_NAME)
class HOSPITALRUN_FRONTEND_2644_TO_2108(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HospitalRunFrontendIntervalImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)
