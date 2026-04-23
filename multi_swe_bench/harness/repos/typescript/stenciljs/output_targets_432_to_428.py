import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class StencilOutputTargetsNpmImageBase(Image):
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
        return "node:20"

    def image_tag(self) -> str:
        return "base-npm"

    def workdir(self) -> str:
        return "base-npm"

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

RUN npm install -g lerna

{code}

{self.clear_env}

"""


class StencilOutputTargetsNpmImageDefault(Image):
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
        return StencilOutputTargetsNpmImageBase(self.pr, self.config)

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
git fetch origin {pr.base.sha}
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || npm install --force || true
npx lerna bootstrap --no-ci --force-local || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
lerna run build || true
lerna run test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude='**/package-lock.json' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' --exclude='pnpm-workspace.yaml' /home/test.patch || git apply --reject --exclude='**/package-lock.json' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' --exclude='pnpm-workspace.yaml' /home/test.patch || true
npm install --legacy-peer-deps || npm install --force || true
npx lerna bootstrap --no-ci --force-local || true
lerna run build || true
lerna run test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude='**/package-lock.json' --exclude='package-lock.json' /home/test.patch /home/fix.patch || git apply --reject --exclude='**/package-lock.json' --exclude='package-lock.json' /home/test.patch /home/fix.patch || true
npm install -g pnpm || true
pnpm install || true
lerna run build || true
lerna run test || true

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


@Instance.register("stenciljs", "output-targets_432_to_428")
class StencilOutputTargetsNpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StencilOutputTargetsNpmImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        cleaned_log = ansi_escape.sub("", test_log)

        # Jest output format:
        #  PASS packages/angular-output-target/__tests__/foo.spec.ts
        #  FAIL packages/react-output-target/__tests__/bar.spec.ts
        #    ✓ should do something (5ms)
        #    ✕ should fail something (10ms)
        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[✓✔]\s+(.+)$"),
        ]

        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[×✕✗]\s+(.+)$"),
        ]

        skipped_res = [
            re.compile(r"^\s*[○⊘]\s+skipped\s+(.+)$"),
            re.compile(r"^SKIP:?\s?(.+?)\s"),
        ]

        ignore_res = [
            re.compile(r"^Test Suites:"),
            re.compile(r"^Tests:"),
            re.compile(r"^Snapshots:"),
            re.compile(r"^Time:"),
            re.compile(r"^Ran all"),
        ]

        for line in cleaned_log.splitlines():
            line = line.strip()
            if not line:
                continue

            skip = False
            for ignore_re in ignore_res:
                if ignore_re.match(line):
                    skip = True
                    break
            if skip:
                continue

            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
