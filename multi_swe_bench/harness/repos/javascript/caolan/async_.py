import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        # Tagged `base-pr-<number>` rather than a shared `base`. DockerfileEnhancer
        # bakes one BASE_COMMIT into this image AND scrubs every other ref
        # (`test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"`),
        # so a shared tag keeps whichever PR built it first and the next PR's
        # `git checkout <its own sha>` in prepare.sh dies on a missing object.
        # Also what the Dockerfile QC contract requires the PR layer to inherit.
        # Costs one base image per PR instead of one per repo; deliberate.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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
                "msb-reporter.js",
                r"""'use strict';
// Mocha reporter that emits one stable, file-qualified id per test.
//
// Mocha's spec reporter prints only suite/test titles, so ids built from it carry
// no file path and collide across files. Mocha 6's own json reporter drops the
// `file` field entirely (verified: its test objects expose only title, fullTitle,
// duration, currentRetry, err). The Runnable DOES carry `.file`, so a custom
// reporter is the only way to reach it in one run.
//
// Emitted shape matches the pytest-style node id used across this repo, e.g.
//   test/priorityQueue.js::priorityQueue::pushAsync
// Levels come from titlePath() joined with '::' rather than fullTitle(), which
// space-joins them ambiguously.
//
// Results are buffered and flushed between markers at EVENT_RUN_END so that test
// stdout (this suite prints 'foo', 'drain', ...) cannot interleave with them.
const Mocha = require('mocha');
const path = require('path');
const {
  EVENT_TEST_PASS,
  EVENT_TEST_FAIL,
  EVENT_TEST_PENDING,
  EVENT_RUN_END,
} = Mocha.Runner.constants;

module.exports = function MsbReporter(runner) {
  const root = process.cwd();
  const lines = [];
  const record = (test, status) => {
    let id;
    try {
      id = test.titlePath().join('::');
    } catch (e) {
      id = test.fullTitle();
    }
    const file = test.file ? path.relative(root, test.file) : '';
    lines.push(status + '\t' + (file ? file + '::' + id : id));
  };
  runner.on(EVENT_TEST_PASS, (t) => record(t, 'PASS'));
  runner.on(EVENT_TEST_FAIL, (t) => record(t, 'FAIL'));
  runner.on(EVENT_TEST_PENDING, (t) => record(t, 'SKIP'));
  runner.once(EVENT_RUN_END, () => {
    process.stdout.write('\n===== BEGIN MSB TEST RESULTS =====\n');
    for (const line of lines) process.stdout.write('MSB_TEST\t' + line + '\n');
    process.stdout.write('===== END MSB TEST RESULTS =====\n');
  });
};
""",
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

# Install deps WITHOUT touching the tracked manifests.
#
# `npm ci` cannot be used: at this base commit package.json and package-lock.json
# are out of sync, so it aborts with EUSAGE and installs nothing. That is why the
# original script leaned on `npm install eslint --save-dev` to populate
# node_modules -- which worked, but rewrote package.json + package-lock.json AFTER
# the clean-tree assertion, shipped a dirty worktree, and forced the run scripts to
# paper over it with `git apply --exclude package.json`.
#
# `--no-save` keeps package.json untouched and `--no-package-lock` keeps the
# lockfile untouched, so the full dependency tree installs and the worktree stays
# byte-identical to the base commit. Verified: 777 packages, mocha present,
# `git status --porcelain` empty, suite runs 679 passing.
npm install --no-save --no-package-lock || true

# The clean-tree assert must be the LAST thing that runs, or it proves nothing.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cp /home/msb-reporter.js /home/{pr.repo}/msb-reporter.js
# mocha 6 refuses an absolute --reporter path ("Unknown \"reporter\""); it resolves
# custom reporters via require() from cwd, so the file must sit in the repo and be
# named relatively. It is untracked, so it does not disturb `git apply`.
#
# `npm run mocha-node-test` (mocha) is used instead of `npm test`, which is
# `npm run lint && npm run mocha-node-test`. Under `set -e` an eslint failure would
# abort before mocha ran and the stage would score as zero tests -- a lint nit must
# never be able to mask the test outcome. `|| true` keeps a non-zero mocha exit
# (expected at the test stage) from killing the script; the verdict is read from the
# MSB_TEST lines, not the exit code.
npm run mocha-node-test -- --reporter ./msb-reporter.js || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cp /home/msb-reporter.js /home/{pr.repo}/msb-reporter.js
# mocha 6 refuses an absolute --reporter path ("Unknown \"reporter\""); it resolves
# custom reporters via require() from cwd, so the file must sit in the repo and be
# named relatively. It is untracked, so it does not disturb `git apply`.
#
# `npm run mocha-node-test` (mocha) is used instead of `npm test`, which is
# `npm run lint && npm run mocha-node-test`. Under `set -e` an eslint failure would
# abort before mocha ran and the stage would score as zero tests -- a lint nit must
# never be able to mask the test outcome. `|| true` keeps a non-zero mocha exit
# (expected at the test stage) from killing the script; the verdict is read from the
# MSB_TEST lines, not the exit code.
npm run mocha-node-test -- --reporter ./msb-reporter.js || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cp /home/msb-reporter.js /home/{pr.repo}/msb-reporter.js
# mocha 6 refuses an absolute --reporter path ("Unknown \"reporter\""); it resolves
# custom reporters via require() from cwd, so the file must sit in the repo and be
# named relatively. It is untracked, so it does not disturb `git apply`.
#
# `npm run mocha-node-test` (mocha) is used instead of `npm test`, which is
# `npm run lint && npm run mocha-node-test`. Under `set -e` an eslint failure would
# abort before mocha ran and the stage would score as zero tests -- a lint nit must
# never be able to mask the test outcome. `|| true` keeps a non-zero mocha exit
# (expected at the test stage) from killing the script; the verdict is read from the
# MSB_TEST lines, not the exit code.
npm run mocha-node-test -- --reporter ./msb-reporter.js || true
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


