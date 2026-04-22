from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "ubuntu:latest"

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo} && "
                f"cd /home/{self.pr.repo} && git fetch --all"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt update && apt install -y git curl && \
    curl -fsSL https://deb.nodesource.com/setup_16.x | bash - && \
    apt install -y nodejs
     
{code}

{self.clear_env}

"""


class ImageDefault(Image):
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

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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
git checkout {pr.base.sha} || (git fetch origin +refs/pull/{pr.number}/head:refs/heads/pr-{pr.number} && git checkout {pr.base.sha})
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || true
npm install --no-save @babel/plugin-proposal-private-property-in-object @babel/plugin-proposal-private-methods --legacy-peer-deps || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
npm test -- --verbose
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
MD5_BEFORE=$(md5sum package.json 2>/dev/null | cut -d' ' -f1)
git apply --whitespace=nowarn /home/test.patch
MD5_AFTER=$(md5sum package.json 2>/dev/null | cut -d' ' -f1)
if [ "$MD5_BEFORE" != "$MD5_AFTER" ]; then
  rm -rf node_modules
  npm install --legacy-peer-deps || true
  npm install --no-save @babel/plugin-proposal-private-property-in-object @babel/plugin-proposal-private-methods --legacy-peer-deps || true
fi
npm test -- --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
MD5_BEFORE=$(md5sum package.json 2>/dev/null | cut -d' ' -f1)
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
MD5_AFTER=$(md5sum package.json 2>/dev/null | cut -d' ' -f1)
if [ "$MD5_BEFORE" != "$MD5_AFTER" ]; then
  rm -rf node_modules
  npm install --legacy-peer-deps || true
  npm install --no-save @babel/plugin-proposal-private-property-in-object @babel/plugin-proposal-private-methods --legacy-peer-deps || true
fi
npm test -- --verbose

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


@Instance.register("downshift-js", "downshift")
class Downshift(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        cleaned_log = ansi_escape.sub("", test_log)

        file_header = re.compile(
            r"^(PASS|FAIL)\s+(.+)$"
        )
        test_line = re.compile(
            r"^(\s*)(✓|✕|○)\s+(.*?)\s*(?:\(\d+\s*ms\))?\s*$"
        )
        describe_line = re.compile(
            r"^(\s+)(\S.*)$"
        )

        current_file = ""
        describe_stack: list[tuple[int, str]] = []

        for line in cleaned_log.split("\n"):
            fm = file_header.match(line)
            if fm:
                current_file = fm.group(2).strip()
                describe_stack = []
                continue

            tm = test_line.match(line)
            if tm:
                indent = len(tm.group(1))
                marker = tm.group(2)
                name = tm.group(3).strip()
                if not name:
                    continue

                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()

                prefix_parts = [current_file] if current_file else []
                prefix_parts.extend(d[1] for d in describe_stack)
                prefix_parts.append(name)
                full_name = " > ".join(prefix_parts)

                if marker == "✓":
                    passed_tests.add(full_name)
                elif marker == "✕":
                    failed_tests.add(full_name)
                elif marker == "○":
                    skipped_tests.add(full_name)
                continue

            if current_file and not tm and not fm:
                dm = describe_line.match(line)
                if dm:
                    indent = len(dm.group(1))
                    text = dm.group(2).strip()
                    if text and not text.startswith("●") and not text.startswith("at ") and not text.startswith("expect("):
                        while describe_stack and describe_stack[-1][0] >= indent:
                            describe_stack.pop()
                        describe_stack.append((indent, text))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
