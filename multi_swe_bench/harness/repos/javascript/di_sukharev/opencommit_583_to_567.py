import posixpath
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "di-sukharev"
_REPO = "opencommit"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"opencommit_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like opencommit_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# CI (.github/workflows/test.yml) runs node 20.x; Node 20 reached end of life before
# this era's PRs (Jun-Aug 2026), so the maintained 22 LTS line is used. Every
# engines constraint in package-lock.json (rimraf 6: 20 || >=22) accepts it.
NODE_IMAGE = "node:22.23.4-bookworm"

_TEST_FILE_RE = re.compile(r"\.test\.ts$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_JEST_REPORT_CJS = r"""const fs = require('fs');

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

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED', focused: 'PASSED'};

const suites = (report.testResults || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
for (const suite of suites) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(ROOT)) file = file.slice(ROOT.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    const seen = new Map();
    for (const a of results) {
        let name = [file].concat(a.ancestorTitles || [], [a.title]).join(' > ');
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


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if f.startswith("test/") and _TEST_FILE_RE.search(f)})


def _only_new_test_files(pr: PullRequest) -> bool:
    is_new = {}
    for chunk in re.split(r"^(?=diff --git )", pr.test_patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match and _TEST_FILE_RE.search(match.group(1)):
            is_new[match.group(1)] = re.search(r"^new file mode ", chunk, re.M) is not None
    return bool(is_new) and all(is_new.values())


def _test_candidates(pr: PullRequest) -> str:
    # The test files the test patch touches. When the patch only ADDS test files,
    # none exists at the baseline, so the directories holding them are run whole
    # (the baseline stage then still observes the suite next to the new files).
    files = _test_files(pr)
    if not files or not _only_new_test_files(pr):
        return " ".join(files)
    dirs = " ".join(sorted({posixpath.dirname(f) for f in files}))
    return f"$(find {dirs} -maxdepth 1 -type f -name '*.test.ts' | sort)"


def _needs_build(pr: PullRequest) -> bool:
    # The e2e suites spawn the bundled CLI (out/cli.cjs), so the bundle has to be
    # rebuilt from the patched src/ before they run (package.json test:e2e:* do the same).
    return any(f.startswith("test/e2e/") for f in _test_files(pr))


def _install_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    npm ci && break || true\n"
        '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    build = (
        "# The e2e tests run the bundled CLI: rebuild it from the current src/.\n"
        "npm run build\n"
        "\n"
        if _needs_build(pr)
        else ""
    )
    return (
        "rm -f /tmp/jest-results.json\n"
        "\n"
        f"{build}"
        "# Only the test files the test patch touches; a file the patch adds does not exist\n"
        "# before it is applied, so each stage runs the ones present at that point.\n"
        "files=()\n"
        f"for f in {_test_candidates(pr)}; do\n"
        '    if [ -f "$f" ]; then files+=("$f"); fi\n'
        "done\n"
        'if [ "${#files[@]}" -eq 0 ]; then\n'
        '    echo "no test file of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "rc=0\n"
        "OCO_TEST_SKIP_VERSION_CHECK=true NODE_OPTIONS=--experimental-vm-modules "
        "node_modules/.bin/jest --ci --runInBand --json --outputFile=/tmp/jest-results.json "
        '--runTestsByPath "${files[@]}" || rc=$?\n'
        "node /home/jest-report.cjs /tmp/jest-results.json\n"
        "if [ ! -s /tmp/jest-results.json ]; then\n"
        '    echo "jest exited with code $rc without writing /tmp/jest-results.json" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class OpencommitImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class OpencommitImageDefault(Image):
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
        return OpencommitImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        jest_report = _JEST_REPORT_CJS.replace("__REPO__", repo)

        return (
            f"{_PREPARE_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            f'test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "# Result reporter the run scripts use; written outside the repo.\n"
            "cat > /home/jest-report.cjs <<'__JEST_REPORT_EOF__'\n"
            f"{jest_report}"
            "__JEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The install CI runs (.github/workflows/test.yml: npm ci), from the lockfile\n"
            "# that ships with this commit.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            f"{_install_block()}"
            "\n"
            "# The e2e suites commit into scratch repos; CI configures this identity globally.\n"
            'git config --global user.email "test@example.com"\n'
            'git config --global user.name "Test User"\n'
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, jest and the ESM transform it is configured with, the modules\n"
            "# the setup file and e2e helpers load, esbuild (the e2e build), jest finding\n"
            "# a non-empty test list, and the reporter turning jest JSON into result lines.\n"
            "test -s /home/jest-report.cjs\n"
            "test -x node_modules/.bin/jest\n"
            "node_modules/.bin/jest --version\n"
            "node -e \"for (const m of ['ts-jest', '@jest/globals', 'cli-testing-library', "
            "'esbuild', 'openai', '@anthropic-ai/sdk']) console.log(m + ' -> ' + "
            "require.resolve(m))\"\n"
            "node -e \"require('esbuild').transformSync('let a = 1', {loader: 'ts'})\"\n"
            "count=$(NODE_OPTIONS=--experimental-vm-modules node_modules/.bin/jest --listTests "
            "| wc -l)\n"
            'echo "jest --listTests: $count files"\n'
            'test "$count" -gt 0\n'
            "mkdir -p /tmp/jest-gate\n"
            "printf '%s' '{\"numTotalTestSuites\":1,\"numTotalTests\":1,\"numPassedTests\":1,"
            "\"testResults\":[{\"name\":\"/home/"
            f"{repo}"
            "/gate.test.ts\",\"status\":\"passed\",\"assertionResults\":[{\"ancestorTitles\":"
            "[\"g\"],\"title\":\"gate\",\"status\":\"passed\"}]}]}' > /tmp/jest-gate/r.json\n"
            "node /home/jest-report.cjs /tmp/jest-gate/r.json | tee /tmp/jest-gate/out.txt\n"
            "grep -qx 'PASSED gate.test.ts > g > gate' /tmp/jest-gate/out.txt\n"
            "rm -rf /tmp/jest-gate\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_test_cmd(self.pr)}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_cmd(self.pr)}"
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


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class OPENCOMMIT_583_TO_567(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpencommitImageDefault(self.pr, self._config)

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
