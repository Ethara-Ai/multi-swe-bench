import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:20.19.5"
_BASE_APT = "ca-certificates git jq"
JEST_RESULTS = "/tmp/jest-results.json"
_JEST_TEST_DIRS = ("tests/ui/", "tests/unit/", "tests/actions/", "tests/navigation/")
_ROOT_TEST_RE = re.compile(r"^[^/]*\.(spec|test)\.tsx?$")


def _scoped_test_patterns(pr: PullRequest) -> str:
    paths = []
    for path in re.findall(r"^\+\+\+ b/(\S+)$", pr.test_patch or "", re.M):
        if not path.endswith((".ts", ".tsx")):
            continue
        if not (path.startswith(_JEST_TEST_DIRS) or _ROOT_TEST_RE.match(path)):
            continue
        if path not in paths:
            paths.append(path)
    if not paths:
        return "'/__no_scoped_jest_tests__$'"
    return " ".join("'/" + re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", p) + "$'" for p in paths)


_JEST_REPORT_JS = r"""const fs = require('fs');

const resultsPath = process.argv[2];
const repoPrefix = process.argv[3];
if (!fs.existsSync(resultsPath)) {
    console.log('jest-report: no results file at ' + resultsPath);
    process.exit(0);
}
const report = JSON.parse(fs.readFileSync(resultsPath, 'utf8'));
const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED'};
const flat = (s) => String(s == null ? '' : s).replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
const seen = new Map();

for (const suite of report.testResults || []) {
    let file = String(suite.name || '');
    if (file.startsWith(repoPrefix)) {
        file = file.slice(repoPrefix.length);
    }
    const assertions = suite.assertionResults || [];
    if (assertions.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of assertions) {
        let name = [file].concat((a.ancestorTitles || []).map(flat), [flat(a.title)]).join(' > ');
        const count = (seen.get(name) || 0) + 1;
        seen.set(name, count);
        if (count > 1) {
            name = name + ' (' + count + ')';
        }
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + name);
    }
}
console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) + ' tests=' + (report.numTotalTests || 0) +
    ' passed=' + (report.numPassedTests || 0) + ' failed=' + (report.numFailedTests || 0) +
    ' pending=' + (report.numPendingTests || 0));
"""


class ExpensifyApp81705To82030ImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        interval = self.pr.number_interval or ""
        _, _, span = interval.partition("_")
        return f"base-{span}" if span else "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {image}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN npm config set fetch-retries 8 && \\
    npm config set fetch-retry-mintimeout 20000 && \\
    npm config set fetch-retry-maxtimeout 180000 && \\
    npm config set fetch-timeout 900000 && \\
    node --version && npm --version && jq --version

RUN git config --global --add safe.directory '*' && \\
    git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\
    git config --global --add url."https://github.com/".insteadOf "git@github.com:" && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global http.postBuffer 524288000 && \\
    git config --global http.lowSpeedLimit 1000 && \\
    git config --global http.lowSpeedTime 600

WORKDIR /home/

RUN for attempt in 1 2 3; do \\
        git clone "${{REPO_URL}}" /home/{repo} && break; \\
        rm -rf /home/{repo}; \\
        if [ "$attempt" -eq 3 ]; then exit 1; fi; \\
        sleep 30; \\
    done && \\
    git -C /home/{repo} rev-parse HEAD

CMD ["/bin/bash"]
"""


class ExpensifyApp81705To82030ImageDefault(Image):
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
        return ExpensifyApp81705To82030ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha
        jest_cmd = (
            "npm test -- --ci --silent --forceExit --maxWorkers=2 --passWithNoTests "
            f"--json --outputFile={JEST_RESULTS} {_scoped_test_patterns(self.pr)}"
        )

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "export CI=true\n"
            "export npm_config_engine_strict=false\n"
            "npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund\n"
            "bash scripts/applyPatches.sh\n"
            "node_modules/.bin/jest --version\n"
            "node -e \"for (const m of ['jest', 'jest-expo', 'babel-jest', 'jest-transformer-svg', 'patch-package', '@react-native-google-signin/google-signin/jest/build/setup.js']) require.resolve(m); console.log('jest toolchain resolved')\"\n"
            "TZ=utc node_modules/.bin/jest --listTests > /tmp/jest-list.txt\n"
            f'grep -q "^/home/{repo}/tests/unit/" /tmp/jest-list.txt\n'
            'echo "DEPS_OK $(wc -l < /tmp/jest-list.txt) jest test files"\n'
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            f"rm -f {JEST_RESULTS}\n"
            "rc=0\n"
            f"{jest_cmd} || rc=$?\n"
            f"node /home/jest_report.js {JEST_RESULTS} /home/{repo}/\n"
            'exit "$rc"\n'
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"rm -f {JEST_RESULTS}\n"
            "rc=0\n"
            f"{jest_cmd} || rc=$?\n"
            f"node /home/jest_report.js {JEST_RESULTS} /home/{repo}/\n"
            'exit "$rc"\n'
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"rm -f {JEST_RESULTS}\n"
            "rc=0\n"
            f"{jest_cmd} || rc=$?\n"
            f"node /home/jest_report.js {JEST_RESULTS} /home/{repo}/\n"
            'exit "$rc"\n'
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "jest_report.js", _JEST_REPORT_JS),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN cd /home/{repo} && \\
    git config --local pack.windowMemory 256m && \\
    git config --local pack.deltaCacheSize 128m && \\
    git config --local pack.threads 4

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
"""


@Instance.register("Expensify", "App_81705_to_82030")
class APP_81705_TO_82030(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ExpensifyApp81705To82030ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*\S)\s*$")

        for line in test_log.splitlines():
            m = result_re.match(line.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "FAILED":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASSED":
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            elif name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("Expensify", "App")(APP_81705_TO_82030)
