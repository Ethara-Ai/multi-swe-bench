import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:16-bullseye"

_RESOLVE = [
    "jest",
    "ts-jest",
    "ts-jest/utils",
    "typescript",
    "lodash",
    "nimma/fallbacks",
    "jest-when",
    "fetch-mock",
    "memfs",
]

_MIGRATOR_FIXTURE_CACHE = "packages/ruleset-migrator/src/__tests__/__fixtures__/.cache/index.json"

_BUILD_OUTPUTS = [
    "packages/core/dist/index.js",
    "packages/functions/dist/index.js",
    "packages/rulesets/dist/index.js",
]

_ENV = (
    "export CI=true\n"
    "export HUSKY=0\n"
    "export YARN_ENABLE_IMMUTABLE_INSTALLS=false\n"
    "export YARN_NODE_LINKER=node-modules\n"
    "export YARN_ENABLE_TELEMETRY=0\n"
    'export NODE_OPTIONS="--max-old-space-size=4096"\n'
)

_TEST_CMD = (
    "rm -f /tmp/jest-results.json\n"
    "set +e\n"
    "node_modules/.bin/jest --ci --silent --maxWorkers=4 --cacheDirectory=.cache/.jest "
    "--json --outputFile=/tmp/jest-results.json\n"
    "jest_rc=$?\n"
    "set -e\n"
    'echo "jest exit code: $jest_rc"\n'
    "node /home/jest-report.js /tmp/jest-results.json\n"
)

_JEST_REPORT_JS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('jest-report: no results file at ' + path + ' -- jest produced no output');
    process.exit(1);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('jest-report: could not parse ' + path + ': ' + e.message);
    process.exit(1);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED'};
const clean = s => String(s).replace(/\s+/g, ' ').trim();

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(ROOT)) file = file.slice(ROOT.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    // Some suites declare several tests with an identical describe path and title
    // (e.g. packages/functions length.test.ts). Suffix repeats with their declaration
    // ordinal so each keeps its own result; assertionResults are in declaration order,
    // so the suffix is stable across run/test/fix.
    const seen = new Map();
    for (const a of results) {
        let name = [file].concat((a.ancestorTitles || []).map(clean), [clean(a.title)]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + name);
    }
}

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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


class SpectralImageBase(Image):
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
        return "base-node16"

    def workdir(self) -> str:
        return "base-node16"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class SpectralImageDefault(Image):
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
        return SpectralImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        resolve_js = ", ".join(f"'{m}'" for m in _RESOLVE)
        build_gate = "".join(f"test -s {p}\n" for p in _BUILD_OUTPUTS)
        jest_report_js = _JEST_REPORT_JS.replace("__REPO__", repo)

        return (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            "cat > /home/jest-report.js <<'__JEST_REPORT_EOF__'\n"
            f"{jest_report_js}"
            "__JEST_REPORT_EOF__\n"
            "\n"
            f"{_ENV}"
            "\n"
            "# --- pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- provision ---\n"
            "node --version\n"
            "yarn --version\n"
            "installed=0\n"
            "for attempt in 1 2 3; do\n"
            "    if yarn install; then installed=1; break; fi\n"
            '    echo "prepare: yarn install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'test "$installed" = 1\n'
            "yarn pretest\n"
            "yarn build\n"
            "\n"
            "# --- gate ---\n"
            f'node -e "for (const m of [{resolve_js}]) console.log(m + \' -> \' + require.resolve(m))"\n'
            "node_modules/.bin/jest --version\n"
            f"test -s {_MIGRATOR_FIXTURE_CACHE}\n"
            f"{build_gate}"
            "suites=$(node_modules/.bin/jest --listTests | grep -c '/__tests__/')\n"
            'echo "jest suites: $suites"\n'
            'test "$suites" -gt 0\n'
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{_ENV}"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            f"{_TEST_CMD}"
        )

    def _test_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{_ENV}"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"{_ENV}"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_TEST_CMD}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

# No reset/checkout between prepare.sh and the prune: prepare.sh already landed detached
# on BASE_COMMIT, and a reset here would discard its provisioning. The hardening block's
# own commit-scoped `git checkout --detach` keeps the working tree as prepare.sh left it.
WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("stoplightio", "spectral")
class Spectral(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpectralImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*)$")

        for line in log.splitlines():
            m = result_re.match(line.rstrip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
