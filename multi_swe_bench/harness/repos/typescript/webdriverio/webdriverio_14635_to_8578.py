import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class WebDriverIOVitestNpmImageBase(Image):
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
        return "node:20"

    def image_tag(self) -> str:
        return "base-vitest-npm"

    def workdir(self) -> str:
        return "base-vitest-npm"

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

ENV WDIO_SKIP_DRIVER_SETUP=1

{code}

{self.clear_env}

"""


class WebDriverIOVitestNpmImageDefault(Image):
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
        return WebDriverIOVitestNpmImageBase(self.pr, self.config)

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
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

npm install -g lerna@6 npm-run-all rimraf || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

rm -f package-lock.json
npm install --legacy-peer-deps || npm install --force || true
npm install shelljs --legacy-peer-deps --no-save || true
npx lerna bootstrap --no-ci --force-local || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npm run setup || true

# Fix EXDEV: Docker overlay cannot rename across layers
# Pre-move got directory using cp+rm (works cross-device)
GOT_PATH="packages/webdriver/node_modules/got"
TMP_GOT_PATH="packages/webdriver/node_modules/tmp_got"
if [ -d "$GOT_PATH" ]; then
    cp -a "$GOT_PATH" "$TMP_GOT_PATH"
    rm -rf "$GOT_PATH"
fi
# Make globalSetup no-op since we already moved got
if [ -f scripts/test/globalSetup.ts ]; then
    printf 'export const setup = async () => {{}}\\nexport const teardown = async () => {{}}\\n' > scripts/test/globalSetup.ts
fi

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
npx vitest --config "$CFG" --run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm run setup || true

# Fix EXDEV: Docker overlay cannot rename across layers
GOT_PATH="packages/webdriver/node_modules/got"
TMP_GOT_PATH="packages/webdriver/node_modules/tmp_got"
if [ -d "$GOT_PATH" ]; then
    cp -a "$GOT_PATH" "$TMP_GOT_PATH"
    rm -rf "$GOT_PATH"
fi
if [ -f scripts/test/globalSetup.ts ]; then
    printf 'export const setup = async () => {{}}\\nexport const teardown = async () => {{}}\\n' > scripts/test/globalSetup.ts
fi

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
npx vitest --config "$CFG" --run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm run setup || true

# Fix EXDEV: Docker overlay cannot rename across layers
GOT_PATH="packages/webdriver/node_modules/got"
TMP_GOT_PATH="packages/webdriver/node_modules/tmp_got"
if [ -d "$GOT_PATH" ]; then
    cp -a "$GOT_PATH" "$TMP_GOT_PATH"
    rm -rf "$GOT_PATH"
fi
if [ -f scripts/test/globalSetup.ts ]; then
    printf 'export const setup = async () => {{}}\\nexport const teardown = async () => {{}}\\n' > scripts/test/globalSetup.ts
fi

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
npx vitest --config "$CFG" --run --reporter=verbose

""".format(pr=self.pr),
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


@Instance.register("webdriverio", "webdriverio_14635_to_8578")
class WebDriverIOVitestNpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebDriverIOVitestNpmImageDefault(self.pr, self._config)

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
        """Parse vitest verbose output into per-test results.

        Verified against real captured vitest output. The verbose reporter emits
        one line per test with the file path first, which makes each id unique
        across the monorepo (Check 4A):

            ✓ packages/foo/tests/bar.test.ts > suite > name
            × packages/foo/tests/bar.test.ts > suite > name

        Check 4B: vitest appends a duration to slow tests -- "... name 304ms",
        "... name 0ms". Left in the id, the SAME test gets a DIFFERENT name when
        its timing drifts between the run/test/fix stages and silently drops out
        of the cross-stage comparison. Timing is stripped from every id.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ALL CSI escapes, not just colour (SGR) ones.
        cleaned_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Require the "file.test.<ext>" path so the bare summary glyphs
        # ("✓ 12 passed") are not mistaken for tests.
        re_pass_individual = re.compile(r"^\s*[✓✔]\s+(\S+\.test\.\w+.*)")
        re_fail_individual = re.compile(r"^\s*[×✕✗✖]\s+(\S+\.test\.\w+.*)")
        # File-level failure header. Capture ONLY the file path token so a
        # load-failure id is clean and identical across stages (vitest appends a
        # " [ ... ]" annotation that must not reach the id).
        re_fail_summary = re.compile(r"^\s*FAIL\s+(\S+\.test\.\w+)")
        re_skip = re.compile(r"^\s*[↓○]\s+(\S+\.test\.\w+.*?)(?:\s*\[skipped\])?$")
        # Trailing vitest duration -- "304ms", "1.5s", "0ms".
        re_timing = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s$")

        def clean(name: str) -> str:
            return re_timing.sub("", name).strip()

        for line in cleaned_log.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue

            m = re_pass_individual.match(line_stripped)
            if m:
                test = clean(m.group(1))
                if test and test not in failed_tests:
                    passed_tests.add(test)
                continue

            m = re_fail_individual.match(line_stripped)
            if m:
                test = clean(m.group(1))
                if test:
                    failed_tests.add(test)
                    if test in passed_tests:
                        passed_tests.remove(test)
                continue

            m = re_fail_summary.match(line_stripped)
            if m:
                test = clean(m.group(1))
                if test:
                    failed_tests.add(test)
                    if test in passed_tests:
                        passed_tests.remove(test)
                continue

            m = re_skip.match(line_stripped)
            if m:
                test = clean(m.group(1))
                if test and test not in passed_tests and test not in failed_tests:
                    skipped_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
