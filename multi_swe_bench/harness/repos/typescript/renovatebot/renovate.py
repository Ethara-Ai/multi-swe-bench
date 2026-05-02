import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RenovateImageBase(Image):
    """Base image for renovatebot/renovate.

    Era 1 (PRs <= 5615): node:10 — verified working, node:12 fails with ERR_PACKAGE_PATH_NOT_EXPORTED
    Era 2 (PRs 5616-14257): node:14 — verified working
    Era 3 (PRs >= 14258): node:18 — verified working, needs build tools for re2 native compilation

    Each era uses a distinct image_tag/workdir to avoid collisions in the
    dependency graph (different Node versions require separate Docker images).
    """

    def __init__(self, pr: PullRequest, config: Config, base_image: str, era: int):
        self._pr = pr
        self._config = config
        self._base_image = base_image
        self._era = era

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._base_image

    def image_tag(self) -> str:
        return f"base-era{self._era}"

    def workdir(self) -> str:
        return f"base-era{self._era}"

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

RUN if grep -q 'buster\\|stretch' /etc/apt/sources.list 2>/dev/null; then \
      sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
      sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
      sed -i '/stretch-updates/d' /etc/apt/sources.list && \
      sed -i '/buster-updates/d' /etc/apt/sources.list && \
      echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid; fi
RUN apt-get update && apt-get install -y git

{code}

{self.clear_env}

"""


class RenovateImageDefaultEra1(Image):
    """Per-PR image for Era 1: PRs #5046, #5105, #5196, #5615 (Dec 2019 - Mar 2020).

    Verified config:
    - Base: node:10
    - Package manager: yarn classic (yarn.lock)
    - Jest: v24.9.0 (5046/5105/5196) / v25.1.0 (5615)
    - Transform: babel-jest (.babelrc with @babel/preset-env + @babel/preset-typescript)
    - Test command: yarn jest --verbose
    - No native deps
    - PR #5615 has prepare script (node tools/prepare.js) that runs during yarn install
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

    def dependency(self) -> Image | None:
        return RenovateImageBase(self.pr, self.config, "node:10", era=1)

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

yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
yarn install --frozen-lockfile || true
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
yarn install --frozen-lockfile || true
yarn jest --verbose || true

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


class RenovateImageDefaultEra2(Image):
    """Per-PR image for Era 2: PR #14257 (Feb 2022).

    Verified config:
    - Base: node:14
    - Package manager: yarn classic (yarn.lock)
    - Jest: v27.5.1 with ts-jest v27.1.3
    - Jest config: jest.config.ts (TypeScript)
    - Transform: ts-jest preset (NOT babel-jest)
    - Test command: yarn jest --verbose
    - Has pretest script: run-s generate:*
    - Has check-re2.mjs post-install (but re2 not strictly required)
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

    def dependency(self) -> Image | None:
        return RenovateImageBase(self.pr, self.config, "node:14", era=2)

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

yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
yarn jest --verbose || true

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


class RenovateImageDefaultEra3(Image):
    """Per-PR image for Era 3: PR #23609 (Jul 2023).

    Verified config:
    - Base: node:18
    - Package manager: yarn classic (yarn.lock)
    - Jest: v29.6.1 with ts-jest v29.1.1 + @swc/core v1.3.70
    - Jest config: jest.config.ts (complex, with test sharding)
    - Transform: ts-jest + swc
    - Test command: yarn jest --verbose
    - Native dep: re2 (compiles from source via node-gyp, needs python3 make g++)
    - Install time: ~110-118s due to re2 compilation
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

    def dependency(self) -> Image | None:
        return RenovateImageBase(self.pr, self.config, "node:18", era=3)

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

apt update && apt install -y python3 make g++ || true
yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
yarn jest --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
yarn jest --verbose || true

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


@Instance.register("renovatebot", "renovate")
class Renovate(Instance):
    """Registry config for renovatebot/renovate.

    Era boundaries (verified via Docker):
    - Era 1: PRs <= 5615 → node:10, babel-jest, Jest 24-25
    - Era 2: PRs 5616-14257 → node:14, ts-jest, Jest 27
    - Era 3: PRs >= 14258 → node:18, ts-jest+swc, Jest 29, re2 native dep

    Parse log format (consistent across all eras):
    - Suite level: PASS/FAIL <filepath> (optional heap size info)
    - Test level: ✓/✕ <test name> (optional timing)
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 5615:
            return RenovateImageDefaultEra1(self.pr, self._config)
        elif self.pr.number <= 14257:
            return RenovateImageDefaultEra2(self.pr, self._config)
        else:
            return RenovateImageDefaultEra3(self.pr, self._config)

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

        current_suite = None

        # Suite-level patterns (verified against actual Jest output from all 3 eras)
        # Era 2 appends heap size: "PASS lib/datasource/conda/index.spec.ts (163 MB heap size)"
        re_pass_suite = re.compile(r"^PASS (\S+)(\s*\(.+\))?$")
        re_fail_suite = re.compile(r"^FAIL (\S+)(\s*\(.+\))?$")

        # Individual test patterns (verified: ✓ for pass, ✕ for fail)
        # Only strip actual timing suffix like (27ms), (0.5s) — NOT descriptive parens like (implicit)
        re_pass_test = re.compile(r"^✓ (.+?)(\s+\(\d[\d.]*\s*m?s\))?$")
        re_fail_test = re.compile(r"^✕ (.+?)(\s+\(\d[\d.]*\s*m?s\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)

            pass_test_match = re_pass_test.match(line)
            if pass_test_match:
                if current_suite is None:
                    raise ValueError(f"Test passed without suite: {line}")

                test = f"{current_suite}:{pass_test_match.group(1)}"
                passed_tests.add(test)

            fail_test_match = re_fail_test.match(line)
            if fail_test_match:
                if current_suite is None:
                    raise ValueError(f"Test failed without suite: {line}")

                test = f"{current_suite}:{fail_test_match.group(1)}"
                failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
