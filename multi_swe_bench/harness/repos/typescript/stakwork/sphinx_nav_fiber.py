import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:20-bookworm"

_BASE_RESOLVE = [
    "jest",
    "jest-environment-jsdom",
    "babel-jest",
    "@babel/preset-env",
    "@babel/preset-typescript",
    "@babel/preset-react",
    "babel-plugin-transform-vite-meta-env",
    "@testing-library/react",
    "@testing-library/jest-dom",
    "react",
    "react-dom",
    "styled-components",
]
_PR_RESOLVE: dict[int, list[str]] = {
    831: [
        "@mui/material/IconButton",
        "@mui/material/InputBase",
        "@mui/material/Paper",
    ],
}

_TEST_CMD = (
    "rm -f /tmp/jest-results.json\n"
    "rc=0\n"
    "NODE_ENV=test node_modules/.bin/jest --ci --silent --coverage=false --no-cache "
    '--transformIgnorePatterns "node_modules/(?!@ngrx|(?!d3-force-3d)|ng-dynamic)/" '
    "--json --outputFile=/tmp/jest-results.json || rc=$?\n"
    "node /home/jest-report.js /tmp/jest-results.json\n"
    "if [ ! -s /tmp/jest-results.json ]; then\n"
    '    echo "jest exited with code $rc without writing /tmp/jest-results.json" >&2\n'
    '    exit "$(( rc == 0 ? 1 : rc ))"\n'
    "fi\n"
)

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"

_JEST_REPORT_JS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('jest-report: no results file at ' + path + ' -- jest produced no output');
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

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(ROOT)) file = file.slice(ROOT.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of results) {
        const parts = [file].concat(a.ancestorTitles || [], [a.title]);
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + parts.join(' > '));
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


class SphinxNavFiberImageBase(Image):
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
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN corepack enable || (rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg && corepack enable)

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class SphinxNavFiberImageDefault(Image):
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
        return SphinxNavFiberImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        resolve = _BASE_RESOLVE + _PR_RESOLVE.get(self.pr.number, [])
        resolve_js = ", ".join(f"'{m}'" for m in resolve)

        jest_report_js = _JEST_REPORT_JS.replace("__REPO__", repo)

        return (
            f"{_SCRIPT_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "# Result reporter the run scripts pipe jest's JSON through; written outside the repo.\n"
            "cat > /home/jest-report.js <<'__JEST_REPORT_EOF__'\n"
            f"{jest_report_js}"
            "__JEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "\n"
            "# Run the vendored yarn named by .yarnrc.yml (the release packageManager pins)\n"
            "# directly, so the install never depends on corepack fetching yarn. The Cypress\n"
            "# binary is only needed for e2e/component tests, and husky's postinstall hook\n"
            "# setup is irrelevant in the image.\n"
            "YARN_JS=$(sed -n 's/^yarnPath: *//p' .yarnrc.yml)\n"
            'test -f "$YARN_JS"\n'
            'node "$YARN_JS" --version\n'
            "export CYPRESS_INSTALL_BINARY=0\n"
            "export HUSKY=0\n"
            "installed=0\n"
            "for attempt in 1 2 3; do\n"
            '    if node "$YARN_JS" install --immutable; then installed=1; break; fi\n'
            '    echo "prepare: yarn install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'if [ "$installed" != 1 ]; then\n'
            '    echo "prepare: yarn install --immutable failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter the run scripts need, the test runner and every module the graded\n"
            "# command loads, and jest's own config resolving to a non-empty test list.\n"
            "test -s /home/jest-report.js\n"
            "test -x node_modules/.bin/jest\n"
            f'node -e "require(\'./package.json\'); for (const m of [{resolve_js}]) console.log(m + \' -> \' + require.resolve(m))"\n'
            "node_modules/.bin/jest --version\n"
            "test_files=$(NODE_ENV=test node_modules/.bin/jest --ci --listTests | wc -l)\n"
            'echo "jest --listTests: $test_files files"\n'
            'test "$test_files" -gt 0\n'
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_TEST_CMD}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
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

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("stakwork", "sphinx-nav-fiber")
class SphinxNavFiber(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SphinxNavFiberImageDefault(self.pr, self._config)

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
