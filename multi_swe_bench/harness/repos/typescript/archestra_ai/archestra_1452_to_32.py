"""archestra-ai/archestra config for PRs 32-1452 (pre-platform era, desktop/desktop_app)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ArchestraImageBaseDesktop(Image):
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
        return "base-desktop"

    def workdir(self) -> str:
        return "base-desktop"

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
# Electron requires GTK3 and other system libraries
RUN apt-get update && apt-get install -y git \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libgtk-3-0 libnotify4 libxss1 libxtst6 xdg-utils \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10

{code}

{self.clear_env}

"""


class ArchestraImageDefaultDesktop(Image):
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
        return ArchestraImageBaseDesktop(self.pr, self.config)

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

# Pre-platform era: working dir is desktop/ (PRs 32-68) or desktop_app/ (PR 329+)
if [ -d desktop_app ]; then
    cd desktop_app
elif [ -d desktop ]; then
    cd desktop
fi

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
export ELECTRON_RUN_AS_NODE=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
if [ -d desktop_app ]; then
    cd desktop_app
elif [ -d desktop ]; then
    cd desktop
fi

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
export ELECTRON_RUN_AS_NODE=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

if [ -d desktop_app ]; then
    cd desktop_app
elif [ -d desktop ]; then
    cd desktop
fi

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
export ELECTRON_RUN_AS_NODE=1
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

if [ -d desktop_app ]; then
    cd desktop_app
elif [ -d desktop ]; then
    cd desktop
fi

pnpm test
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ArchestraImageDefaultDesktop dependency must be an Image")
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


@Instance.register("archestra-ai", "archestra_1452_to_32")
class ARCHESTRA_1452_TO_32(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ArchestraImageDefaultDesktop(self.pr, self._config)

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

        # vitest standalone output: ✓ <filepath> (<N> tests) <time>
        re_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+.*\))?$", re.MULTILINE)
        re_fail = re.compile(r"^\s*[×✗]\s+(.+?)(?:\s+\(\d+.*\))?$", re.MULTILINE)

        for m in re_pass.finditer(clean_log):
            passed_tests.add(m.group(1).strip())
        for m in re_fail.finditer(clean_log):
            failed_tests.add(m.group(1).strip())

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
