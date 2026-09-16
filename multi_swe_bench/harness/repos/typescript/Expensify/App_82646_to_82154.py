import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_ROOTS = ("tests/ui/", "tests/unit/", "tests/actions/", "tests/navigation/")


def _test_paths(pr: PullRequest) -> list[str]:
    paths = []
    for line in (pr.test_patch or "").splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        if not m:
            continue
        path = m.group(2)
        if not path.endswith((".ts", ".tsx")):
            continue
        if not path.startswith(_TEST_ROOTS):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def _test_scope(pr: PullRequest) -> str:
    return " ".join("'" + p.replace(".", "\\.") + "$'" for p in _test_paths(pr))


def _run_scope_block(pr: PullRequest) -> str:
    """Baseline scope block for run.sh.

    Keeps the PR's own test files when they already exist on the base tree, so
    the before-patch baseline is unchanged. A PR whose test patch only ADDS new
    test files would otherwise match nothing on base (empty baseline), so when
    none of the scoped files exist yet, fall back to the whole test tree and
    the baseline still captures pre-existing suite behavior.
    """
    paths = _test_paths(pr)
    if not paths:
        return ""

    lines = ['RUN_SCOPE=""']
    for p in paths:
        pattern = p.replace(".", "\\.")
        lines.append(
            f'if [ -f "{p}" ]; then RUN_SCOPE="$RUN_SCOPE {pattern}$"; fi'
        )
    fallback = " ".join(r.rstrip("/") for r in _TEST_ROOTS)
    lines.append(f'if [ -z "$RUN_SCOPE" ]; then RUN_SCOPE="{fallback}"; fi')
    return "\n".join(lines) + "\n"


_JEST_REPORT_JS = r"""const fs = require('fs');

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
const PREFIX = '/home/__REPO__/';
const flat = (s) => String(s == null ? '' : s).replace(/[\r\n]+/g, ' ').trim();

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(PREFIX)) file = file.slice(PREFIX.length);

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


_PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export npm_config_engine_strict=false

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

ok=0
for attempt in 1 2 3 4; do
    extra=""
    if [ "$attempt" -gt 1 ]; then
        extra="--prefer-offline"
    fi
    if npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund $extra; then
        ok=1
        echo "prepare: npm ci succeeded on attempt $attempt"
        break
    fi
    echo "prepare: npm ci attempt $attempt failed"
    sleep 15
done
if [ "$ok" -ne 1 ]; then
    echo "prepare: npm ci failed after 4 attempts"
    exit 1
fi

if [ -d patches ]; then
    patch_dir="$(mktemp -d ./tmp-patches-XXXXXX)"
    find ./patches -type f -name '*.patch' -exec cp {} "$patch_dir" \;
    npx --no-install patch-package --patch-dir "$patch_dir"
    rm -rf "$patch_dir"
fi

node --version
npm --version
node_modules/.bin/jest --version
node -e "require('./package.json'); require.resolve('jest'); require.resolve('jest-expo'); require.resolve('babel-jest'); require.resolve('jest-environment-jsdom'); require.resolve('@testing-library/react-native'); console.log('DEPS_OK')"
"""


_RUN_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--experimental-vm-modules --max_old_space_size=8192"

cd /home/__REPO__
"""


_TEST_CMD = """rm -f /tmp/jest-results.json
rc=0
TZ=utc node_modules/.bin/jest --ci --silent --runInBand --forceExit --json --outputFile=/tmp/jest-results.json __SCOPE__ || rc=$?
node /home/jest-report.js /tmp/jest-results.json
exit "$rc"
"""


class ExpensifyApp82646To82154ImageBase(Image):
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
        return "node:20.19.5-bookworm"

    def image_tag(self) -> str:
        return "base-82646_to_82154"

    def workdir(self) -> str:
        return "base-82646_to_82154"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

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

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

RUN npm config set fetch-timeout 900000 && \\
    npm config set fetch-retries 8 && \\
    npm config set fetch-retry-mintimeout 20000 && \\
    npm config set fetch-retry-maxtimeout 180000 && \\
    npm config set maxsockets 6 && \\
    npm config set fund false && \\
    npm config set audit false

RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\
    git config --global url."https://github.com/".insteadOf "git@github.com:" && \\
    git config --global --add safe.directory '*'

{self.clear_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class ExpensifyApp82646To82154ImageDefault(Image):
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
        return ExpensifyApp82646To82154ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = _TEST_CMD.replace("__SCOPE__", _test_scope(self.pr))
        header = _RUN_HEADER.replace("__REPO__", repo)

        run_test_cmd = _TEST_CMD.replace("__SCOPE__", "$RUN_SCOPE")
        run_scope = _run_scope_block(self.pr)
        if run_scope:
            run_test_cmd = run_scope + run_test_cmd
        else:
            run_test_cmd = test_cmd
        run_sh = header + "\n" + run_test_cmd
        test_run_sh = (
            header
            + "git apply --whitespace=nowarn /home/test.patch\n\n"
            + test_cmd
        )
        fix_run_sh = (
            header
            + "git apply --whitespace=nowarn /home/test.patch\n"
            + "git apply --whitespace=nowarn /home/fix.patch\n\n"
            + test_cmd
        )
        prepare_sh = _PREPARE_SH.replace("__REPO__", repo).replace(
            "__BASE_SHA__", self.pr.base.sha
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "jest-report.js", _JEST_REPORT_JS.replace("__REPO__", repo)),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening_block = Image._HARDENING_BLOCK.replace(" --aggressive", "")

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach "${{BASE_COMMIT}}"

RUN git config --local pack.windowMemory 256m && \\
    git config --local pack.deltaCacheSize 128m && \\
    git config --local pack.threads 2

RUN bash /home/prepare.sh

{hardening_block}"""


@Instance.register("Expensify", "App_82646_to_82154")
class APP_82646_TO_82154(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExpensifyApp82646To82154ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S.*)$")

        for raw in clean_log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
