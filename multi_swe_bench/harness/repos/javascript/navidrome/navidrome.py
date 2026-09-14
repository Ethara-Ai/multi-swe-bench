import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# navidrome is a Go repo, but the graded PRs here live entirely in the React UI under
# ui/ (react-scripts 3.4 -> jest 24, lockfileVersion 1). The repo's .nvmrc and CI pin
# Node 14, whose bundled npm 6 is the one that reads a v1 lockfile natively.
NODE_IMAGE = "node:14-bullseye"

# Dev dependencies a PR's test.patch imports but only its fix.patch adds to
# ui/package.json. Pre-installed at the exact version from the fix patch's own
# package-lock.json, so the grading environment can load the gold test without any
# network during run/test/fix. This does not make the test stage pass: the test still
# imports modules and constants that only the fix patch creates.
_PR_TEST_DEPS: dict[int, list[str]] = {
    835: ["@testing-library/react-hooks@5.1.0"],
}

# Modules the graded test command loads; asserted by the DEPS_OK gate in prepare.sh.
_BASE_RESOLVE = [
    "react-scripts/package.json",
    "jest",
    "jest-environment-jsdom-fourteen",
    "@testing-library/jest-dom/extend-expect",
    "react",
    "react-dom",
]
_PR_RESOLVE: dict[int, list[str]] = {
    835: [
        "@testing-library/react-hooks",
        "css-mediaquery",
        "redux",
        "react-redux",
        "@material-ui/core/useMediaQuery",
    ],
}

# Byte-identical in run/test/fix. The results file is removed first because the warm run
# in prepare.sh bakes one into the image; a stage whose jest dies early would otherwise
# re-report the baseline results.
_TEST_CMD = (
    "rm -f /tmp/jest-results.json\n"
    "CI=true node_modules/.bin/react-scripts test --watchAll=false --ci --silent "
    "--json --outputFile=/tmp/jest-results.json || true\n"
    "node /home/jest-report.js /tmp/jest-results.json\n"
)

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


class NavidromeImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
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

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class NavidromeImageDefault(Image):
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
        return NavidromeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        extra = _PR_TEST_DEPS.get(self.pr.number, [])
        extra_install = (
            f"npm install --no-save --no-audit --no-fund {' '.join(extra)} || true\n"
            "git checkout -- .\n"
            "bash /home/check_git_changes.sh\n"
            if extra
            else ""
        )
        resolve = _BASE_RESOLVE + _PR_RESOLVE.get(self.pr.number, [])
        resolve_js = ", ".join(f"'{m}'" for m in resolve)

        jest_report_js = _JEST_REPORT_JS.replace("__REPO__", repo)

        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            "cat > /home/jest-report.js <<'__JEST_REPORT_EOF__'\n"
            f"{jest_report_js}"
            "__JEST_REPORT_EOF__\n"
            "\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            f"cd /home/{repo}/ui\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "for attempt in 1 2 3; do\n"
            "    if npm ci --no-audit --no-fund; then break; fi\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            f"{extra_install}"
            "\n"
            f'node -e "for (const m of [{resolve_js}]) console.log(m + \' -> \' + require.resolve(m))"\n'
            "node_modules/.bin/jest --version\n"
            'echo "DEPS_OK"\n'
            "\n"
            f"{_TEST_CMD}"
        )

    def _run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}/ui\n"
            f"{_TEST_CMD}"
        )

    def _test_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "cd ui\n"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "cd ui\n"
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

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("navidrome", "navidrome")
class Navidrome(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NavidromeImageDefault(self.pr, self._config)

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

        # jest-report.js prints one result per line at column 0; jest's own reporter
        # output (stderr, indented failure blocks) is never anchored there.
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
