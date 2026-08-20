"""sindresorhus/p-retry harness config — AVA, npm, ESM (no build step).

p-retry is a small ESM JavaScript library (``index.js`` uses ``export default``;
``index.d.ts`` is types only, so there is nothing to compile). Tests are AVA in
``test.js`` at the repo root.

Node version
------------
node:18. PR #59 ("Support AbortController") adds tests guarded by
``if (globalThis.AbortController !== undefined)`` and the feature is documented
as *"Requires Node.js 16 or later."*. AbortController only exists on Node >= 15,
so an older base image would SKIP those tests in every stage and the instance
would produce no f2p. node:18 is chosen so the AbortController tests actually
run (FAIL before the fix, PASS after).

Test invocation
---------------
AVA is invoked directly as ``npx --no-install ava --tap`` rather than through
``npm test`` (which would also run xo/tsd and could abort the stage before any
test ran). ``--tap`` gives stable, machine-parseable output across AVA versions.

Image chain:  node:18 -> Base (clone repo)
                       -> Per-PR (git checkout + npm install + patches + run)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class PRetryImageBase(Image):
    """Base image — node:18, clone repo."""

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

# git and curl are already provided by the node:18 base image (buildpack-deps
# derived), so no package installation step is needed here.
{code}

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class PRetryImageDefault(Image):
    """Per-PR instance image. Checks out the base commit, installs deps,
    and injects patches + the three stage scripts."""

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
        return PRetryImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _make_run_script(self, patches: str) -> str:
        """Generate a stage script. ``patches`` applies the relevant diffs
        (empty for the baseline run). AVA is invoked directly with --tap; the
        three stages MUST invoke it identically so their results are comparable."""
        return """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{patches}
# The patches can touch dependencies; reinstall so the run sees them.
npm install || true

export CI=true
npx --no-install ava --tap 2>&1 || true
""".format(
            repo=self.pr.repo,
            patches=patches,
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

npm install || true

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            # run.sh — baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh — test.patch only
            File(
                ".",
                "test-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn /home/test.patch || "
                    "git apply --whitespace=nowarn --3way /home/test.patch || true"
                ),
            ),
            # fix-run.sh — test.patch + fix.patch
            File(
                ".",
                "fix-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || "
                    "git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true"
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


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("sindresorhus", "p-retry")
class PRetry(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PRetryImageDefault(self.pr, self._config)

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
        """Parse AVA TAP output.

        TAP 13 (``ava --tap``):
            ok 1 - test name
            not ok 2 - test name
            ok 3 - test name # SKIP
        The test name is everything after the number and optional dash.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        re_ok = re.compile(r"^ok\s+\d+\s*-?\s*(.*)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s*-?\s*(.*)$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        def qualify(title: str) -> str:
            """Normalize an AVA test point to a pytest-like ``<file>::<title>`` id.

            AVA's TAP uses ``<file> › <title>`` only when a run spans multiple
            test files; with p-retry's single ``test.js`` it emits the bare
            title, so we prefix the known file. The id stays identical across
            the run/test/fix stages, so f2p/n2p detection is unaffected."""
            if " › " in title:
                file, _, rest = title.partition(" › ")
                return f"{file.strip()}::{rest.strip()}"
            return f"test.js::{title}"

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # `not ok` must be tested before `ok`.
            m = re_not_ok.match(stripped)
            if m:
                name = m.group(1)
                directive = re_directive.search(name)
                name = re_directive.sub("", name).strip()
                if not name:
                    continue
                name = qualify(name)
                # `not ok ... # TODO` is an expected failure, not a real one.
                if directive and directive.group(1).upper() == "TODO":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            m = re_ok.match(stripped)
            if m:
                name = m.group(1)
                directive = re_directive.search(name)
                name = re_directive.sub("", name).strip()
                if not name:
                    continue
                name = qualify(name)
                if directive and directive.group(1).upper() == "SKIP":
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        # Keep the three sets disjoint (TestResult rejects overlap).
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
