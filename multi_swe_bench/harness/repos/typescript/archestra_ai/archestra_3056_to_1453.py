"""archestra-ai/archestra config for PRs 1453-3056 (platform era, turbo+vitest)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ArchestraImageBase(Image):
    """Base image for archestra platform era (PRs >= 1453)."""

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
        return "base-platform"

    def workdir(self) -> str:
        return "base-platform"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10

{code}

{self.clear_env}

"""


class ArchestraImageDefault(Image):
    """PR-specific image for archestra platform era."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return ArchestraImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

cd /home/{repo}/platform
git reset --hard
git checkout {base_sha}

pnpm install || true

if [ -f backend/vitest.config.ts ]; then
    sed -i 's/pool: "threads"/pool: "forks"/' backend/vitest.config.ts
    sed -i '/pool: "forks"/a\\    maxWorkers: 2,' backend/vitest.config.ts
fi
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export ARCHESTRA_DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}/platform
pnpm test
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export ARCHESTRA_DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}/platform
git apply --whitespace=nowarn /home/test.patch
pnpm test
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export ARCHESTRA_DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}/platform
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm test
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
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


@Instance.register("archestra-ai", "archestra_3056_to_1453")
class ARCHESTRA_3056_TO_1453(Instance):
    """Instance for archestra PRs 1453-3056 (platform era, turbo+vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ArchestraImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # vitest turbo output: @<pkg>:test:  ✓ <filepath> (<N> tests) <time>
        re_passed = re.compile(r"(@\w[\w\-]*):test:\s+✓\s+(\S+\.test\.(?:ts|tsx))")
        re_failed = re.compile(r"(@\w[\w\-]*):test:\s+×\s+(\S+\.test\.(?:ts|tsx))")
        re_fail_alt = re.compile(r"(@\w[\w\-]*):test:\s+FAIL\s+(\S+\.test\.(?:ts|tsx))")

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_passed.search(line)
            if m:
                passed_tests.add(f"{m.group(1)}:{m.group(2)}")
                continue

            m = re_failed.search(line) or re_fail_alt.search(line)
            if m:
                failed_tests.add(f"{m.group(1)}:{m.group(2)}")
                continue

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
