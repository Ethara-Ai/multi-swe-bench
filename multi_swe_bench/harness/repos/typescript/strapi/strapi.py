import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest/jest file-level metadata with optional trailing bare timing:
    #   (2 tests) 75ms, (1 test | 1 failed) 120ms, (2 tests | 2 skipped)
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class StrapiImageBaseV4(Image):

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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base-v4"

    def workdir(self) -> str:
        return "base-v4"

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

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class StrapiImageBaseV5(Image):

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-v5"

    def workdir(self) -> str:
        return "base-v5"

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

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN corepack enable

{code}

{self.clear_env}

"""


class StrapiImageDefaultV4(Image):

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
        return StrapiImageBaseV4(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git reset --hard
git checkout {base_sha}
echo ">>> yarn install"
yarn install || true
echo ">>> yarn build (|| true: pack-up may segfault under qemu but other packages succeed)"
yarn build || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
cd /home/{repo}
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
cd /home/{repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch
if git diff --name-only HEAD | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  yarn install || true
  yarn build || true
fi
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
cd /home/{repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch /home/fix.patch
if git diff --name-only HEAD | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  yarn install || true
  yarn build || true
fi
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


class StrapiImageDefaultV5(Image):

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
        return StrapiImageBaseV5(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git reset --hard
git checkout {base_sha}

# Yarn Berry (v4) setup
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules

# Enable corepack and set up yarn
corepack enable
echo ">>> yarn set version stable"
yarn set version stable
echo ">>> yarn install"
yarn install || true
echo ">>> yarn build (|| true: pack-up may segfault under qemu but other packages succeed)"
yarn build || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
cd /home/{repo}
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
cd /home/{repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch
if git diff --name-only HEAD | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  yarn install || true
  yarn build || true
fi
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
cd /home/{repo}
git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch /home/fix.patch
if git diff --name-only HEAD | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  yarn install || true
  yarn build || true
fi
yarn test:unit --verbose
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


# ---------------------------------------------------------------------------
# Instance — era switching: ref=="v4" is always v4; for develop/main,
# PR number disambiguates (v4 PRs <=22707, v5 PRs >=23302, no PRs in gap).
# ---------------------------------------------------------------------------


@Instance.register("strapi", "strapi")
class Strapi(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _is_v5(self) -> bool:
        if self.pr.base.ref == "v4":
            return False
        return self.pr.number >= 23302

    def dependency(self) -> Image:
        if self._is_v5():
            return StrapiImageDefaultV5(self.pr, self._config)
        return StrapiImageDefaultV4(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()

            # Strip NX/turbo monorepo task prefix:
            # @strapi/admin:test:unit: PASS ... → PASS ...
            stripped = re.sub(r"^@[\w\-/.]+:\S+:\s+", "", stripped)

            # Jest/Vitest file-level PASS/FAIL
            if stripped.startswith("PASS "):
                passed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue
            if stripped.startswith("FAIL "):
                failed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue

            # Test-level pass (✓/✔)
            m = re.match(
                r"\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                passed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Test-level fail (×/✕/✗)
            m = re.match(
                r"\s*[×✕✗]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                failed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Skipped (○)
            m = re.match(
                r"\s*○\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                skipped_tests.add(_clean_test_name(m.group(1)))
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
