import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_htop_version(base_label: str) -> tuple[int, int, int]:
    """Parse (major, minor, patch) from base.label like '2.2.0..3.0.0'."""
    first_tag = base_label.split("..")[0]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d+)\.(\d+)", first_tag)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    raise ValueError(f"Cannot parse htop version from base.label: {base_label}")


def _needs_fcommon(version: tuple[int, int, int]) -> bool:
    """GCC 10+ requires -fcommon for htop <= 2.x due to multiple definitions."""
    return version[0] <= 2


def _needs_python_is_python3(version: tuple[int, int, int]) -> bool:
    """htop 2.2.x uses MakeHeader.py with '#!/usr/bin/env python'."""
    return version[0] <= 2


def _get_extra_cflags(version: tuple[int, int, int]) -> str:
    if _needs_fcommon(version):
        return "CFLAGS='-fcommon' "
    return ""


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
        return "ubuntu:22.04"

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

        version = _parse_htop_version(self.pr.base.label)
        extra_pkgs = ""
        if _needs_python_is_python3(version):
            extra_pkgs = " python-is-python3"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && \\
    apt-get install -y automake autoconf libtool git make gcc g++ \\
    pkg-config libncurses5-dev libncursesw5-dev{extra_pkgs} \\
    && apt-get clean

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
        version = _parse_htop_version(self.pr.base.label)
        extra_cflags = _get_extra_cflags(version)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
./autogen.sh
{cflags}./configure
make clean
make -j$(nproc)
make distcheck
""".format(repo=self.pr.repo, cflags=extra_cflags),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
./autogen.sh
{cflags}./configure
make clean
make -j$(nproc)
make distcheck
""".format(repo=self.pr.repo, cflags=extra_cflags),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./autogen.sh
{cflags}./configure
make clean
make -j$(nproc)
make distcheck
""".format(repo=self.pr.repo, cflags=extra_cflags),
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


@Instance.register("htop-dev", "htop")
class Htop(Instance):
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

        # distcheck success: "htop-X.Y.Z archives ready for distribution: htop-X.Y.Z.tar.gz"
        re_distcheck_pass = re.compile(
            r"^.+archives ready for distribution:.*$"
        )
        re_fail = re.compile(r"^make.*\*\*\*.*Error")

        distcheck_passed = False
        build_failed = False

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            if re_distcheck_pass.match(line):
                distcheck_passed = True

            if re_fail.match(line):
                build_failed = True

        if distcheck_passed and not build_failed:
            passed_tests.add("distcheck")
        elif build_failed:
            failed_tests.add("distcheck")
        else:
            failed_tests.add("distcheck")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
