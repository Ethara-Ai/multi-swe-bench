import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# vuejs/vue has two distinct toolchain eras (Docker-verified 2026-05-18 on arm64):
#
#   Era A — Vue 2.1.x .. 2.6.x  (PRs <= 12455, release_line 2.1..2.6)
#     test:unit = `karma start <build|test/unit>/karma.unit.config.js`
#     Karma + Jasmine, runs in a real browser. Built with webpack 1.x..4.x.
#     Run headlessly with system Chromium via a generated karma override that
#     also swaps in a flat, fully-qualified custom reporter.
#
#   Era C — Vue 2.7.x  (PRs > 12455, release_line 2.7)
#     test:unit = `vitest run test/unit`, package manager pnpm (via corepack).
#
# Boundary 12455: no dataset PRs fall in (11906, 12563); kept for continuity.
# The dataset JSONL has no `number_interval`, so the harness routes every
# record by `vuejs/vue`; a single registration with PR-number-conditional
# dependency is therefore required (multi-file/number_interval would leave
# every record unroutable). Each era still has its own ImageBase/ImageDefault
# and a unique image_tag, and parse_log handles both reporters.
#
# Docker-verified end-to-end (run -> test-run -> fix-run, valid f2p):
#   Era A: v2.1.8(790) v2.3.0(892) v2.5.0(1035) v2.5.11(1070) v2.6.13(1292);
#          PR 11906 f2p: "Component slot > v-if inside scoped slot"
#          absent -> TESTFAIL -> TESTPASS.
#   Era C: v2.7.0(1417) v2.7.15(ok); PR 12563 f2p: 2 reactivity specs
#          NONE -> fail -> pass.

# Headless-Chrome karma override placed *beside* the repo's own
# karma.unit.config.js so the base config's relative `./index.js` /
# `./karma.base.config.js` patterns resolve identically. Notes:
#  * custom launcher uses base "Chrome" + explicit --headless so it works with
#    old karma-chrome-launcher 2.0.0 (Vue 2.1) and 3.x (Vue 2.6) alike;
#  * the plugin list is pinned so karma does NOT auto-discover the broken
#    karma-phantomjs-launcher (phantomjs has no arm64 binary) — these package
#    names exist throughout Vue 2.1.x..2.6.x devDependencies;
#  * an inline reporter emits one flat line per spec with a fully-qualified
#    name (suite path + description) and an unambiguous TESTPASS/TESTFAIL/
#    TESTSKIP marker. This avoids karma-mocha-reporter's leaf-only name
#    collisions, indentation scraping and summary-line ambiguity, and keeps
#    names identical across run/test/fix stages. Stable across karma 1.x-6.x.
KARMA_HEADLESS_JS = r"""const base = require("./karma.base.config.js")

function FlatReporter(baseReporterDecorator) {
  baseReporterDecorator(this)
  const self = this
  this.onSpecComplete = function (browser, result) {
    const suite = (result.suite || []).join(" > ")
    const name = suite ? suite + " > " + result.description : result.description
    const tag = result.skipped ? "TESTSKIP" : (result.success ? "TESTPASS" : "TESTFAIL")
    self.write(tag + " " + name + "\n")
  }
}
FlatReporter.$inject = ["baseReporterDecorator"]

module.exports = function (config) {
  config.set(Object.assign({}, base, {
    browsers: ["ChromeHeadlessCI"],
    customLaunchers: {
      ChromeHeadlessCI: {
        base: "Chrome",
        flags: [
          "--headless",
          "--no-sandbox",
          "--disable-gpu",
          "--disable-dev-shm-usage",
          "--remote-debugging-port=9222"
        ]
      }
    },
    reporters: ["flat"],
    singleRun: true,
    plugins: [
      "karma-jasmine",
      "karma-webpack",
      "karma-sourcemap-loader",
      "karma-chrome-launcher",
      { "reporter:flat": ["type", FlatReporter] }
    ]
  }))
}
"""


