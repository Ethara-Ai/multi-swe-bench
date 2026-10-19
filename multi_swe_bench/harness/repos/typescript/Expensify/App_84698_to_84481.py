from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "Expensify"
_REPO = "App"
_ERA_KEY = "App_84698_to_84481"
_BASE_TAG = "base-84698_to_84481"
_NODE_IMAGE = "node:20.20.0-bookworm"
_APT_PACKAGES = "git ca-certificates patch jq"
_TEST_DIRS = ("tests/ui/", "tests/unit/", "tests/actions/", "tests/navigation/")
_TEST_EXTENSIONS = (".ts", ".tsx")
_JEST = "node_modules/.bin/jest"
_JEST_RESULTS = "/tmp/jest-results.json"
_JEST_LISTING = "/tmp/jest-listtests.txt"
_NODE_OPTIONS = "--experimental-vm-modules --max_old_space_size=8192"
_NPM_INSTALL = "npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund"
_PATCH_SCRIPT = "scripts/applyPatches.sh"

_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    LC_ALL=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
    "    NPM_CONFIG_CAFILE=${CA_CERT_PATH} \\\n"
    "    NPM_CONFIG_FETCH_TIMEOUT=900000 \\\n"
    "    NPM_CONFIG_FETCH_RETRIES=8 \\\n"
    "    NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000 \\\n"
    "    NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=180000 \\\n"
    "    NPM_CONFIG_MAXSOCKETS=6 \\\n"
    "    NPM_CONFIG_FUND=false \\\n"
    "    NPM_CONFIG_AUDIT=false \\\n"
    "    NPM_CONFIG_ENGINE_STRICT=false \\\n"
    "    NODE_ENV=test \\\n"
    "    STANDALONE_NEW_DOT=true \\\n"
    "    CI=true"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_GIT_URL_REWRITE = (
    'RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\\n'
    '    git config --global url."https://github.com/".insteadOf "git@github.com:"'
)

_HARDENING_BLOCK = """RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 64m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_JEST_REPORT_JS = """const fs = require('fs');

const path = process.argv[2];
if (!path || !fs.existsSync(path)) {
    console.log('jest-report: no results file at ' + path);
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('jest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED'};
const PREFIX = '/home/__PR_REPO__/';
const flat = (s) => String(s == null ? '' : s).replace(/[\\r\\n]+/g, ' ').replace(/\\s+/g, ' ').trim();

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\\\/g, '/');
    if (file.startsWith(PREFIX)) {
        file = file.slice(PREFIX.length);
    }

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of results) {
        const parts = [file].concat((a.ancestorTitles || []).map(flat), [flat(a.title)]);
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + parts.join(' > '));
    }
}

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^(?P<status>PASSED|FAILED|SKIPPED)\s+(?P<id>\S.*\S|\S)$")


def _submodule_scrub_block(repo: str) -> str:
    return f"""RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
    fi"""


def _test_scope(test_patch: str) -> str:
    paths: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(\S+)", test_patch or "", re.MULTILINE):
        if not path.endswith(_TEST_EXTENSIONS):
            continue
        if not path.startswith(_TEST_DIRS):
            continue
        if path not in paths:
            paths.append(path)
    return " ".join(paths)


def _test_command(scope: str) -> str:
    return f"""TEST_SCOPE="{scope}"
if [ -z "$TEST_SCOPE" ]; then
    echo "jest-report: no jest test targets in this test patch"
    exit 0
fi

rm -f {_JEST_RESULTS}
RC=0
TZ=utc NODE_OPTIONS="{_NODE_OPTIONS}" {_JEST} --ci --silent --runInBand --forceExit \\
    --json --outputFile={_JEST_RESULTS} $TEST_SCOPE 2>&1 || RC=$?
node /home/jest_report.js {_JEST_RESULTS}

exit $RC"""


class ExpensifyApp84698To84481ImageBase(Image):
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

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

{_GIT_URL_REWRITE}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class ExpensifyApp84698To84481ImageDefault(Image):
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
        return ExpensifyApp84698To84481ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        test_cmd = _test_command(_test_scope(self.pr.test_patch))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "jest_report.js", _JEST_REPORT_JS.replace("__PR_REPO__", repo)),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "GIT_TREE_CLEAN"
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

git config --global --add safe.directory '*'

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

node --version
npm --version

{_NPM_INSTALL}
bash {_PATCH_SCRIPT}

test -x {_JEST}
NODE_OPTIONS="{_NODE_OPTIONS}" {_JEST} --listTests > {_JEST_LISTING}
test -s {_JEST_LISTING}
node -e 'require.resolve("jest"); require.resolve("jest-expo"); console.log("DEPS_OK")'
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

cd /home/{repo}
git reset --hard
git clean -fd

{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch

{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        repo = self.pr.repo

        sections = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "COPY jest_report.js /home/\n"
            "COPY check_git_changes.sh /home/\n"
            "COPY prepare.sh /home/\n"
            "COPY run.sh /home/\n"
            "COPY test-run.sh /home/\n"
            "COPY fix-run.sh /home/"
        )

        sections.append("RUN bash /home/prepare.sh")
        sections.append(_HARDENING_BLOCK)
        sections.append(_submodule_scrub_block(repo))

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register(_ORG, _ERA_KEY)
class APP_84698_TO_84481(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ExpensifyApp84698To84481ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        final_status: dict[str, str] = {}
        for raw in _ANSI_RE.sub("", log).splitlines():
            match = _RESULT_RE.match(raw.strip())
            if not match:
                continue
            final_status[match.group("id").strip()] = match.group("status")

        passed_tests: set[str] = {n for n, s in final_status.items() if s == "PASSED"}
        failed_tests: set[str] = {n for n, s in final_status.items() if s == "FAILED"}
        skipped_tests: set[str] = {n for n, s in final_status.items() if s == "SKIPPED"}

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
