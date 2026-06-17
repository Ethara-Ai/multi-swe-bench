"""mswjs/msw registry.

Conformant with image.py's hardening model: every PR image's dependency()
returns a base-image *string* (node:18 / node:20), so the shared
Image.dockerfile() owns the build:

    git clone "${REPO_URL}"  ->  git checkout "${BASE_COMMIT}"
        ->  extra_setup()  ->  _HARDENING_BLOCK

and DockerfileEnhancer injects the proxy/cert infra ARGs and the final
sanitize pass. The _HARDENING_BLOCK detaches HEAD at ${BASE_COMMIT}, removes
the origin remote, deletes every other ref (heads/remotes/tags/replace),
expires the reflog and gc-prunes, then asserts the repo contains *only* the
base commit's history -- so the PR's fix can no longer be read out of git
history / fetched from origin (reward-hacking prevention).

The previous version chained ImageBase18/ImageBase20 Image objects and
overrode dockerfile() on every PR image. Because dependency() returned an
Image (not a str), DockerfileEnhancer.enhance() bailed out early: no
hardening, no infra, and BASE_COMMIT/REPO_URL build-args were never passed
(build_dataset.py only sets them when dependency() is a str). The clone kept
its origin remote and full history, leaving the fix reachable.

# ---------------------------------------------------------------------------
# Era boundaries (by PR number)
#
# Group 1: PRs  ..#607    -- yarn + jest --runInBand      (node:18)
# Group 2: PRs #608-#1375 -- yarn + jest --maxWorkers=3   (node:18)
# Group 3: PRs #1376-#1436 -- pnpm + jest --maxWorkers=3  (node:18)
# Group 4: PRs #1437-#2578 -- pnpm + vitest run           (node:18)
# Group 5: PRs #2579+      -- pnpm + vitest run           (node:20)
# ---------------------------------------------------------------------------
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- shared script fragments ----------------------------------------------

# Type-declaration (*.test-d.ts) check for the jest --runInBand era (group 1),
# emitted after the test run with `set +e` so a tsc failure is reported as a
# per-file PASS/FAIL line rather than aborting the script.
_TESTD_TSC_BLOCK = """
set +e

TEST_D_FILES=$(find src -name '*.test-d.ts' 2>/dev/null || true)
if [ -n "$TEST_D_FILES" ]; then
  TSC_OUT=$(npx tsc --noEmit --strict 2>&1 || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "^$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi
"""

# Vitest-era (groups 4/5) extra passes: an optional node-environment vitest
# config and the package.json `test:ts` type-declaration check.
_VITEST_EXTRA_BLOCK = """
set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi
"""

_LOCKFILE_EXCLUDE = "--exclude yarn.lock --exclude pnpm-lock.yaml"


def _group_for(number: int) -> int:
    """Route a PR number to its toolchain era."""
    if number <= 607:
        return 1  # yarn + jest --runInBand     (node:18)
    elif number <= 1375:
        return 2  # yarn + jest --maxWorkers=3   (node:18)
    elif number <= 1436:
        return 3  # pnpm + jest --maxWorkers=3   (node:18)
    elif number <= 2578:
        return 4  # pnpm + vitest run            (node:18)
    else:
        return 5  # pnpm + vitest run            (node:20)


class ImageDefault(Image):
    """Single PR-image type, parametrized by era (PR number).

    dependency() returns a base-image *string*, so the shared, hardened
    Image.dockerfile() builds the image and DockerfileEnhancer injects infra
    + the final git-history sanitize. dockerfile() is intentionally NOT
    overridden here.
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

    # --- era helpers -------------------------------------------------------

    def _group(self) -> int:
        return _group_for(self.pr.number)

    def dependency(self) -> Union[str, "Image"]:
        # Era 3 needs node:22 (node:sqlite built-in used in tests).
        # Era 5 needs node:20+. Everything else on node:18.
        if self._group() == 3:
            return "node:22"
        return "node:20" if self._group() == 5 else "node:18"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _pm_setup(self) -> str:
        """corepack / package-manager activation, run inside prepare.sh."""
        if self._group() <= 2:
            # yarn classic eras
            return "corepack enable\ncorepack prepare yarn@1.22.22 --activate"
        if self._group() == 3:
            # pnpm 11 (ships with node:22 corepack) blocks postinstall scripts;
            # pin to pnpm 9.x which has no approve-builds restriction.
            return "corepack enable\ncorepack prepare pnpm@9.15.4 --activate"
        return "corepack enable"

    def _install_cmd(self) -> str:
        if self._group() <= 2:
            return "yarn install --frozen-lockfile || true"
        return "pnpm install --no-frozen-lockfile || true"

    def _reinstall_cmd(self) -> str:
        # After applying a patch the lockfile may need to give; never frozen.
        if self._group() <= 2:
            return "yarn install || true"
        return "pnpm install --no-frozen-lockfile || true"

    def _build_cmd(self) -> str:
        # vitest eras build before running tests.
        return "pnpm build || true" if self._group() >= 4 else ""

    def _runner_block(self) -> str:
        g = self._group()
        if g == 1:
            return "yarn jest --runInBand\n" + _TESTD_TSC_BLOCK
        elif g == 2:
            return "yarn jest --maxWorkers=3\n"
        elif g == 3:
            return "pnpm jest --maxWorkers=3\n"
        else:
            return "pnpm vitest run\n" + _VITEST_EXTRA_BLOCK

    # --- dockerfile glue ---------------------------------------------------

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}` and before _HARDENING_BLOCK.
        # Stage patches + eval scripts into /home/ and run prepare.sh, which
        # installs deps (+ builds) so the eval scripts run offline. Installed
        # node_modules is untracked, so the hardening pass (which only rewrites
        # git history) leaves it intact.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        pm_setup = self._pm_setup()
        install_cmd = self._install_cmd()
        reinstall_cmd = self._reinstall_cmd()
        build_cmd = self._build_cmd()
        runner_block = self._runner_block()

        # prepare.sh: repo is ALREADY cloned + checked out at ${BASE_COMMIT}
        # and (about to be) hardened by Image.dockerfile(); so no git
        # fetch/checkout here -- just activate the package manager, install
        # dependencies, and build.
        build_line = f"{build_cmd}\n" if build_cmd else ""
        prepare = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            "git reset --hard || true\n"
            f"{pm_setup}\n"
            f"{install_cmd}\n"
            f"{build_line}"
        )

        run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"{runner_block}"
        )

        reinstall_line = f"{reinstall_cmd}\n"
        build_after_patch = f"{build_cmd}\n" if build_cmd else ""

        test_run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/test.patch || true\n"
            f"{reinstall_line}"
            f"{build_after_patch}"
            f"{runner_block}"
        )

        fix_run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/test.patch || true\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/fix.patch || true\n"
            f"{reinstall_line}"
            f"{build_after_patch}"
            f"{runner_block}"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("mswjs", "msw")
class Msw(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _group(self) -> int:
        return _group_for(self.pr.number)

    def dependency(self) -> Optional[Image]:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        re_tsc_error = re.compile(r"(\S+\.test-d\.(?:tsx|ts))\(\d+,\d+\):\s*error\s+TS\d+")

        group = self._group()

        if group <= 3:
            re_pass = re.compile(r"PASS\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"FAIL\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        else:
            re_pass = re.compile(r"(?:PASS|[✓✔])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"(?:FAIL|[❯×✗])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_skip = re.compile(r"[↓⊘]\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                skip_match = re_skip.search(clean)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