class ImageBaseKarma(Image):
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
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base-karma"

    def workdir(self) -> str:
        return "base-karma"

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

RUN apt-get update && apt-get install -y git jq chromium

{self.clear_env}

"""


class ImageDefaultKarma(Image):
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
        return ImageBaseKarma(self.pr, self._config)

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
                "karma.headless.js",
                KARMA_HEADLESS_JS,
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Place the headless-Chrome karma override beside the repo's unit config so
# the base config's relative paths resolve. Vue 2.1-2.3 keep it in build/,
# Vue 2.4+ in test/unit/.
if [ -f test/unit/karma.unit.config.js ]; then
  cp /home/karma.headless.js test/unit/karma.headless.js
else
  cp /home/karma.headless.js build/karma.headless.js
fi

# phantomjs-prebuilt / puppeteer have no arm64 binary and are unused here
# (we drive Chromium directly), so skip lifecycle scripts and tolerate failure.
export PUPPETEER_SKIP_DOWNLOAD=true
yarn install --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/bin/chromium

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f test/unit/karma.unit.config.js ]; then KCFG=test/unit/karma.headless.js; else KCFG=build/karma.headless.js; fi
node_modules/.bin/karma start "$KCFG"

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


class ImageBaseVitest(Image):
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base-vitest"

    def workdir(self) -> str:
        return "base-vitest"

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

RUN apt-get update && apt-get install -y git jq
RUN corepack enable

{self.clear_env}

"""


class ImageDefaultVitest(Image):
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
        return ImageBaseVitest(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# corepack resolves the pnpm version from package.json packageManager
# (pnpm@7.1.0 for v2.7.0-2.7.14, pnpm@8.9.2 for v2.7.15+).
corepack enable
# puppeteer chromium has no arm64 binary and is e2e-only; skip + tolerate.
export PUPPETEER_SKIP_DOWNLOAD=true
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
pnpm exec vitest run test/unit --reporter verbose

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
pnpm exec vitest run test/unit --reporter verbose

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
pnpm exec vitest run test/unit --reporter verbose

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


@Instance.register("vuejs", "vue")
class Vue(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        # Era A: Vue 2.1-2.6 (Karma/Jasmine, headless Chromium)
        # Era C: Vue 2.7   (Vitest + pnpm)
        if self.pr.number <= 12455:
            return ImageDefaultKarma(self.pr, self._config)

        return ImageDefaultVitest(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI/colour first — karma + webpack emit coloured output.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Era A: custom karma reporter -> "TESTPASS|TESTFAIL|TESTSKIP <name>"
        #   where <name> is the fully-qualified "suite > ... > description".
        re_marker = re.compile(r"^(TESTPASS|TESTFAIL|TESTSKIP) (.+?)\s*$")
        # Era C: vitest --reporter verbose -> "√|✓ <name>" pass, "× <name>"
        #   fail, "↓ <name>" skipped; <name> = "file.spec.ts > suite > test".
        re_pass = re.compile(r"^[√✓] (.+?)\s*$")
        re_fail = re.compile(r"^× (.+?)\s*$")
        re_skip = re.compile(r"^↓ (.+?)\s*$")
        # Guard against any reporter summary line being captured as a name.
        re_summary = re.compile(
            r"^\d+\s+(passed|failed|skipped)|tests?\s+(completed|passed|failed)"
        )

        for line in log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_marker.match(line)
            if m:
                tag, name = m.group(1), m.group(2)
                if tag == "TESTPASS":
                    passed_tests.add(name)
                elif tag == "TESTFAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            m = re_pass.match(line)
            if m and not re_summary.match(m.group(1)):
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m and not re_summary.match(m.group(1)):
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m and not re_summary.match(m.group(1)):
                skipped_tests.add(m.group(1))

        # Enforce TestResult disjoint-set invariants (a test reported both
        # pass and fail — e.g. flaky/retried — counts as failed).
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
