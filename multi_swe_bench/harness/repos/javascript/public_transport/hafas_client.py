from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Era boundary: PR 304 switches from CJS (node>=10, tape/tap@15) to ESM (node>=16, tap@18+).
# PRs <= 264: CJS era - node:14 base
# PRs >= 304: ESM era - node:22 base
_ERA_BOUNDARY = 304


class ImageBase(Image):
    """Base image for the CJS era (PRs <= 264). Uses node:14."""

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
        return "node:14"

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

{code}

{self.clear_env}

"""


class ImageBaseESM(Image):
    """Base image for the ESM era (PRs >= 304). Uses node:22."""

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
        return "node:22"

    def image_tag(self) -> str:
        return "base-esm"

    def workdir(self) -> str:
        return "base-esm"

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


# PRs whose test patches only add e2e tests for a new transit profile.
# We inject a synthetic unit test that require()s the profile module.
# Without fix.patch the profile dir doesn't exist → MODULE_NOT_FOUND → FAIL.
# With fix.patch the profile dir exists → require succeeds → PASS.
_PROFILE_TEST_PRS: dict[int, str] = {
    94: "invg",
    171: "svv",
}

# PR-177 introduces VCR (replayer) infrastructure.  The test.patch adds e2e
# fixtures and the fix.patch carries the actual bug-fix, but package.json
# changes are excluded by git-apply so replayer is never installed.  We install
# it in prepare.sh and run e2e tests with VCR_MODE=playback.
_VCR_REPLAYER_PRS: set[int] = {177}

# Tap-era PRs that already have replayer + e2e fixtures in the base image.
# Just need VCR_MODE=playback added to the test command.
_VCR_TAP15_PRS: set[int] = {251, 264}

# ESM-era PRs that already have @pollyjs + e2e fixtures in the base image.
_VCR_ESM_PRS: set[int] = {344}


def _test_cmd(pr_number: int, include_e2e: bool = True) -> str:
    """Return the correct test command based on era.

    PRs 94-177 (tape era): node test/index.js
    PRs 244-264 (tap@15 era): npx tap test/*.js test/format/*.js test/parse/*.js
    PRs >= 304 (tap@18+ ESM era): npx tap test/lib/*.js test/*.js test/format/*.js test/parse/*.js

    When include_e2e is True, VCR e2e execution is appended for qualifying PRs.
    """
    if pr_number <= 177:
        base = "node test/index.js"
    elif pr_number < _ERA_BOUNDARY:
        base = "npx tap test/*.js test/format/*.js test/parse/*.js"
    else:
        base = "npx tap test/lib/*.js test/*.js test/format/*.js test/parse/*.js"

    if include_e2e:
        if pr_number in _VCR_REPLAYER_PRS:
            base += "\nVCR_MODE=playback node test/e2e/index.js"
        elif pr_number in _VCR_TAP15_PRS:
            base += "\nVCR_MODE=playback npx tap test/e2e/*.js"
        elif pr_number in _VCR_ESM_PRS:
            base += "\nVCR_MODE=playback npx tap test/e2e/*.js"

    return base


class ImageDefault(Image):
    """PR-specific image for ESM era (PRs >= 304)."""

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
        return ImageBaseESM(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        base_test_cmd = _test_cmd(self.pr.number, include_e2e=False)
        full_test_cmd = _test_cmd(self.pr.number, include_e2e=True)
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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{base_test_cmd}
""".format(pr=self.pr, base_test_cmd=base_test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/test.patch
{full_test_cmd}

""".format(pr=self.pr, full_test_cmd=full_test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/fix.patch
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/test.patch
{full_test_cmd}

""".format(pr=self.pr, full_test_cmd=full_test_cmd),
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


class ImageDefaultEarly(Image):
    """PR-specific image for CJS era (PRs <= 264)."""

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

    def _profile_test_content(self) -> str | None:
        profile = _PROFILE_TEST_PRS.get(self.pr.number)
        if not profile:
            return None
        return (
            "'use strict'\n"
            "const test = require('tape')\n"
            f"test('{profile} profile loads correctly', function (t) {{\n"
            "  try {\n"
            f"    const profile = require('../p/{profile}')\n"
            "    t.ok(profile, 'profile module is truthy')\n"
            "    t.ok(profile.products, 'profile exposes products')\n"
            "  } catch (err) {\n"
            "    t.fail('profile module could not be loaded: ' + err.message)\n"
            "  }\n"
            "  t.end()\n"
            "})\n"
        )

    def _extra_prepare(self) -> str:
        if self.pr.number in _VCR_REPLAYER_PRS:
            return "npm install replayer || true\n"
        return ""

    def _inject_profile_test(self) -> str:
        if self.pr.number not in _PROFILE_TEST_PRS:
            return ""
        return (
            "cp /home/profile-check.js /home/{repo}/test/profile-check.js\n"
            "echo \"require('./profile-check')\" >> /home/{repo}/test/index.js\n"
        ).format(repo=self.pr.repo)

    def files(self) -> list[File]:
        base_test_cmd = _test_cmd(self.pr.number, include_e2e=False)
        full_test_cmd = _test_cmd(self.pr.number, include_e2e=True)
        extra_prepare = self._extra_prepare()
        inject_profile = self._inject_profile_test()

        result = [
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
{extra_prepare}
""".format(pr=self.pr, extra_prepare=extra_prepare),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{inject_profile}{base_test_cmd}
""".format(pr=self.pr, base_test_cmd=base_test_cmd, inject_profile=inject_profile),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/test.patch
{inject_profile}{full_test_cmd}

""".format(pr=self.pr, full_test_cmd=full_test_cmd, inject_profile=inject_profile),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/fix.patch
git apply --exclude package.json --exclude package-lock.json --whitespace=nowarn /home/test.patch
{inject_profile}{full_test_cmd}

""".format(pr=self.pr, full_test_cmd=full_test_cmd, inject_profile=inject_profile),
            ),
        ]

        profile_content = self._profile_test_content()
        if profile_content:
            result.append(File(".", "profile-check.js", profile_content))

        return result

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


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _parse_tap(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_tap_test = re.compile(r"^\s*(ok|not ok)\s+(\d+)\s+(?:-\s+)?(.+?)(?:\s+#.*)?$")
    re_tap_subtest = re.compile(r"^\s*# Subtest:\s*(.+)")
    re_tap_skip = re.compile(r"#\s*SKIP\b", re.IGNORECASE)
    re_tap_todo = re.compile(r"#\s*TODO\b", re.IGNORECASE)
    re_tape_group = re.compile(r"^# (.+)$")

    re_tapspec_pass = re.compile(r"^\s*[✔✓]\s+(.+?)(?:\s+\(\d+.*\))?\s*$")
    re_tapspec_fail = re.compile(r"^\s*[✖✗×]\s+(.+?)(?:\s+\(\d+.*\))?\s*$")

    lines = log.splitlines()
    subtest_stack: list[str] = []
    tape_group: str = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        subtest_match = re_tap_subtest.match(line)
        if subtest_match:
            indent = len(line) - len(line.lstrip())
            level = indent // 4
            subtest_stack = subtest_stack[:level]
            subtest_stack.append(subtest_match.group(1).strip())
            continue

        tap_match = re_tap_test.match(stripped)
        if tap_match:
            status = tap_match.group(1)
            test_num = tap_match.group(2)
            test_name = tap_match.group(3).strip()

            if subtest_stack:
                full_name = ":".join(subtest_stack + [test_name])
            elif tape_group:
                full_name = f"{tape_group}#{test_num}"
            else:
                full_name = f"#{test_num} {test_name}"

            full_name = re.sub(r"\s*#\s*time=\S+", "", full_name)

            if re_tap_skip.search(line):
                skipped_tests.add(full_name)
            elif re_tap_todo.search(line):
                skipped_tests.add(full_name)
            elif status == "ok":
                passed_tests.add(full_name)
            elif status == "not ok":
                failed_tests.add(full_name)
            continue

        if not subtest_stack:
            group_match = re_tape_group.match(stripped)
            if group_match:
                candidate = group_match.group(1).strip()
                if not candidate.startswith("tests ") and not candidate.startswith("pass") and candidate != "ok" and not candidate.startswith("fail"):
                    tape_group = candidate
                continue

        tapspec_pass = re_tapspec_pass.match(stripped)
        if tapspec_pass:
            test_name = tapspec_pass.group(1).strip()
            if test_name:
                passed_tests.add(test_name)
            continue

        tapspec_fail = re_tapspec_fail.match(stripped)
        if tapspec_fail:
            test_name = tapspec_fail.group(1).strip()
            if test_name:
                failed_tests.add(test_name)
            continue

    for test in failed_tests:
        passed_tests.discard(test)
        skipped_tests.discard(test)
    for test in skipped_tests:
        passed_tests.discard(test)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("public-transport", "hafas-client")
class HafasClient(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number < _ERA_BOUNDARY:
            return ImageDefaultEarly(self.pr, self._config)
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
        return _parse_tap(_strip_ansi(test_log))