class ImageDefault1234(Image):
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
        return ImageBase(self.pr, self._config)

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

npm install || true
npm install eslint --save-dev || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm test -- --verbose  

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package.json --whitespace=nowarn /home/test.patch
npm test -- --verbose  

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm test -- --verbose 

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


@Instance.register("caolan", "async")
class Async(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 1234:
            return ImageDefault1234(self.pr, self._config)

        return ImageDefault(self.pr, self._config)

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

        # Preferred path: the msb-reporter emits one `MSB_TEST\t<STATUS>\t<id>` line
        # per test, where <id> is the pytest-style node id `file::suite::test`. It is
        # exact (mocha hands us the verdict directly), immune to this suite's console
        # output, and carries the file path that the spec reporter never prints.
        #
        # The block below is kept as a fallback so logs captured before the reporter
        # existed -- and the <=1234 image, which still runs the plain spec reporter --
        # continue to parse.
        msb_re = re.compile(r"^MSB_TEST\t(PASS|FAIL|SKIP)\t(.+)$", re.MULTILINE)
        msb_matches = msb_re.findall(test_log)
        if msb_matches:
            for status, name in msb_matches:
                name = name.strip()
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "FAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
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

        if self.pr.number <= 1142:
            # 小于等于1142的PR，加上以下逻辑
            pass_pattern1 = re.compile(r"^✔\s+(.+)$", re.MULTILINE)
            for match in pass_pattern1.finditer(test_log):
                test_name = match.group(1).strip()
                passed_tests.add(f"test-async.js:{test_name}")

            fail_pattern1 = re.compile(r"\[31m✖\s+(.+?)(?:\[39m|$)", re.MULTILINE)
            for match in fail_pattern1.finditer(test_log):
                test_name = match.group(1).strip()
                failed_tests.add(f"test-async.js:{test_name}")

        # Mocha's spec reporter is parsed structurally: 2-space indent per nesting
        # level, tests marked "✓" (pass), "N)" (fail) or "-" (pending).
        #
        # ANSI is stripped defensively. Mocha disables colour on a non-TTY, so the
        # captured logs are currently clean, but a stage run under a TTY would
        # otherwise silently stop matching every marker and report zero tests.
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        lines = ansi_escape.sub("", test_log).splitlines()

        # Keep track of the nesting level structure
        current_path = []
        indentation_to_level = {}

        # Everything after "N failing" is mocha's failure REPORT, not the run. It
        # repeats each failure as a multi-line suite/test/error block, and parsing it
        # as if it were live output invents failed entries from suite headers -- that
        # is what turned this repo's 4 real failures into 6. Stop at the summary.
        re_summary = re.compile(r"^\s*\d+\s+(passing|failing|pending)\b")

        for line in lines:
            if re_summary.match(line):
                break

            # Match test lines - including both group headers and test cases
            # Group 1: indentation spaces
            # Group 2: optional status indicator (✓, number+) or -)
            # Group 3: test name (without timing info)
            match = re.match(
                r"^(\s*)(?:(✓|[0-9]+\)|-)\s+)?(.*?)(?:\s+\([0-9]+m?s\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            # A line with no status marker is only a suite header if it sits where
            # mocha puts one: indented at least two spaces, on the 2-space grid. This
            # repo's own tests print to stdout (test/consoleFunctions.js emits 'foo'
            # and foo at column 0), and treating those as root suites prefixed every
            # subsequent test with "foo:" -- corrupting names, colliding two of them
            # in the result set, and making the f2p comparison depend on where console
            # output happened to land.
            if not status and (indent < 2 or indent % 2 != 0 or "\t" in spaces):
                continue

            # Determine the level in the hierarchy based on indentation
            # First time we see this indentation, assign it a level
            if indent not in indentation_to_level:
                if not indentation_to_level:  # First item
                    indentation_to_level[indent] = 0
                else:
                    # Find the closest smaller indentation
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        # If no smaller indentation, this must be a root level
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]

            # Update the current path based on level
            current_path = current_path[:level]
            current_path.append(name)

            # Only add to passed/failed sets if this is an actual test (has status indicator)
            if status:
                full_path = ":".join(current_path)
                if status == "✓":
                    passed_tests.add(full_path)
                elif status == "-":
                    skipped_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        # The three sets MUST be disjoint or TestResult raises. Failure wins.
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
