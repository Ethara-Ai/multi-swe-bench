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
        return "node:22"

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

RUN apt-get update && apt-get install -y --no-install-recommends netcat-openbsd less && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageBaseOld(Image):
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
        return "base-old"
    def workdir(self) -> str:
        return "base-old"
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
RUN apt-get update && apt-get install -y --no-install-recommends netcat-openbsd less && rm -rf /var/lib/apt/lists/*
WORKDIR /home/
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

npm ci || npm install
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm run build
npm run test:coverage -- --reporter=verbose
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm install
if [ -f tsconfig.json ]; then
  node -e "let f=require('fs'),j=JSON.parse(f.readFileSync('tsconfig.json','utf8'));j.compilerOptions=j.compilerOptions||{{}};j.compilerOptions.skipLibCheck=true;f.writeFileSync('tsconfig.json',JSON.stringify(j,null,2))"
fi
npm run build
npm run test:coverage -- --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm install
if [ -f tsconfig.json ]; then
  node -e "let f=require('fs'),j=JSON.parse(f.readFileSync('tsconfig.json','utf8'));j.compilerOptions=j.compilerOptions||{{}};j.compilerOptions.skipLibCheck=true;f.writeFileSync('tsconfig.json',JSON.stringify(j,null,2))"
fi
npm run build
npm run test:coverage -- --reporter=verbose

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


class ImageDefault849(Image):
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

npm ci || npm install
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
if [ -f tsconfig.json ]; then
  node -e "let f=require('fs'),j=JSON.parse(f.readFileSync('tsconfig.json','utf8'));j.compilerOptions=j.compilerOptions||{{}};j.compilerOptions.skipLibCheck=true;f.writeFileSync('tsconfig.json',JSON.stringify(j,null,2))"
fi
timeout 300 npm test -- --reporter=verbose || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm install
if [ -f tsconfig.json ]; then
  node -e "let f=require('fs'),j=JSON.parse(f.readFileSync('tsconfig.json','utf8'));j.compilerOptions=j.compilerOptions||{{}};j.compilerOptions.skipLibCheck=true;f.writeFileSync('tsconfig.json',JSON.stringify(j,null,2))"
fi
timeout 300 npm test -- --reporter=verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm install
if [ -f tsconfig.json ]; then
  node -e "let f=require('fs'),j=JSON.parse(f.readFileSync('tsconfig.json','utf8'));j.compilerOptions=j.compilerOptions||{{}};j.compilerOptions.skipLibCheck=true;f.writeFileSync('tsconfig.json',JSON.stringify(j,null,2))"
fi
timeout 300 npm test -- --reporter=verbose || true

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


@Instance.register("google", "zx")
class Zx(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 849:
            return ImageDefault849(self.pr, self._config)

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
        stack = []

        for line in test_log.splitlines():
            if not line.strip():
                continue

            m_subtest = re.match(r"^(\s*)# Subtest:\s*(.+)", line)
            if m_subtest:
                indent = len(m_subtest.group(1))
                sub_test_name = m_subtest.group(2).strip()
                level = indent // 4
                stack = stack[:level]  # 弹出深层次测试
                stack.append(sub_test_name)
                continue

            m_test = re.match(r"^\s*(ok|not ok)\s+\d+\s+-\s+(.+)", line)
            if m_test:
                status = m_test.group(1)
                test_name = m_test.group(2).strip()
                full_test_name = (
                    ":".join(stack[:-1] + [test_name]) if stack else test_name
                )

                if status == "ok":
                    passed_tests.add(full_test_name)
                else:
                    failed_tests.add(full_test_name)
                continue

        # Fallback: parse uvu test runner output
        if not passed_tests and not failed_tests:
            current_suite = ""
            for line in test_log.splitlines():
                stripped = line.strip()
                clean = re.sub(r'\x1b\[[0-9;]*m', '', stripped)
                if not clean.strip():
                    continue

                suite_match = re.match(
                    r'^([\w][\w.-]+\.(?:test|spec)\.\w+)$', clean.strip()
                )
                if suite_match:
                    current_suite = suite_match.group(1)
                    continue

                summary_match = re.search(
                    r'\(\s*(\d+)\s*/\s*(\d+)\s*\)', clean
                )
                if summary_match and current_suite:
                    passed = int(summary_match.group(1))
                    total = int(summary_match.group(2))
                    failed = total - passed
                    if passed > 0:
                        passed_tests.add(f"{current_suite}:passed({passed})")
                    if failed > 0:
                        failed_tests.add(f"{current_suite}:failed({failed})")
                    current_suite = ""
                    continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
