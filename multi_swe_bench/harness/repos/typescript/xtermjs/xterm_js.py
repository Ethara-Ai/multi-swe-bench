"""xtermjs/xterm.js harness config — Mocha + Chai, npm, TypeScript monorepo (core + addons)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class XtermJsImageBase(Image):
    """Base Docker image: node:22-bookworm with the repo cloned.

    xterm.js requires Node >= 18 (tested on 22) and uses node-pty which
    needs build-essential + python3 for native addon compilation via node-gyp.
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

    def dependency(self) -> Union[str, "Image"]:
        return "node:22-bookworm"

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \\
    git \\
    curl \\
    build-essential \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class XtermJsImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    xterm.js is a TypeScript monorepo (core + addons).  The test pipeline is:

        npm install --legacy-peer-deps    (handles peer dep conflicts on older PRs)
        npm run setup || true             (new PRs: tsc+esbuild; old PRs: may fail)
        npx tsc -b ./tsconfig.all.json || true  (ensures out/ always has test files)
        npm run test-unit                 (mocha via bin/test.js or bin/test_unit.js)
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

    def dependency(self) -> Image:
        return XtermJsImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install dependencies — --ignore-scripts prevents npm's "prepare" lifecycle hook
# from running "npm run setup" during install (which fails on mid-era PRs due to
# addon webpack/ajv issues). --legacy-peer-deps handles peer conflicts on older PRs.
npm install --legacy-peer-deps --ignore-scripts

# Rebuild node-pty native addon (node-gyp) — --ignore-scripts above skips ALL
# lifecycle hooks including node-pty's install hook that compiles pty.node.
# Without this, Terminal2.test.ts crashes mocha at load time (MODULE_NOT_FOUND).
npm rebuild node-pty || true

# Build: npm run setup works for new PRs (tsc+esbuild). For old/mid PRs the
# presetup hook (install-addons.js) may fail before tsc runs, so we follow up
# with an explicit tsc -b to ensure out/ always contains compiled test files.
npm run setup || true
npx tsc -b ./tsconfig.all.json || true
npm run esbuild || true

# jsdom shim: some mid-era PRs compile browser-dependent test files that reference
# document/window at module top level, crashing mocha in Node. This provides the
# required browser globals so mocha can load all test files.
cat > /home/{repo}/jsdom-setup.js << 'JSDOM_EOF'
const {{ JSDOM }} = require("jsdom");
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {{ url: "http://localhost", pretendToBeVisual: true }});
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.HTMLCanvasElement = dom.window.HTMLCanvasElement;
JSDOM_EOF
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

NODE_OPTIONS="--require /home/{repo}/jsdom-setup.js" npm run test-unit 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --exclude yarn.lock --exclude package-lock.json --whitespace=nowarn /home/test.patch

npm run setup || true
npx tsc -b ./tsconfig.all.json || true
npm run esbuild || true

NODE_OPTIONS="--require /home/{repo}/jsdom-setup.js" npm run test-unit 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --exclude yarn.lock --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch

npm install --legacy-peer-deps --ignore-scripts
npm rebuild node-pty || true

npm run setup || true
npx tsc -b ./tsconfig.all.json || true
npm run esbuild || true

NODE_OPTIONS="--require /home/{repo}/jsdom-setup.js" npm run test-unit 2>&1
""".format(repo=self.pr.repo),
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


@Instance.register("xtermjs", "xterm.js")
class XTERMJS_XTERM_JS(Instance):
    """Harness instance for xtermjs/xterm.js — Mocha + Chai unit tests."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return XtermJsImageDefault(self.pr, self._config)

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
        """Parse Mocha spec reporter output into pass/fail/skip sets.

        Mocha spec reporter output looks like:

            Terminal
              constructor
                \u2713 should open with correct size (234ms)
                \u2714 should handle resize
                - should be skipped (pending)

            2807 passing (15s)
            1 failing

            1) Terminal > constructor
                 should fail on bad input:
               Error: expected X to equal Y
                at Context.<anonymous> (file.js:40:96)

        In non-TTY / Docker environments the checkmark may appear as
        different unicode glyphs (\u2713 vs \u2714).  The indented structure groups
        tests under describe() blocks.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        # Mocha test result markers (spec reporter)
        # Passed: \u2713 or \u2714 followed by test name, optional (Nms) duration
        re_pass = re.compile(
            r"^(\s+)[\u2713\u2714]\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )
        # Failed: N) test name  (mocha lists failures with a number prefix
        # in the failure detail section at the bottom of output)
        re_fail_numbered = re.compile(
            r"^\s+(\d+)\)\s+(.+?)\s*$"
        )
        # Skipped/pending: - test name (Mocha uses a dash for pending tests)
        re_skip = re.compile(
            r"^(\s+)-\s+(.+?)(?:\s+\(\d+m?s\))?\s*$"
        )

        # Summary line patterns (for detecting failure list section)
        re_summary_failing = re.compile(r"^\s*(\d+)\s+failing\b")

        # Track whether we're in the numbered failure list section
        in_failure_list = False
        failure_list_tests: set[str] = set()

        for line in clean_log.splitlines():
            # Detect start of Mocha's numbered failure list
            m = re_summary_failing.match(line)
            if m:
                in_failure_list = True
                continue

            # Passed test
            m = re_pass.match(line)
            if m:
                test_name = m.group(2).strip()
                passed_tests.add(test_name)
                in_failure_list = False
                continue

            # Skipped/pending test
            m = re_skip.match(line)
            if m:
                test_name = m.group(2).strip()
                skipped_tests.add(test_name)
                in_failure_list = False
                continue

            # Numbered failure (from the failure detail list at bottom)
            m = re_fail_numbered.match(line)
            if m:
                test_name = m.group(2).strip()
                failure_list_tests.add(test_name)
                continue

        # Merge failure list into failed_tests
        failed_tests.update(failure_list_tests)

        # Deduplicate: a test in the failure list should not be in passed/skipped
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
