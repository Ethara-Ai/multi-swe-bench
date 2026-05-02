import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase6007to5331(Image):
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
        return "rust:1.79.0"

    def image_tag(self) -> str:
        return "base-rust179"

    def workdir(self) -> str:
        return "base-rust179"

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

RUN apt-get update && apt-get install -y gcc make

{code}

{self.clear_env}

"""


class ImageDefault6007to5331(Image):
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
        return ImageBase6007to5331(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _extract_test_filters(test_patch: str, fix_patch: str) -> str:
        filters = set()
        for patch in [test_patch, fix_patch]:
            if not patch:
                continue
            for line in patch.split("\n"):
                if line.startswith("diff --git"):
                    path = line.split(" b/")[-1] if " b/" in line else ""
                    m = re.search(r"tests/by-util/(test_\w+)", path)
                    if m:
                        filters.add(m.group(1))
                    m = re.search(r"src/uu/(\w+)/", path)
                    if m:
                        filters.add(f"test_{m.group(1)}")
        return " ".join(sorted(filters))

    def files(self) -> list[File]:
        test_filters = self._extract_test_filters(
            self.pr.test_patch, self.pr.fix_patch
        )
        cargo_base = "cargo test --features unix"
        if test_filters:
            cargo_test_cmd = " && ".join(
                f'{cargo_base} "{f}" -- --test-threads=1'
                for f in test_filters.split()
            )
        else:
            cargo_test_cmd = f"{cargo_base} -- --test-threads=1"

        git_apply_opts = "--binary --3way"

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", """#!/bin/bash
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
""".format()),
            File(".", "prepare.sh", """#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
{cargo_base} || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha, cargo_base=cargo_base)),
            File(".", "run.sh", """#!/bin/bash
set -e
cd /home/{repo}
{cmd}
""".format(repo=self.pr.repo, cmd=cargo_test_cmd)),
            File(".", "test-run.sh", """#!/bin/bash
set -e
cd /home/{repo}
git apply {apply_opts} /home/test.patch || git apply --binary /home/test.patch || git apply /home/test.patch
touch -c src/uu/*/src/*.rs tests/by-util/*.rs Cargo.toml Cargo.lock 2>/dev/null
{cmd}
""".format(repo=self.pr.repo, cmd=cargo_test_cmd, apply_opts=git_apply_opts)),
            File(".", "fix-run.sh", """#!/bin/bash
set -e
cd /home/{repo}
git apply {apply_opts} /home/test.patch /home/fix.patch || git apply --binary /home/test.patch /home/fix.patch || git apply /home/test.patch /home/fix.patch
touch -c src/uu/*/src/*.rs tests/by-util/*.rs Cargo.toml Cargo.lock 2>/dev/null
{cmd}
""".format(repo=self.pr.repo, cmd=cargo_test_cmd, apply_opts=git_apply_opts)),
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


@Instance.register("uutils", "coreutils_6007_to_5331")
class Coreutils6007to5331(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault6007to5331(self.pr, self._config)

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
        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]
        for line in test_log.splitlines():
            line = line.strip()
            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))
            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))
            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
