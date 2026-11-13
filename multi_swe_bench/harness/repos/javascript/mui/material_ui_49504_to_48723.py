import importlib
import re
import shlex
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "mui"
_REPO = "material-ui"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"material_ui_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like material_ui_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_LO}_to_{_ERA_HI}"

NODE_IMAGE = "node:22.23.3-bookworm"

importlib.import_module("multi_swe_bench.harness.repos.typescript.mui")
_OLDER_ERAS = Instance._registry.get(f"{_ORG}/{_REPO}")

_RESULTS_FILE = "/tmp/mswb-vitest.txt"
_REPORTER_FILE = "/home/mswb_vitest_reporter.mjs"

_NODE_TEST_FILE_RE = re.compile(r"^(?:docs|packages(?:-internal)?/[^/]+)/.+\.test\.[cm]?[jt]sx?$")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (\S.*)$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_ENV_EXPORTS = (
    "export CI=true\n"
    "export HUSKY=0\n"
    "export TZ=UTC\n"
    "export TEST_SCOPE=node\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1\n"
    "export npm_config_update_notifier=false\n"
)
_PREPARE_HEADER = f"#!/bin/bash\nset -e\n\n{_ENV_EXPORTS}\n"
_SCRIPT_HEADER = f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}\n"

_VITEST_REPORTER_MJS = r"""import fs from 'node:fs';
import path from 'node:path';

const STATUS = { passed: 'PASSED', failed: 'FAILED', skipped: 'SKIPPED', pending: 'SKIPPED' };
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();

export default class MswbVitestReporter {
  onInit(vitest) {
    this.root = vitest.config.root;
  }

  onTestRunEnd(testModules, unhandledErrors, reason) {
    const lines = [];
    const counts = { PASSED: 0, FAILED: 0, SKIPPED: 0 };
    const seen = new Map();
    const emit = (status, name) => {
      const n = (seen.get(name) || 0) + 1;
      seen.set(name, n);
      counts[status] += 1;
      lines.push('MSWB_RESULT ' + status + ' ' + (n > 1 ? name + ' #' + n : name));
    };
    const modules = [...testModules]
      .map((mod) => ({ mod, file: path.relative(this.root, mod.moduleId).split(path.sep).join('/') }))
      .sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0));
    for (const { mod, file } of modules) {
      for (const test of mod.children.allTests()) {
        emit(STATUS[test.result().state] || 'FAILED', file + ' > ' + clean(test.fullName));
      }
      for (const suite of mod.children.allSuites()) {
        if (suite.errors().length > 0) {
          emit('FAILED', file + ' > ' + clean(suite.fullName) + ' > <suite hook failed>');
        }
      }
      if (mod.errors().length > 0) {
        emit('FAILED', file + ' > <file failed to run>');
        for (const error of mod.errors()) {
          lines.push('MSWB_LOG ' + clean(error.message).slice(0, 300));
        }
      }
    }
    lines.push(
      'MSWB_SUMMARY reason=' + reason + ' files=' + modules.length +
        ' passed=' + counts.PASSED + ' failed=' + counts.FAILED +
        ' skipped=' + counts.SKIPPED + ' unhandled=' + (unhandledErrors || []).length,
    );
    fs.writeFileSync(process.env.MSWB_RESULTS, lines.join('\n') + '\n');
  }
}
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
    files = set()
    for section in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = re.match(r"^diff --git a/\S+ b/(\S+)$", section, re.M)
        if not header or re.search(r"^deleted file mode ", section, re.M):
            continue
        if _NODE_TEST_FILE_RE.match(header.group(1)):
            files.add(header.group(1))
    return sorted(files)


def _test_cmd(pr: PullRequest) -> str:
    declared = " ".join(shlex.quote(path) for path in _test_files(pr))
    return (
        f"declared=({declared})\n"
        'if [ "${#declared[@]}" -eq 0 ]; then\n'
        '    echo "=== MSWB: test.patch adds or changes no vitest node test file ==="\n'
        "    exit 1\n"
        "fi\n"
        "\n"
        "present=()\n"
        'for path in "${declared[@]}"; do\n'
        '    if [ -e "$path" ]; then\n'
        '        present+=("$path")\n'
        "    fi\n"
        "done\n"
        'if [ "${#present[@]}" -eq 0 ]; then\n'
        '    echo "=== MSWB: no test.patch test file exists at this stage ==="\n'
        "    exit 0\n"
        "fi\n"
        "printf '%s\\n' \"${present[@]}\"\n"
        "\n"
        f"rm -f {_RESULTS_FILE}\n"
        "rc=0\n"
        f"MSWB_RESULTS={_RESULTS_FILE} timeout --kill-after=60 3600 ./node_modules/.bin/vitest run "
        f"--reporter=default --reporter={_REPORTER_FILE} --passWithNoTests --maxWorkers=2 "
        '--testTimeout=60000 --hookTimeout=60000 "${present[@]}" < /dev/null || rc=$?\n'
        f"if [ ! -s {_RESULTS_FILE} ]; then\n"
        '    echo "=== MSWB: vitest exited with status $rc without writing results ==="\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        f"cat {_RESULTS_FILE}\n"
        'exit "$rc"\n'
    )


class MaterialUi49504To48723ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        return f"""{enh.SYNTAX_DIRECTIVE}
FROM {self.dependency()}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{enh._CERT_SYMLINKS}

{self.global_env}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

RUN node --version && npm --version && git --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class MaterialUi49504To48723ImageDefault(Image):
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
        return MaterialUi49504To48723ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return (
            f"{_PREPARE_HEADER}"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f'if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then\n'
            f'    git fetch --no-tags origin "{sha}"\n'
            "fi\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "pnpm_version=\"$(node -p \"require('./package.json').packageManager.split('@')[1].split('+')[0]\")\"\n"
            'npm install -g "pnpm@${pnpm_version}"\n'
            "node --version\n"
            'test "$(pnpm --version)" = "${pnpm_version}"\n'
            "\n"
            "for attempt in 1 2 3; do\n"
            "    pnpm install --frozen-lockfile && break || true\n"
            '    echo "prepare: pnpm install attempt ${attempt} failed; retrying in 30s" >&2\n'
            "    sleep 30\n"
            "done\n"
            "test -x node_modules/.bin/vitest\n"
            "\n"
            f"test -s {_REPORTER_FILE}\n"
            "test -f vitest.config.mts\n"
            "./node_modules/.bin/vitest --version\n"
            "node --input-type=module -e \"for (const id of ['vitest', 'jsdom', "
            "'@mui/internal-test-utils', 'react', 'react-dom']) console.log(id + ' -> ' + import.meta.resolve(id))\"\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{_SCRIPT_HEADER}cd /home/{self.pr.repo}\n\n{_test_cmd(self.pr)}"

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}cd /home/{self.pr.repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"\n{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}cd /home/{self.pr.repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"\n{_test_cmd(self.pr)}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswb_vitest_reporter.mjs", _VITEST_REPORTER_MJS),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        hardening = (
            Image._HARDENING_BLOCK.replace(
                "git gc --prune=now --aggressive",
                "git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now",
            )
            .replace(
                "git repack -a -d -l --quiet",
                "git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet",
            )
            .strip("\n")
        )
        return f"""FROM {self.dependency().image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}
"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class MATERIAL_UI_49504_TO_48723(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            if _OLDER_ERAS is None:
                raise ValueError(f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})")
            return _OLDER_ERAS(pr, config, *args, **kwargs)
        return super().__new__(cls)

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MaterialUi49504To48723ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in clean_log.split("\n"):
            match = _RESULT_RE.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
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
