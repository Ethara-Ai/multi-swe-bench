"""playcanvas/engine repo config.

Layout (per-PR base):
  * ImageBase   -- node:24 image, default Image.dockerfile(): clones the engine,
                   pins BASE_COMMIT, then the default hardening block scrubs history.
  * ImageDefault -- copies the patches and scripts and runs prepare.sh
                    (pin check, npm ci, hard gate).

Toolchain (same at both base shas, cc5764d6 for #9401 and dec96866 for #9923):
npm with package-lock.json, mocha unit tests (`npm test`), engines.node >=18.3.
jsdom 30 needs node ^22.22.2 || ^24.15.0, and mocha 12 (the later sha) needs
^20.19 || >=22.12, so node 24 LTS covers both shas. CI used node 24.x, then 26.x.
The native dev deps (canvas, esbuild, webgpu) ship linux-x64 and linux-arm64
prebuilds.

Test command: the repo's own `npm test` script (its mocha flags differ between
shas, e.g. `--node-option expose-gc` was added later, so the script itself is used),
run once per spec file with mocha's TAP reporter:

    npm test --silent -- --reporter tap <file>

The script runs each file on its own. When a test.patch adds a spec that imports a
module only the fix creates, mocha cannot load that file. Run per file, that only
removes that file's tests from the test stage. In a single run the whole suite would
abort, and the test.patch's other tests could not report FAILED.

The TAP reporter prints fullTitle(), so describe context is kept. Every file is
preceded by an `MSWE_FILE <path>` marker, and test names are `<path> > <fullTitle>`.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:24.22.2-bookworm"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export npm_config_audit=false\n"
    "export npm_config_fund=false\n"
    "export npm_config_update_notifier=false\n"
)

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

# Binary files in fix patches have no full index ("Binary files ... differ"); they
# are example assets / thumbnails, never read by the unit tests.
_GIT_APPLY = "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.webp' --exclude='*.jpg'"


def _npm_ci_with_retry() -> str:
    return (
        "ok=0\n"
        "for attempt in 1 2 3; do\n"
        "    if npm ci; then ok=1; break; fi\n"
        '    echo "npm ci attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        'test "$ok" = 1\n'
    )


def _test_block() -> str:
    # Same block in run.sh, test-run.sh and fix-run.sh.
    return (
        "# Re-sync node_modules only if a patch changed the dependency manifests.\n"
        "if ! git diff --quiet HEAD -- package.json package-lock.json; then\n"
        "    echo 'MSWE_LOG dependency manifests changed; running npm ci'\n"
        "    " + _npm_ci_with_retry().rstrip("\n").replace("\n", "\n    ") + "\n"
        "fi\n"
        "\n"
        "mapfile -t SPECS < <(find test -name '*.test.mjs' -not -path 'test/bundles/*' | LC_ALL=C sort)\n"
        'test "${#SPECS[@]}" -gt 0\n'
        'echo "MSWE_LOG ${#SPECS[@]} spec files"\n'
        "rc=0\n"
        'for spec in "${SPECS[@]}"; do\n'
        '    echo "MSWE_FILE $spec"\n'
        '    npm test --silent -- --reporter tap "$spec" || rc=$?\n'
        '    echo "MSWE_FILE_END $spec rc=$rc"\n'
        "done\n"
        "exit $rc\n"
    )


class PlaycanvasEngineImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []


class PlaycanvasEngineImageDefault(Image):
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
        return PlaycanvasEngineImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _header(self) -> str:
        return f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}\ncd /home/{self.pr.repo}\n\n"

    def _prepare_sh(self) -> str:
        sha = self.pr.base.sha
        return (
            f"{self._header()}"
            "# --- Section 1: pin ---\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- Section 2: provision ---\n"
            "node --version\n"
            "npm --version\n"
            f"{_npm_ci_with_retry()}"
            "\n"
            "# --- Section 3: gate ---\n"
            "test -x node_modules/.bin/mocha\n"
            "node -e \"require('canvas').createCanvas(4, 4).getContext('2d'); "
            "require('jsdom'); require('chai'); require('sinon'); require('serve-handler');\"\n"
            "node --input-type=module -e \"const m = await import('./src/core/math/vec3.js'); "
            "if (new m.Vec3(1, 2, 3).length() <= 0) process.exit(1);\"\n"
            "npm test --silent -- --reporter tap test/core/math/vec3.test.mjs > /tmp/gate.log\n"
            "grep -q '^ok 1 ' /tmp/gate.log\n"
            "rm -f /tmp/gate.log\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{self._header()}{_test_block()}"

    def _test_run_sh(self) -> str:
        return f"{self._header()}{_GIT_APPLY} /home/test.patch\n{_test_block()}"

    def _fix_run_sh(self) -> str:
        return (
            f"{self._header()}"
            f"{_GIT_APPLY} /home/test.patch /home/fix.patch\n"
            f"{_test_block()}"
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
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("playcanvas", "engine")
class ENGINE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PlaycanvasEngineImageDefault(self.pr, self._config)

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
        log = log.replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        file_re = re.compile(r"^MSWE_FILE (\S+)\s*$")
        file_end_re = re.compile(r"^MSWE_FILE_END ")
        # mocha TAP reporter: "ok 12 <fullTitle>", "not ok 3 <fullTitle>",
        # "ok 7 <fullTitle> # SKIP -"
        tap_re = re.compile(r"^(not ok|ok) \d+ (.+?)\s*$")
        skip_re = re.compile(r"\s+# SKIP\b.*$")

        spec: Optional[str] = None
        for line in log.splitlines():
            m = file_re.match(line)
            if m:
                spec = m.group(1)
                continue
            if file_end_re.match(line):
                spec = None
                continue
            if spec is None:
                continue
            m = tap_re.match(line)
            if not m:
                continue
            status, title = m.group(1), m.group(2)
            is_skip = bool(skip_re.search(title))
            title = skip_re.sub("", title).strip()
            if not title:
                continue
            name = f"{spec} > {title}"
            if status == "not ok":
                failed_tests.add(name)
            elif is_skip:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
