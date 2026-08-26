import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Identical in all three graded stages - only patch application differs between them.
#
# jest is invoked directly, NOT through `npm test`. package.json defines
# "test": "npm run lint && jest", so `npm test` runs eslint first and a lint failure
# would abort before a single test executed - which would leave the stage with no
# results and silently satisfy report.py's "fix something" check vacuously.
#
# ./node_modules/.bin/jest rather than `npx jest`: npx falls back to fetching from the
# registry when the local binary is missing, which would turn a broken install into a
# slow network op instead of an immediate, obvious failure.
TEST_CMD = "./node_modules/.bin/jest --ci --json --outputFile=/home/jest.json"


class ChangelogReaderActionImageBase(Image):
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
        # devDependencies pin jest ^25.1.0 (Feb 2020), which does not survive Node 18+.
        # package-lock.json is lockfileVersion 1, so this is npm 6/Node 14 era code; the
        # repo's own CI ran on whatever ubuntu-latest shipped in mid-2022, i.e. Node 16.
        # 16 is the highest version jest 25 is safe on, and -bookworm pins the distro so
        # the tag cannot drift underneath us. The full image (not -slim) already ships
        # git, so this config needs no apt-get at all.
        return "node:16-bookworm"

    def image_tag(self) -> str:
        # Per-PR base image. A shared "base" tag would be pinned to whichever
        # BASE_COMMIT built it first, and the history scrub then deletes every
        # other commit - so a second PR could not check its own base out.
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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # DEBIAN_FRONTEND and LANG are deliberately NOT set here: DockerfileEnhancer
        # already injects both (with TZ and the proxy/CA wiring) in the block it places
        # right after FROM. Re-declaring them only produces duplicate ENV lines.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV npm_config_fund=false
ENV npm_config_audit=false

WORKDIR /home/

{code}

{copy_commands}

{self.clear_env}

"""


class ChangelogReaderActionImageDefault(Image):
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
        return ChangelogReaderActionImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "reset_jest.sh",
                # /home/jest.json is the single source of truth for results, so it must
                # not survive from one stage into the next. node_modules is deliberately
                # left alone - it is baked into the image by prepare.sh and reinstalling
                # it per stage would put an npm registry round-trip in the graded path.
                """#!/bin/bash
rm -f /home/jest.json
rm -rf coverage .jest-cache 2>/dev/null || true
""",
            ),
            File(
                ".",
                "print_test_detail.sh",
                # jest's console output is not safe to parse: the per-test lines are bare
                # "  ✓ <name>" with no file path, so two tests that share a name in
                # different files collapse into one result. This repo has exactly that -
                # "should not throw error" appears in three of the four new rule suites.
                # jest --json carries testFilePath, ancestorTitles and title per
                # assertion, which is enough to build a collision-free id.
                """#!/bin/bash
echo "===== BEGIN JEST DETAIL ====="
node -e '
const fs = require("fs");
const path = "/home/jest.json";
if (!fs.existsSync(path)) { console.log("NO_JEST_JSON"); process.exit(0); }
let data;
try { data = JSON.parse(fs.readFileSync(path, "utf8")); }
catch (e) { console.log("JEST_JSON_UNPARSEABLE"); process.exit(0); }
const root = process.cwd() + "/";
for (const suite of data.testResults || []) {
  const name = suite.name || "";
  const file = name.startsWith(root) ? name.slice(root.length) : name;
  const results = suite.assertionResults || [];
  if (results.length === 0) {
    // A suite that could not even load - e.g. src/rules/*.test.js requiring a module
    // that only the fix patch creates - reports zero assertions. Emit the suite itself
    // so the failure is visible in the log; it carries no per-test id, so those tests
    // correctly land as NONE for this stage.
    console.log("TESTCASE " + file + " FAILED");
    continue;
  }
  for (const a of results) {
    const id = [file].concat(a.ancestorTitles || []).concat([a.title]).join("::");
    const st = a.status === "passed" ? "PASSED"
      : (a.status === "pending" || a.status === "skipped" || a.status === "todo") ? "SKIPPED"
      : "FAILED";
    console.log("TESTCASE " + id + " " + st);
  }
}
'
echo "===== END JEST DETAIL ====="
""",
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

# npm ci, not npm install: it installs exactly the lockfile and never rewrites it, so
# the working tree stays clean for check_git_changes.sh. node_modules/ is gitignored.
npm ci

# Hard gates, deliberately NOT tolerant. If jest cannot boot on this Node version, or
# the base tree cannot be required, then no stage can produce results - and that must
# fail here rather than three stages later behind a silently empty report.
./node_modules/.bin/jest --version
./node_modules/.bin/jest --listTests > /dev/null
node -e "require('./src/validate-entry'); console.log('DEPS_OK')"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/reset_jest.sh
set +e
{test_cmd}
JEST_RC=$?
set -e
echo "JEST_EXIT_CODE=$JEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/reset_jest.sh
set +e
{test_cmd}
JEST_RC=$?
set -e
echo "JEST_EXIT_CODE=$JEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/reset_jest.sh
set +e
{test_cmd}
JEST_RC=$?
set -e
echo "JEST_EXIT_CODE=$JEST_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
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


@Instance.register("mindsers", "changelog-reader-action")
class ChangelogReaderAction(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ChangelogReaderActionImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # Only the lines emitted by print_test_detail.sh are authoritative. jest's own
        # "PASS <file>" and "  ✓ <name>" lines are ignored on purpose: the first is
        # suite-level and the second carries no file, so together they mix two id
        # namespaces and let same-named tests in different files overwrite each other.
        #
        # The id group is greedy rather than \\S+ because jest test names contain spaces;
        # the status is always the final whitespace-delimited token on the line.
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            if line.startswith("===== BEGIN JEST DETAIL ====="):
                in_detail = True
                continue
            if line.startswith("===== END JEST DETAIL ====="):
                in_detail = False
                continue
            if not in_detail:
                continue

            m = case_re.match(line)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
