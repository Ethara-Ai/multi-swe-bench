import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

"""

# The tested project lives in the `desktop-app/` workspace of the monorepo;
# `browser-extension/` and `desktop-app-legacy/` carry no jest suites.
_APP_DIR = "desktop-app"

# Shared jest invocation. CI=true keeps jest non-interactive, FORCE_COLOR=0 /
# NO_COLOR=1 strip the ANSI escapes that would otherwise break parse_log, and
# --runInBand avoids the worker-pool OOM ts-jest hits inside the container.
_JEST_ENV = """export CI=true
export FORCE_COLOR=0
export NO_COLOR=1
export NODE_OPTIONS="--max-old-space-size=4096"
"""

_JEST_CMD = "npx jest --config ./jest.config.js --verbose --runInBand 2>&1"

# The three run scripts must surface a failure rather than swallow it: no
# `|| true` and no trailing `exit 0` on the test command. jest exiting non-zero
# in the test stage is the expected f2p signal, and the harness reads the log
# either way -- docker_util.run() never inspects the container exit status and
# build_dataset.py appends `>> /home/<stage>_msb.log 2>&1` to the command.
# pipefail (not bare `set -e`) is required so a failure inside the npx pipeline
# cannot be masked.
_RUN_PREAMBLE = "set -eo pipefail"


class ResponsivelyAppImageBase(Image):
    """Level 1: shared base image (tag ``base``).

    dependency() returns a *string* and this Dockerfile carries no ``# syntax``
    directive, so DockerfileEnhancer engages: it prepends the
    ``# syntax``/ARG(TARGETARCH, REPO_URL, BASE_COMMIT)/proxy-ARG/ENV/LABEL/
    CA-symlink infra block, then rewrites the plain
    ``RUN git clone ... /home/<repo>`` line into the standardized clone +
    ``WORKDIR`` + ``git reset --hard`` + ``git checkout ${BASE_COMMIT}`` +
    history-hardening + ``CMD`` tail. That is why the clone must be the LAST
    instruction emitted here -- anything after it would land below the
    generated ``CMD``.

    node:16 is the pin CI uses for this era (.github/workflows/test.yml ->
    actions/setup-node with node-version: 16), and the full (buildpack-deps)
    variant already ships git, python3 and build-essential, so no apt layer is
    needed. Newer Node majors are not safe: desktop-app/yarn.lock resolves
    jest 28 / ts-jest 28 / electron-builder 23, the toolchain CI green-lights
    on 16.
    """

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
        return "node:16"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ResponsivelyAppImageDefault(Image):
    """Level 2: PR image (tag ``pr-<number>``).

    dependency() is an Image, so DockerfileEnhancer leaves this Dockerfile
    verbatim: ``FROM <base>``, the COPY block for the seven payload files, then
    ``RUN bash /home/prepare.sh`` -- the ideal PR-specific layout.
    """

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
        return ResponsivelyAppImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cd /home/{pr.repo}/{app_dir}

# --ignore-scripts is required, not an optimisation: desktop-app's postinstall
# runs `electron-builder install-app-deps` plus a webpack DLL build, and the
# `electron` package's own install script downloads a ~100MB platform binary.
# None of that is reachable from the jsdom unit tests, and all of it fails or
# stalls in a headless build container.
export CI=true
export HUSKY=0
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export ELECTRON_BUILDER_ALLOW_UNRESOLVED_DEPENDENCIES=true
yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 \\
    || yarn install --ignore-scripts --network-timeout 600000 \\
    || true

# jest.config.js loads .erb/scripts/check-build-exists.ts as a setupFile, which
# throws unless release/app/dist/{{main,renderer}} hold a built bundle. CI gets
# those from `yarn run package` (a full electron-builder run). The unit tests
# never read the bundles, so stub the two files instead. release/app/dist is
# git-ignored (desktop-app/.gitignore), so this keeps `git status` clean.
mkdir -p release/app/dist/main release/app/dist/renderer
touch release/app/dist/main/main.js
touch release/app/dist/renderer/renderer.js

""".format(pr=self.pr, app_dir=_APP_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{preamble}

cd /home/{pr.repo}/{app_dir}
{jest_env}
{jest_cmd}

""".format(
                    pr=self.pr,
                    app_dir=_APP_DIR,
                    preamble=_RUN_PREAMBLE,
                    jest_env=_JEST_ENV,
                    jest_cmd=_JEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
{preamble}

cd /home/{pr.repo}
git checkout -- .
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

cd /home/{pr.repo}/{app_dir}
{jest_env}
{jest_cmd}

""".format(
                    pr=self.pr,
                    app_dir=_APP_DIR,
                    preamble=_RUN_PREAMBLE,
                    jest_env=_JEST_ENV,
                    jest_cmd=_JEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
{preamble}

cd /home/{pr.repo}
git checkout -- .
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn --exclude='**/yarn.lock' --exclude='**/package-lock.json' /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

cd /home/{pr.repo}/{app_dir}
{jest_env}
{jest_cmd}

""".format(
                    pr=self.pr,
                    app_dir=_APP_DIR,
                    preamble=_RUN_PREAMBLE,
                    jest_env=_JEST_ENV,
                    jest_cmd=_JEST_CMD,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("responsively-org", "responsively-app")
class ResponsivelyApp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ResponsivelyAppImageDefault(self.pr, self._config)

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
        """Parse `jest --verbose` output.

        Jest prints one ``PASS``/``FAIL`` header per suite followed by an
        indented tree of ``+``/``x``/``o`` marker rows, so each test is keyed as
        ``<suite path>::<test name>`` to stay unique across suites that reuse an
        ``it()`` title.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        # Trailing "(25 ms)" / "(1.2 s)" timings are non-deterministic -> strip.
        timing_re = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")

        re_pass_suite = re.compile(r"^PASS\s+(\S+)")
        re_fail_suite = re.compile(r"^FAIL\s+(\S+)")
        re_pass_test = re.compile(r"^[✓✔]\s+(.+)$")
        re_fail_test = re.compile(r"^[✕✗✘×]\s+(.+)$")
        re_skip_test = re.compile(r"^[○◌↓]\s+(?:skipped\s+)?(.+)$")

        current_suite: Optional[str] = None

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line).strip()
            if not line:
                continue

            suite = re_pass_suite.match(line) or re_fail_suite.match(line)
            if suite:
                current_suite = suite.group(1)
                continue

            if current_suite is None:
                continue

            for pattern, bucket in (
                (re_pass_test, passed_tests),
                (re_fail_test, failed_tests),
                (re_skip_test, skipped_tests),
            ):
                match = pattern.match(line)
                if match:
                    name = timing_re.sub("", match.group(1)).strip()
                    if name:
                        bucket.add(f"{current_suite}::{name}")
                    break

        # TestResult.__post_init__ raises ValueError if the buckets intersect,
        # which would abort report generation for the whole instance. jest can
        # legitimately emit one name twice -- a retried test (jest.retryTimes),
        # a suite re-run after a watch-mode style reload, or a `✓` row followed
        # by a `✕` row for the same title -- so collapse to a single verdict
        # instead of trusting the log. Failure is the strongest signal, then
        # skip, then pass.
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
