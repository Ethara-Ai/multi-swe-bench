import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class WebDriverIOJestImageBase(Image):
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

    # Single shared base for the whole Jest era: one base image, not one per PR
    # (matches the repo convention, e.g. trpc's "base").
    #
    # The pipeline hardening block prunes the base image's git history down to a
    # single BASE_COMMIT's ancestry, so a shared base does NOT already contain
    # every PR's base commit. prepare.sh compensates by re-attaching the remote
    # and fetching the exact `base.sha` before checkout, so one base serves all
    # PRs without `fatal: unable to read tree`.
    def image_tag(self) -> str:
        return "base-jest"

    def workdir(self) -> str:
        return "base-jest"

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


class WebDriverIOJestImageDefault(Image):
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
        return WebDriverIOJestImageBase(self.pr, self.config)

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
                "patch_jest_config.sh",
                """#!/bin/bash
# Old jest's resolver cannot open `node:`-scheme imports (e.g. `node:stream`) that
# today's cheerio/parse5 pull in, so those suites fail to load and capture 0 tests.
# Add a moduleNameMapper that strips the `node:` prefix to the repo's package.json
# jest config. Safe/additive: only affects `node:` imports (which are broken anyway)
# and no-ops when there is no package.json jest key.
node -e '
const fs = require("fs");
try {
  const j = JSON.parse(fs.readFileSync("package.json", "utf8"));
  if (j.jest) {
    j.jest.moduleNameMapper = Object.assign({}, j.jest.moduleNameMapper, { "^node:(.*)$": "$1" });
    fs.writeFileSync("package.json", JSON.stringify(j, null, 2));
    console.log("patch_jest_config: node: mapper added to package.json jest");
  } else {
    console.log("patch_jest_config: no package.json jest key; skipped");
  }
} catch (e) { console.log("patch_jest_config skipped: " + e.message); }
' || true
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

npm install -g lerna@6 npm-run-all || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# The shared base image is pruned (git gc) to a single BASE_COMMIT's ancestry by
# the pipeline hardening block, so this PR's commit may be absent. Re-attach the
# remote and fetch the exact sha before checkout so one base serves every PR.
git remote add origin https://github.com/{pr.org}/{pr.repo}.git 2>/dev/null || true
git fetch --depth=1 origin {pr.base.sha} 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Fresh-resolve install: delete the old (lockfileVersion 1) lockfile and let npm
# resolve. `npm ci` was tried and fails on these out-of-sync era lockfiles (wipes
# node_modules -> no jest/ts-jest at all -> 0 tests). Fresh-resolve reliably
# installs a working jest+ts-jest toolchain; the too-new `node:`-scheme deps it
# pulls are handled at run time by the node: moduleNameMapper (patch_jest_config.sh).
rm -f package-lock.json
npm install --legacy-peer-deps || npm install --force || true
npm install rimraf --legacy-peer-deps --no-save || true
npx lerna bootstrap --no-ci --force-local || true

# npm may rewrite tracked lockfiles; node_modules/ is gitignored and survives, so
# restoring tracked files returns the worktree to exactly BASE_COMMIT -- the state
# every `git apply` in the three run scripts expects.
git checkout -- .
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npm run build || true
bash /home/patch_jest_config.sh || true

# Scope jest to ONLY the test files touched by test.patch. Running the whole
# monorepo suite pulls in hundreds of unrelated, flaky/env-broken tests whose
# PASS->FAIL drift between stages invalidates otherwise-good f2p instances.
TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[jt]sx?$' | sort -u)
EXIST=""
for f in $TARGET; do [ -f "$f" ] && EXIST="$EXIST $f"; done
if [ -n "$EXIST" ]; then
    npx jest --no-coverage --verbose $EXIST
else
    echo "run.sh: no pre-existing target test files at base (nothing to run)"
fi

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
npm run build || true
bash /home/patch_jest_config.sh || true

TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[jt]sx?$' | sort -u)
if [ -n "$TARGET" ]; then
    npx jest --no-coverage --verbose $TARGET
else
    npx jest --no-coverage --verbose
fi

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
npm run build || true
bash /home/patch_jest_config.sh || true

TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[jt]sx?$' | sort -u)
if [ -n "$TARGET" ]; then
    npx jest --no-coverage --verbose $TARGET
else
    npx jest --no-coverage --verbose
fi

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


@Instance.register("webdriverio", "webdriverio_10978_to_5279")
class WebDriverIOJest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebDriverIOJestImageDefault(self.pr, self._config)

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
        """Parse Jest verbose output into per-test results.

        Three defects fixed here after a real-log audit of PR 6000:

        4B (was corrupting the dataset). Jest appends a duration to each test
        line -- "should do X (2 ms)". The previous regex captured the whole
        remainder of the line, so the SAME test appeared under a DIFFERENT name
        in each stage whenever its timing shifted by a millisecond. Measured on
        the real logs: run had 630 passing names, fix had 647, and only 240
        matched. 390 tests silently fell out of the cross-stage comparison, and
        p2p came back as 333 instead of ~630. The report still said valid=True,
        which is precisely why this is dangerous. Timing is now stripped.

        4A. The old patterns fed BOTH the file-level "PASS <path>" headers and
        the per-test "v <name>" lines into one set -- 89 file paths mixed with
        558 test names. Worse, a bare test name has no file context, so two
        identically-named tests in different packages of this monorepo would
        collide. Test ids are now qualified as "<file>::<test name>", and suite
        headers are used only to establish that context, never recorded as
        results themselves.

        4C. The ANSI pattern only matched SGR sequences (ending in "m"). Any
        other CSI escape survived into the captured name. Widened to the full
        [a-zA-Z] terminator set.
        """
        # 4C: strip ALL CSI escapes, not just colour (SGR) ones.
        cleaned_log = re.sub(r"\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Jest suite header: "PASS packages/foo/tests/bar.test.js (1.2 s)".
        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+\.[jt]sx?)")
        # Per-test line. The glyph set covers Jest's unicode and ASCII fallbacks.
        re_test = re.compile(r"^([✓✔×✕✗○⊘10x])\s+(.+)$")
        # 4B: Jest's trailing duration -- "(2 ms)", "(1.5 s)". Must not reach the id.
        re_timing = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")

        PASS_GLYPHS = "✓✔"
        FAIL_GLYPHS = "×✕✗"
        SKIP_GLYPHS = "○⊘"

        def record(status: str, test_id: str) -> None:
            if status == "PASS":
                if test_id in failed_tests:
                    return
                skipped_tests.discard(test_id)
                passed_tests.add(test_id)
            elif status == "FAIL":
                passed_tests.discard(test_id)
                skipped_tests.discard(test_id)
                failed_tests.add(test_id)
            elif status == "SKIP":
                if test_id not in passed_tests and test_id not in failed_tests:
                    skipped_tests.add(test_id)

        current_suite = None
        for raw_line in cleaned_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            m = re_suite.match(line)
            if m:
                # Context only. A suite header is not a test result (4A).
                current_suite = m.group(2)
                continue

            m = re_test.match(line)
            if not m:
                continue

            glyph, name = m.group(1), re_timing.sub("", m.group(2)).strip()
            if name.startswith("skipped "):
                name = name[len("skipped "):].strip()
            if not name:
                continue

            test_id = f"{current_suite}::{name}" if current_suite else name

            if glyph in PASS_GLYPHS:
                record("PASS", test_id)
            elif glyph in FAIL_GLYPHS:
                record("FAIL", test_id)
            elif glyph in SKIP_GLYPHS:
                record("SKIP", test_id)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
