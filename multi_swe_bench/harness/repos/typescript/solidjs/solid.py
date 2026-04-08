import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_base_version(pr: PullRequest) -> tuple[int, ...]:
    label = pr.base.label
    if not label or ".." not in label:
        return (0, 0, 0)
    base_tag = label.split("..")[0]
    version_str = base_tag.lstrip("v")
    version_str = re.sub(r"[-+].*", "", version_str)
    parts = version_str.split(".")
    if not all(p.isdigit() for p in parts):
        return (0, 0, 0)

    return tuple(int(x) for x in parts)


# ---------------------------------------------------------------------------
# Era 1: npm + jest (no monorepo) — v0.4.2 through v0.15.0
# PRs: 24, 45, 48, 61, 86, 107
# ---------------------------------------------------------------------------

class ImageBaseEra1(Image):

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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

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
RUN apt update && apt install -y git

{code}

{self.clear_env}

"""


class ImageDefaultEra1(Image):

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
        return ImageBaseEra1(self.pr, self.config)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm run build || true
npx jest --verbose --ci || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm run build || true
npx jest --verbose --ci || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build || true
npx jest --verbose --ci || true

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


# ---------------------------------------------------------------------------
# Era 2: npm + lerna + jest — v0.15.2 through v1.4.8
# PRs: 127 through 1113
# ---------------------------------------------------------------------------

class ImageBaseEra2(Image):

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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

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
RUN apt update && apt install -y git

{code}

{self.clear_env}

"""


class ImageDefaultEra2(Image):

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
        return ImageBaseEra2(self.pr, self.config)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm run build || true
npx lerna run test --concurrency=1 --stream --no-bail || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm run build || true
npx lerna run test --concurrency=1 --stream --no-bail || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build || true
npx lerna run test --concurrency=1 --stream --no-bail || true

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


# ---------------------------------------------------------------------------
# Era 3: pnpm + turbo + jest — v1.5.0-beta.1 through v1.6.15
# PRs: 1182 through 1609 (base version < 1.6.16)
# ---------------------------------------------------------------------------

class ImageBaseEra3(Image):

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
        return "base-era3"

    def workdir(self) -> str:
        return "base-era3"

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
RUN apt update && apt install -y git
RUN npm install -g pnpm@8

{code}

{self.clear_env}

"""


class ImageDefaultEra3(Image):

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
        return ImageBaseEra3(self.pr, self.config)

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
pnpm install --no-frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm run build || true
npx turbo run test --continue || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm run build || true
npx turbo run test --continue || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm run build || true
npx turbo run test --continue || true

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


# ---------------------------------------------------------------------------
# Era 4: pnpm + turbo + vitest — v1.6.16 onward
# PRs: 1563 onward (base version >= 1.6.16)
# ---------------------------------------------------------------------------

class ImageBaseEra4(Image):

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
        return "base-era4"

    def workdir(self) -> str:
        return "base-era4"

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
RUN apt update && apt install -y git
RUN npm install -g pnpm@9

{code}

{self.clear_env}

"""


