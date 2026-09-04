import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# webdriverio -- pnpm/vitest era (main branch, PR >= 12432)
# =============================================================================
#
# At PR 12432 the main branch migrated the monorepo from npm + lerna to a pnpm
# workspace. package.json gains a `packageManager` field (pnpm@8.15.3, later
# pnpm@9.0.4, then pnpm@10.12.4) and a pnpm-lock.yaml + pnpm-workspace.yaml. The
# npm + lerna-bootstrap install used by the older era configs does not work on
# these commits, so this file provides the pnpm toolchain.
#
# The .nvmrc across this era climbs from v20.11.1 to v24. node:24 was verified
# (interactively, in Docker) to build and run the vitest suite at BOTH ends of
# the range, so a single base image spans the whole era:
#
#   PR 12432 (pnpm@8.15.3, .nvmrc v20.11.1): 2961 passed | 11 skipped
#   PR 15013 (pnpm@10.12.4, .nvmrc v24)    : 3854 passed |  7 skipped
#
# corepack (shipped with node:24) reads the pinned `packageManager` version from
# each commit's package.json, so the correct pnpm is used per-PR automatically.
#
# Unit tests run via the repo's own script contract, `test:unit:run`, which is
# `vitest --config vitest.config.ts --run` throughout this era.
# =============================================================================


class WebDriverIOVitestPnpmImageBase(Image):
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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base-vitest-pnpm"

    def workdir(self) -> str:
        return "base-vitest-pnpm"

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
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0

RUN corepack enable

{code}

{self.clear_env}

