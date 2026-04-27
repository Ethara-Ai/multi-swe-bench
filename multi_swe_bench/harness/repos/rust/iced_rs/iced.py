import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class IcedImageBase(Image):
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
        return "rust:latest"

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

RUN apt-get update && apt-get install -y \\
    libxkbcommon-dev \\
    libgtk-3-dev \\
    pkg-config \\
    cmake \\
    libfontconfig1-dev \\
    libfreetype6-dev \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class IcedImageDefault(Image):
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
        return IcedImageBase(self.pr, self.config)

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

# Build workspace packages to cache dependencies.
# Use dynamic package discovery to avoid broken crates (e.g. iced_web/dodrio).
PKGS=$(cargo metadata --no-deps --format-version 1 2>/dev/null | \\
  python3 -c "import json,sys,re; d=json.load(sys.stdin); members=set(); \\
[members.add(re.search(r'#(.+?)@', m).group(1)) for m in d.get('workspace_members', []) if re.search(r'#(.+?)@', m)]; \\
pkgs=sorted(p for p in members if p.startswith('iced') and p != 'iced_web'); \\
print(' '.join('-p ' + p for p in pkgs))")
cargo test $PKGS || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Dynamically discover iced_* workspace members, excluding iced_web
# which has broken transitive dependencies on older commits.
PKGS=$(cargo metadata --no-deps --format-version 1 2>/dev/null | \\
  python3 -c "import json,sys,re; d=json.load(sys.stdin); members=set(); \\
[members.add(re.search(r'#(.+?)@', m).group(1)) for m in d.get('workspace_members', []) if re.search(r'#(.+?)@', m)]; \\
pkgs=sorted(p for p in members if p.startswith('iced') and p != 'iced_web'); \\
print(' '.join('-p ' + p for p in pkgs))")
cargo test $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch

PKGS=$(cargo metadata --no-deps --format-version 1 2>/dev/null | \\
  python3 -c "import json,sys,re; d=json.load(sys.stdin); members=set(); \\
[members.add(re.search(r'#(.+?)@', m).group(1)) for m in d.get('workspace_members', []) if re.search(r'#(.+?)@', m)]; \\
pkgs=sorted(p for p in members if p.startswith('iced') and p != 'iced_web'); \\
print(' '.join('-p ' + p for p in pkgs))")
cargo test $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch

PKGS=$(cargo metadata --no-deps --format-version 1 2>/dev/null | \\
  python3 -c "import json,sys,re; d=json.load(sys.stdin); members=set(); \\
[members.add(re.search(r'#(.+?)@', m).group(1)) for m in d.get('workspace_members', []) if re.search(r'#(.+?)@', m)]; \\
pkgs=sorted(p for p in members if p.startswith('iced') and p != 'iced_web'); \\
print(' '.join('-p ' + p for p in pkgs))")
cargo test $PKGS

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


@Instance.register("iced-rs", "iced")
class Iced(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IcedImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

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