class ImageDefaultEra4(Image):

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
        return ImageBaseEra4(self.pr, self.config)

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
pnpm install --no-frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm run build || true
npx turbo run test --continue || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm run build || true
npx turbo run test --continue || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm run build || true
npx turbo run test --continue || true

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


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("solidjs", "solid")
class Solid(Instance):
    _VITEST_VERSION = (1, 6, 16)
    _PNPM_VERSION = (1, 5, 0)
    _LERNA_VERSION = (0, 15, 2)

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._base_version = _parse_base_version(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _era(self) -> int:
        v = self._base_version
        if v < self._LERNA_VERSION:
            return 1  # npm + jest (no monorepo)
        elif v < self._PNPM_VERSION:
            return 2  # npm + lerna + jest
        elif v < self._VITEST_VERSION:
            return 3  # pnpm + turbo + jest
        else:
            return 4  # pnpm + turbo + vitest

    def dependency(self) -> Optional[Image]:
        era = self._era()
        if era == 1:
            return ImageDefaultEra1(self.pr, self._config)
        elif era == 2:
            return ImageDefaultEra2(self.pr, self._config)
        elif era == 3:
            return ImageDefaultEra3(self.pr, self._config)
        else:
            return ImageDefaultEra4(self.pr, self._config)

    def _test_cmd(self) -> str:
        era = self._era()
        if era == 1:
            return "npm run build || true; npx jest --verbose --ci || true"
        elif era == 2:
            return "npm run build || true; npx lerna run test --concurrency=1 --stream --no-bail || true"
        elif era == 3:
            return "pnpm run build || true; npx turbo run test --continue || true"
        else:
            return "pnpm run build || true; npx turbo run test --continue || true"

    def _install_ensure(self) -> str:
        era = self._era()
        if era == 1:
            return "[ -d node_modules ] || npm install || true"
        elif era == 2:
            return "[ -d node_modules ] || npm install || true"
        else:
            return "[ -d node_modules ] || pnpm install --no-frozen-lockfile || true"

    def _lockfile_exclude(self) -> str:
        era = self._era()
        if era <= 2:
            return "--exclude package-lock.json"
        else:
            return "--exclude pnpm-lock.yaml"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f"bash -c 'cd /home/{self.pr.repo}; {self._install_ensure()}; {self._test_cmd()}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return f"bash -c 'cd /home/{self.pr.repo}; git apply {self._lockfile_exclude()} --whitespace=nowarn /home/test.patch; {self._install_ensure()}; {self._test_cmd()}'"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return f"bash -c 'cd /home/{self.pr.repo}; git apply {self._lockfile_exclude()} --whitespace=nowarn /home/test.patch /home/fix.patch; {self._install_ensure()}; {self._test_cmd()}'"

    @staticmethod
    def _detect_era_from_log(test_log: str) -> int:
        """Auto-detect era from log content when base version is unavailable."""
        if re.search(r"[✓✔]\s+\S+\.(?:test|spec)\.", test_log):
            return 4
        if "vitest" in test_log.lower():
            return 4
        return 3

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        era = self._era()
        if self._base_version == (0, 0, 0):
            era = self._detect_era_from_log(test_log)

        if era <= 3:
            re_pass_file = re.compile(r"PASS\s+(\S+\.(?:test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail_file = re.compile(r"FAIL\s+(\S+\.(?:test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                clean = re.sub(r"^(?:[\w@/.-]+:\s+)?", "", clean)

                pass_file = re_pass_file.search(clean)
                if pass_file:
                    passed_tests.add(pass_file.group(1))
                    continue

                fail_file = re_fail_file.search(clean)
                if fail_file:
                    failed_tests.add(fail_file.group(1))
                    continue

        else:
            # Vitest output parsing (Era 4)
            # File-level: ✓ packages/solid/test/signals.spec.ts (N tests)
            # File-level: × packages/solid/test/signals.spec.ts (N tests)
            re_pass_file = re.compile(r"[✓✔]\s+(\S+\.(?:test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail_file = re.compile(r"[❯×✗]\s+(\S+\.(?:test|spec)\.(?:tsx|ts|jsx|js))")
            re_skip_file = re.compile(r"[↓⊘]\s+(\S+\.(?:test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                # Strip turbo prefix: e.g. solid-js:test: or solid#test:
                clean = re.sub(r"^[\w@/.-]+[#:]\w+:\s*", "", clean)

                pass_file = re_pass_file.search(clean)
                if pass_file:
                    passed_tests.add(pass_file.group(1))
                    continue

                fail_file = re_fail_file.search(clean)
                if fail_file:
                    failed_tests.add(fail_file.group(1))
                    continue

                skip_file = re_skip_file.search(clean)
                if skip_file:
                    skipped_tests.add(skip_file.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
