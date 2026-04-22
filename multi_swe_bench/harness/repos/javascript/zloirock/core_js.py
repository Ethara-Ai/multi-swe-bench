import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Era boundary: PRs <= 1036 use lerna + webpack (Era 1)
#               PRs >= 1279 use npm workspaces + qunit CLI (Era 2)
ERA_1_MAX_PR = 1036


class CoreJsImageBase(Image):

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
        return "node:18"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class CoreJsImageDefault(Image):

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
        return CoreJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _is_era1(self) -> bool:
        return self.pr.number <= ERA_1_MAX_PR

    def _prepare_script(self) -> str:
        if self._is_era1():
            # Era 1: lerna monorepo with webpack bundling
            # - puppeteer env vars prevent chromium download failures
            # - --legacy-peer-deps + --ignore-scripts for clean npm install
            # - lerna@4 needed because lerna v7+ removed bootstrap
            # - PATH must include node_modules/.bin for run-p/run-s (npm-run-all)
            # - Individual build steps instead of npm run init/bundle (which use run-p not in PATH)
            # - Install qunit@2.25.0 CLI to get TAP v13 output (original uses node-qunit with table format)
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export PATH="$PWD/node_modules/.bin:$PATH"

npm install --legacy-peer-deps --ignore-scripts || true
npx lerna@4 bootstrap --no-ci || true

zx scripts/generate-indexes.mjs
zx scripts/clean-and-copy.mjs
zx scripts/build-compat-data.mjs
zx scripts/build-compat-entries.mjs
zx scripts/build-compat-modules-by-versions.mjs
zx scripts/bundle.mjs

webpack --entry ./tests/helpers/qunit-helpers.js --output-filename qunit-helpers.js
webpack --entry ./tests/tests/index.js --output-filename tests.js
webpack --entry ./tests/pure/index.js --output-filename pure.js

npm install qunit@2.25.0 --legacy-peer-deps --ignore-scripts || true

""".format(pr=self.pr)
        else:
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true
npm run prepare || true
npm run bundle-tests || true

""".format(pr=self.pr)

    def _run_script(self) -> str:
        if self._is_era1():
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
npx qunit packages/core-js-bundle/index.js tests/bundles/tests.js 2>&1 || true

""".format(pr=self.pr)
        else:
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
npx qunit packages/core-js/index.js tests/bundles/unit-global.js 2>&1 || true

""".format(pr=self.pr)

    def _test_run_script(self) -> str:
        if self._is_era1():
            # Regenerate indexes (scans dirs for new test files) and rebuild webpack bundles
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
git apply --whitespace=nowarn /home/test.patch

zx scripts/generate-indexes.mjs
webpack --entry ./tests/tests/index.js --output-filename tests.js
webpack --entry ./tests/pure/index.js --output-filename pure.js

npx qunit packages/core-js-bundle/index.js tests/bundles/tests.js 2>&1 || true

""".format(pr=self.pr)
        else:
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm run bundle-tests || true

npx qunit packages/core-js/index.js tests/bundles/unit-global.js 2>&1 || true

""".format(pr=self.pr)

    def _fix_run_script(self) -> str:
        if self._is_era1():
            # Full rebuild needed: fix patch may add source modules that need bundling
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
export PATH="$PWD/node_modules/.bin:$PATH"
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

zx scripts/generate-indexes.mjs
zx scripts/clean-and-copy.mjs
zx scripts/bundle.mjs
webpack --entry ./tests/tests/index.js --output-filename tests.js
webpack --entry ./tests/pure/index.js --output-filename pure.js

npx qunit packages/core-js-bundle/index.js tests/bundles/tests.js 2>&1 || true

""".format(pr=self.pr)
        else:
            return """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm run prepare || true
npm run bundle-tests || true

npx qunit packages/core-js/index.js tests/bundles/unit-global.js 2>&1 || true

""".format(pr=self.pr)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                self._prepare_script(),
            ),
            File(
                ".",
                "run.sh",
                self._run_script(),
            ),
            File(
                ".",
                "test-run.sh",
                self._test_run_script(),
            ),
            File(
                ".",
                "fix-run.sh",
                self._fix_run_script(),
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


@Instance.register("zloirock", "core-js")
class CoreJs(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CoreJsImageDefault(self.pr, self._config)

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

        # TAP v13: "ok <N> <name>", "not ok <N> <name>", directives "# SKIP" / "# TODO"
        re_ok = re.compile(r"^ok\s+\d+\s+(.+)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s+(.+)$")
        re_directive = re.compile(r"^(.+?)\s+#\s+(SKIP|TODO)\b.*$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_not_ok.match(line)
            if m:
                test_name = m.group(1).strip()
                d = re_directive.match(test_name)
                if d:
                    skipped_tests.add(d.group(1).strip())
                else:
                    failed_tests.add(test_name)
                continue

            m = re_ok.match(line)
            if m:
                test_name = m.group(1).strip()
                d = re_directive.match(test_name)
                if d:
                    skipped_tests.add(d.group(1).strip())
                else:
                    passed_tests.add(test_name)
                continue

        failed_tests -= skipped_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
