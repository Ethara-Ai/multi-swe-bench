"""remotion-dev/remotion config for PRs 4947-5474 (bun era, pnpm@8)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RemotionImageBaseBun(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-bun"

    def workdir(self) -> str:
        return "base-bun"

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
RUN apt-get update && apt-get install -y git ffmpeg unzip && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@8
RUN curl -fsSL https://bun.sh/install | bash
ENV BUN_INSTALL=/root/.bun
ENV PATH="/root/.bun/bin:$PATH"

{code}

{self.clear_env}

"""


class RemotionImageDefaultBun(Image):
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
        return RemotionImageBaseBun(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
git checkout {base_sha}

echo 'node-linker=hoisted' >> .npmrc
pnpm install || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export BUN_INSTALL=/root/.bun
export PATH="/root/.bun/bin:$PATH"

cd /home/{repo}
npx turbo run test --no-update-notifier
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export BUN_INSTALL=/root/.bun
export PATH="/root/.bun/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
npx turbo run test --no-update-notifier
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export BUN_INSTALL=/root/.bun
export PATH="/root/.bun/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx turbo run test --no-update-notifier
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("RemotionImageDefaultBun dependency must be an Image")
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


@Instance.register("remotion-dev", "remotion_5474_to_4947")
class REMOTION_5474_TO_4947(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return RemotionImageDefaultBun(self.pr, self._config)

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

        # bun: <pkg>:test: (pass) <test name> [<time>]
        re_bun_pass = re.compile(
            r"([\w@/\-]+):test:\s+\(pass\)\s+(.+?)\s+\[[\d.]+(?:ms|s)\]"
        )
        # bun: <pkg>:test: (fail) <test name> [<time>]
        re_bun_fail = re.compile(
            r"([\w@/\-]+):test:\s+\(fail\)\s+(.+?)\s+\[[\d.]+(?:ms|s)\]"
        )

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_bun_pass.search(line)
            if m:
                passed_tests.add(f"{m.group(1)}:{m.group(2)}")
                continue

            m = re_bun_fail.search(line)
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