"""


class WebDriverIOVitestPnpmImageDefault(Image):
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
        return WebDriverIOVitestPnpmImageBase(self.pr, self.config)

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
git remote add origin https://github.com/{pr.org}/{pr.repo}.git 2>/dev/null || true
git fetch --depth=1 origin {pr.base.sha} 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

corepack enable
# `|| true`: native optional deps can fail to compile on arm64 without being
# fatal to the unit suite.
pnpm install || true

# pnpm install can rewrite pnpm-lock.yaml / other tracked files. node_modules is
# gitignored and survives, so restoring tracked files returns the worktree to
# exactly BASE_COMMIT -- the state every `git apply` in the run scripts expects.
git checkout -- .
# pnpm install + husky postinstall can leave benign untracked artifacts that
# `git checkout -- .` does not remove; they do not affect `git apply` in the run
# scripts, so this final clean-tree assert is advisory only (never fatal).
bash /home/check_git_changes.sh || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Memory guard: `pnpm run setup` builds the whole monorepo (compile:all runs
# `pnpm -r` across packages in parallel). Under several parallel instances this
# OOM-kills the compiler/vitest in a small Docker VM. Build packages one at a
# time and bound the node heap so a single instance stays within budget.
export NPM_CONFIG_WORKSPACE_CONCURRENCY=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
pnpm run setup || true

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
# Scope vitest to ONLY the test files touched by test.patch (deterministic;
# avoids whole-suite flaky/env failures). Only run files that exist (new test
# files added by the patch won't exist in the baseline run.sh stage).
TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[cm]?[jt]sx?$' | sort -u)
RUN_FILES=""
for f in $TARGET; do [ -f "$f" ] && RUN_FILES="$RUN_FILES $f"; done
if [ -n "$RUN_FILES" ]; then
    npx vitest --config "$CFG" --run --reporter=verbose --coverage.enabled=false $RUN_FILES
else
    echo "no target test files present (nothing to run)"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Memory guard: `pnpm run setup` builds the whole monorepo (compile:all runs
# `pnpm -r` across packages in parallel). Under several parallel instances this
# OOM-kills the compiler/vitest in a small Docker VM. Build packages one at a
# time and bound the node heap so a single instance stays within budget.
export NPM_CONFIG_WORKSPACE_CONCURRENCY=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude=pnpm-lock.yaml /home/test.patch
pnpm run setup || true

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
# Scope vitest to ONLY the test files touched by test.patch (deterministic;
# avoids whole-suite flaky/env failures). Only run files that exist (new test
# files added by the patch won't exist in the baseline run.sh stage).
TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[cm]?[jt]sx?$' | sort -u)
RUN_FILES=""
for f in $TARGET; do [ -f "$f" ] && RUN_FILES="$RUN_FILES $f"; done
if [ -n "$RUN_FILES" ]; then
    npx vitest --config "$CFG" --run --reporter=verbose --coverage.enabled=false $RUN_FILES
else
    echo "no target test files present (nothing to run)"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Memory guard: `pnpm run setup` builds the whole monorepo (compile:all runs
# `pnpm -r` across packages in parallel). Under several parallel instances this
# OOM-kills the compiler/vitest in a small Docker VM. Build packages one at a
# time and bound the node heap so a single instance stays within budget.
export NPM_CONFIG_WORKSPACE_CONCURRENCY=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude=pnpm-lock.yaml /home/test.patch /home/fix.patch
pnpm run setup || true

CFG=vitest.config.ts
[ -f vitest.config.mts ] && CFG=vitest.config.mts
# Scope vitest to ONLY the test files touched by test.patch (deterministic;
# avoids whole-suite flaky/env failures). Only run files that exist (new test
# files added by the patch won't exist in the baseline run.sh stage).
TARGET=$(grep -E '^\\+\\+\\+ b/' /home/test.patch | awk '{{print $2}}' | sed 's#^b/##' | grep -E '\\.test\\.[cm]?[jt]sx?$' | sort -u)
RUN_FILES=""
for f in $TARGET; do [ -f "$f" ] && RUN_FILES="$RUN_FILES $f"; done
if [ -n "$RUN_FILES" ]; then
    npx vitest --config "$CFG" --run --reporter=verbose --coverage.enabled=false $RUN_FILES
else
    echo "no target test files present (nothing to run)"
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


@Instance.register("webdriverio", "webdriverio_15013_to_12432")
class WebDriverIOVitestPnpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebDriverIOVitestPnpmImageDefault(self.pr, self._config)

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

        Verified against real captured output (node:24, PR 12432 and 15013).
        The verbose reporter emits one line per test:

            ✓ packages/foo/tests/bar.test.ts > suite > name
            × packages/foo/tests/bar.test.ts > suite > name
             FAIL  packages/foo/tests/bar.test.ts > suite > name

        The leading "packages/.../file.test.ts > ..." path makes each id unique
        across this monorepo (Check 4A -- low collision risk).

        Check 4B: vitest appends a duration to slow tests -- "... name 304ms",
        "... name 1010ms", even "... name 0ms". If left in the id the SAME test
        gets a DIFFERENT name whenever its timing drifts between the run/test/fix
        stages, silently falling out of the cross-stage comparison. Timing is
        stripped from every captured name.
        """
        # Strip ALL CSI escapes, not just colour (SGR) ones.
        cleaned_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Trailing vitest duration -- "304ms", "1.5s", "0ms". Must not reach the id.
        re_timing = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s$")
        # A real test line carries the "file > ..." path; the bare summary glyphs
        # (e.g. "✓ 12 passed") do not, so require the ".test." path to avoid them.
        re_pass = re.compile(r"^[✓✔]\s+(\S+\.test\.\w+.*)$")
        re_fail = re.compile(r"^[×✕✗✖]\s+(\S+\.test\.\w+.*)$")
        # File-level failure header: capture ONLY the file path token so a
        # load-failure id is clean and identical across stages (vitest appends a
        # " [ ... ]" annotation that must not reach the id).
        re_fail_hdr = re.compile(r"^FAIL\s+(\S+\.test\.\w+)")
        re_skip = re.compile(r"^[↓○]\s+(\S+\.test\.\w+.*?)(?:\s*\[skipped\])?$")

        def clean(name: str) -> str:
            return re_timing.sub("", name).strip()

        for raw_line in cleaned_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                name = clean(m.group(1))
                if name and name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = re_fail.match(line) or re_fail_hdr.match(line)
            if m:
                name = clean(m.group(1))
                if name:
                    passed_tests.discard(name)
                    skipped_tests.discard(name)
                    failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = clean(m.group(1))
                if name and name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        # Invariant: no id in both passed and failed (test runner retries, etc.).
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
