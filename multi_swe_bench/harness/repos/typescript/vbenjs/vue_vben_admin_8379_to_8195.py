import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "vbenjs"
_REPO = "vue-vben-admin"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"vue_vben_admin_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like vue_vben_admin_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# .node-version at every base commit of this era is 24.16.0 (engines: ^22.18.0 || ^24.12.0).
NODE_IMAGE = "node:24.16.0-bookworm"
# package.json "packageManager": "pnpm@11.16.0" at every base commit of this era.
PNPM_VERSION = "11.16.0"
# The repo's .npmrc points at registry.npmmirror.com; install from the public registry instead.
NPM_REGISTRY = "https://registry.npmjs.org/"

# Only real vitest test files; *.benchmark.ts and fixtures are timing-based / not tests.
_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.(?:js|jsx|mjs|cjs|ts|tsx|mts|cts)$")

_SCRIPT_HEADER = (
    "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n"
    'export NODE_OPTIONS="--max-old-space-size=4096"\n\n'
)
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_VITEST_REPORT_CJS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('vitest-report: no results file at ' + path + ' -- vitest produced no output');
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('vitest-report: could not parse ' + path + ': ' + e.message);
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
    const seen = new Map();
    for (const a of results) {
        let name = [file].concat(a.ancestorTitles || [], [a.title]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + name);
    }
}

console.log('vitest-report: suites=' + (report.numTotalTestSuites || 0) +
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
    return sorted({f for f in files if _TEST_FILE_RE.search(f)})


def _test_cmd(pr: PullRequest) -> str:
    files = " ".join(_test_files(pr))
    return (
        "rm -f /tmp/vitest-results.json\n"
        "\n"
        "# Only the vitest test files the test patch touches (benchmarks excluded); a file the\n"
        "# patch adds does not exist before it is applied, so each stage runs the ones present.\n"
        "test_files=()\n"
        f"for f in {files}; do\n"
        '    if [ -f "$f" ]; then test_files+=("$f"); fi\n'
        "done\n"
        "\n"
        'if [ "${#test_files[@]}" -eq 0 ]; then\n'
        '    echo "no test file of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "rc=0\n"
        "node_modules/.bin/vitest run --reporter=json --outputFile=/tmp/vitest-results.json "
        '"${test_files[@]}" || rc=$?\n'
        "node /home/vitest-report.cjs /tmp/vitest-results.json\n"
        "if [ ! -s /tmp/vitest-results.json ]; then\n"
        '    echo "vitest exited with code $rc without writing /tmp/vitest-results.json" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class VueVbenAdminImageBase(Image):
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

# Shard-wide toolchain: the pnpm version every base commit of this era pins.
RUN npm install -g pnpm@{PNPM_VERSION} --no-fund --no-audit && pnpm --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class VueVbenAdminImageDefault(Image):
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
        return VueVbenAdminImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        vitest_report = _VITEST_REPORT_CJS.replace("__REPO__", repo)

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
            "cat > /home/vitest-report.cjs <<'__VITEST_REPORT_EOF__'\n"
            f"{vitest_report}"
            "__VITEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "pnpm --version\n"
            "\n"
            "# Frozen install from this commit's pnpm-lock.yaml; postinstall runs the workspace\n"
            "# `stub` builds the tests import. CI=true makes the lefthook `prepare` hook a no-op.\n"
            "for attempt in 1 2 3; do\n"
            f"    pnpm install --frozen-lockfile --config.registry={NPM_REGISTRY} && break || true\n"
            '    echo "prepare: pnpm install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            "\n"
            "# --- 3. gate ---\n"
            "# Reporter present, vitest runnable, the modules vitest.config.ts and the tests load\n"
            "# resolvable, and vitest collecting a non-empty file list for the form-ui package.\n"
            "test -s /home/vitest-report.cjs\n"
            "test -x node_modules/.bin/vitest\n"
            "node --input-type=module -e \"for (const m of ['vitest', 'vitest/config', "
            "'@vitejs/plugin-vue', '@vitejs/plugin-vue-jsx', 'happy-dom', 'vue']) "
            "console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
            "# rolldown >= 1.1 (vite 8.1+) segfaults under QEMU user-mode emulation, so an\n"
            "# amd64 image cross-built on an arm64 host cannot execute vitest at build time.\n"
            "# Detect that case (x86_64 userland over an ARM host CPU) and check the native\n"
            "# binding is installed instead; native arm64/amd64 builds run the full gate.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping vitest execution"\n'
            "    ls node_modules/.pnpm/@rolldown+binding-linux-x64-gnu@*/node_modules/@rolldown/binding-linux-x64-gnu/*.node\n"
            "else\n"
            "    node_modules/.bin/vitest --version\n"
            "    n=$(node_modules/.bin/vitest list --filesOnly packages/@core/ui-kit/form-ui/__tests__ | wc -l)\n"
            '    echo "vitest list --filesOnly packages/@core/ui-kit/form-ui/__tests__: $n files"\n'
            '    test "$n" -gt 0\n'
            "fi\n"
            'echo "DEPS_OK"\n'
        )

    def _reset(self) -> str:
        # Idempotent on an already-provisioned container: drop any earlier patch, including
        # files a previous test.patch added (untracked, not ignored). node_modules is ignored
        # and therefore kept.
        return "git reset --hard\ngit clean -fdq\n"

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{self._reset()}"
            f"{_test_cmd(self.pr)}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{self._reset()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{self._reset()}"
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
class VUE_VBEN_ADMIN_8379_TO_8195(Instance):
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
        return VueVbenAdminImageDefault(self.pr, self._config)

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
