import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files


class IcestarkImageBase(Image):
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
        # icestark 2.x (2021) pins jest 24 / ts-jest 24 / typescript 4.3 and
        # ships a yarn.lock. node:14 is the runtime of that era and bundles
        # yarn 1.x, which is the package manager that lockfile belongs to.
        return "node:14"

    # Tagged per PR rather than with a shared "base" tag: the base image pins a
    # node version and a warm yarn cache chosen for THIS PR's era, so a second
    # icestark PR from a different era must not silently overwrite it.
    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The clone below is written in the exact form DockerfileEnhancer
        # standardizes to (`git clone "${REPO_URL}"`), so the enhancer leaves
        # this block alone and the extra `git fetch` survives.
        #
        # That fetch is REQUIRED for this repo. icestark PRs target `release/*`
        # branches that upstream deletes after the release ships, so their base
        # commits are no longer reachable from any branch or tag -- a plain
        # clone + `git checkout <base.sha>` fails with "reference is not a
        # tree". GitHub still serves those commits by SHA and via
        # refs/pull/*/head, so the base commit is fetched explicitly.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git fetch --no-tags origin "${{BASE_COMMIT}}" \\
    || git fetch --no-tags origin "+refs/pull/*/head:refs/remotes/origin/pr/*"

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


class IcestarkImageDefault(Image):
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
        return IcestarkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # icestark runs jest through the ts-jest preset, which type-checks every
    # spec before executing it. With type-checking on, a test patch that calls
    # the post-fix signature makes the WHOLE suite fail to compile at the
    # test.patch stage ("Test suite failed to run"), so jest reports zero test
    # cases -- every test lands on run=PASS / test=NONE / fix=PASS, which
    # report.py classifies as p2p and the instance ends up with no f2p target
    # at all. Turning ts-jest diagnostics off lets the suite compile so the
    # affected cases fail at RUNTIME and produce a real fail->pass signal. The
    # flag is identical in run.sh / test-run.sh / fix-run.sh so all three stages
    # execute the same tests.
    # Substituted through str.format() as a VALUE, so its braces are literal
    # and must not be doubled.
    TEST_CMD = (
        "npx jest --ci --verbose --colors=false --coverage=false"
        ' --globals=\'{"ts-jest":{"diagnostics":false}}\''
    )

    def _test_files(self) -> str:
        return " ".join(get_modified_files(self.pr.test_patch))

    def files(self) -> list[File]:
        test_files = self._test_files()
        test_cmd = self.TEST_CMD
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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
# The committed yarn.lock is out of date w.r.t. package.json at this commit, so
# --frozen-lockfile fails. --pure-lockfile makes the fallback resolve without
# rewriting yarn.lock; the checkout below is the belt-and-braces guarantee that
# the worktree still matches base.sha, so test.patch/fix.patch apply cleanly.
yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 \\
    || yarn install --pure-lockfile --ignore-scripts --network-timeout 600000 \\
    || true
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
export NODE_ENV=unittest

cd /home/{pr.repo}
{test_cmd} {test_files}

""".format(pr=self.pr, test_cmd=test_cmd, test_files=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=unittest

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd} {test_files}

""".format(pr=self.pr, test_cmd=test_cmd, test_files=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=unittest

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd} {test_files}

""".format(pr=self.pr, test_cmd=test_cmd, test_files=test_files),
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


@Instance.register("ice-lab", "icestark")
class Icestark(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IcestarkImageDefault(self.pr, self._config)

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

        # jest 24 `--verbose` prints one line per test case, e.g.
        #     ✓ appendAssets basic (12ms)
        #     ✕ appendLink -> success (3ms)
        #     ○ skipped some test
        # Only the test title is captured. The trailing timing varies between
        # the run/test/fix stages and must never land inside the test name, or
        # the same test appears as two separate entries in the cross-stage
        # union that Report.__post_init__ builds.
        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        # Timing suffix printed by jest: "(12ms)" (jest <= 26) or "(12 ms)".
        timing_re = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")
        # jest can render an aggregate instead of a title; not a real test.
        skip_summary_re = re.compile(r"^skipped\s+\d+\s+tests?$")

        # Only jest's own status glyphs. `●` is deliberately NOT here: jest uses
        # it for failure-detail headers ("● Test suite failed to run",
        # "● describe > test"), so treating it as a status marker would inject
        # phantom test names that exist in one stage only.
        case_re = re.compile(r"^\s*([✓√✕×○✎])\s+(.+)$")

        passed_markers = {"✓", "√"}
        failed_markers = {"✕", "×"}
        skipped_markers = {"○", "✎"}

        # tests/handleAssets.spec.tsx declares two DIFFERENT tests under the
        # identical title 'processHtml - baseElement - relative' (an upstream
        # copy-paste of the title). Collapsing them into one set entry would
        # silently drop a test case, so repeated titles get a stable
        # occurrence suffix. jest executes a file's tests in declaration order,
        # so the same test gets the same suffix in every stage.
        seen: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line).rstrip()
            match = case_re.match(line)
            if not match:
                continue

            marker = match.group(1)
            name = timing_re.sub("", match.group(2)).strip()
            if not name or skip_summary_re.match(name):
                continue

            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} #{seen[name]}"

            if marker in passed_markers:
                passed_tests.add(name)
            elif marker in failed_markers:
                failed_tests.add(name)
            elif marker in skipped_markers:
                skipped_tests.add(name)

        # TestResult.__post_init__ rejects a name that lands in more than one
        # bucket. A duplicated/retried title resolves to its worst outcome.
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
