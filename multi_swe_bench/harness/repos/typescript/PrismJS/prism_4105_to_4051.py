import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "PrismJS"
_REPO = "prism"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"prism_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like prism_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# v2 source tree (ESM, src/ + tests/ importing src/ directly, mocha + npm lockfile).
# engines.node ">=18"; CI matrix 20.x/22.x/24.x; mocha 10/11 need node >=18.18.
NODE_IMAGE = "node:22.23.4-bookworm"

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# Spec files mocha loads directly (tests/run.js drives the .test fixtures and is
# always run). Helpers, snapshots, type files and fixtures are not spec files.
_SPEC_FILE_RE = re.compile(r"^tests/(?!helper/|languages/)(?:[^/]+/)*[^/]+\.js$")

# Mocha reporter: one `STATUS <file> > <suite...> > <title>` line per test. The
# language fixtures all come from tests/run.js, whose suite title names the
# fixture directory ("Testing language 'markdown!+diff+javascript'") and whose
# test title names the fixture file, so each .test file gets one stable name.
_MOCHA_REPORT_CJS = r"""'use strict';

const path = require('path');

const ROOT = '/home/__REPO__/';

function rel(file) {
    let f = String(file || '<unknown file>').replace(/\\/g, '/');
    if (f.startsWith('file://')) f = f.slice('file://'.length);
    return f.startsWith(ROOT) ? f.slice(ROOT.length) : f;
}

function fileOf(runnable) {
    for (let r = runnable; r; r = r.parent) {
        if (r.file) return rel(r.file);
    }
    return '<unknown file>';
}

function MochaReport(runner) {
    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;

    function emit(status, runnable) {
        let name = [fileOf(runnable)].concat(runnable.titlePath()).join(' > ');
        name = name.replace(/\s+/g, ' ').trim();
        const key = status + ' ' + name;
        const n = (seen.get(key) || 0) + 1;
        seen.set(key, n);
        if (n > 1) name += ' #' + n;
        if (status === 'PASSED') passed++;
        else if (status === 'FAILED') failed++;
        else skipped++;
        console.log(status + ' ' + name);
    }

    runner.on('pass', test => emit('PASSED', test));
    runner.on('pending', test => emit('SKIPPED', test));
    runner.on('fail', (test, err) => {
        emit('FAILED', test);
        const msg = String((err && err.message) || err).split('\n').slice(0, 6).join('\n    ');
        console.log('    ! ' + msg);
    });
    runner.once('end', () => {
        console.log('mocha-report: passed=' + passed + ' failed=' + failed + ' skipped=' + skipped);
    });
}

module.exports = MochaReport;
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


def _spec_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if _SPEC_FILE_RE.match(f) and f != "tests/run.js"})


def _test_cmd(pr: PullRequest) -> str:
    specs = " ".join(_spec_files(pr))
    return (
        "rm -f /tmp/mocha-out.txt\n"
        "\n"
        "# tests/run.js runs every language fixture under tests/languages (the .test\n"
        "# files the test patch adds or edits included). Next to it, the spec files the\n"
        "# test patch touches; one the patch adds or deletes runs only where it exists.\n"
        "specs=(tests/run.js)\n"
        f"for f in {specs}; do\n"
        '    if [ -f "$f" ]; then specs+=("$f"); fi\n'
        "done\n"
        "\n"
        "rc=0\n"
        "node_modules/.bin/mocha --reporter /home/mocha-report.cjs \"${specs[@]}\" 2>&1 "
        "| tee /tmp/mocha-out.txt || rc=$?\n"
        "if ! grep -qE '^(PASSED|FAILED|SKIPPED) ' /tmp/mocha-out.txt; then\n"
        '    echo "mocha exited with code $rc without reporting any test" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class PrismImageBase(Image):
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


class PrismImageDefault(Image):
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
        return PrismImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        mocha_report = _MOCHA_REPORT_CJS.replace("__REPO__", repo)

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
            "cat > /home/mocha-report.cjs <<'__MOCHA_REPORT_EOF__'\n"
            f"{mocha_report}"
            "__MOCHA_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The install CI runs (.github/workflows/test.yml: npm ci, then npm test).\n"
            "# The tests import src/ directly, so no build step is needed.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            "for attempt in 1 2 3; do\n"
            "    npm ci && break || true\n"
            '    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, mocha, the modules the test helpers load (jsdom, chai, the\n"
            "# snapshot plugin, yargs, the regex analysers), the fixture corpus, and the\n"
            "# reporter loading under mocha and reporting a passing test.\n"
            "test -s /home/mocha-report.cjs\n"
            "test -x node_modules/.bin/mocha\n"
            "node_modules/.bin/mocha --version\n"
            "node --input-type=module -e \"for (const m of ['mocha', 'chai', 'jsdom', "
            "'mocha-chai-jest-snapshot', 'yargs', 'regexpp', 'refa', 'scslre', 'prettier']) "
            "console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
            "fixtures=$(find tests/languages -name '*.test' | wc -l)\n"
            'echo "language fixtures: $fixtures"\n'
            'test "$fixtures" -gt 0\n'
            "mkdir -p /tmp/mocha-gate\n"
            "printf \"describe('gate', () => { it('works', () => {}) })\\n\" "
            "> /tmp/mocha-gate/gate.spec.cjs\n"
            "node_modules/.bin/mocha --reporter /home/mocha-report.cjs "
            "/tmp/mocha-gate/gate.spec.cjs | tee /tmp/mocha-gate/out.txt\n"
            "grep -q '^PASSED .* > gate > works$' /tmp/mocha-gate/out.txt\n"
            "rm -rf /tmp/mocha-gate\n"
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
            "git clean -fdq\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fdq\n"
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


@Instance.register(_ORG, _ERA_FILE)
class PRISM_4105_TO_4051(Instance):
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
        return PrismImageDefault(self.pr, self._config)

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
