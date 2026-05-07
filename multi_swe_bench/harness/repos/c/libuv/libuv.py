import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LibuvImageBase(Image):
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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /testbed"
        else:
            code = f"COPY {self.pr.repo} /testbed"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /testbed
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    autoconf \
    automake \
    libtool \
    pkg-config \
    sudo \
    patch \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash testuser && echo 'testuser ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

{code}

RUN git config --global --add safe.directory '*'

{self.clear_env}

"""


class LibuvImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "Image":
        return LibuvImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /testbed
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

cd /testbed
./autogen.sh
./configure
make -j$(nproc)
chown -R testuser:testuser /testbed
su testuser -c "cd /testbed && make check"
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /testbed
git apply --whitespace=nowarn /home/test.patch
./autogen.sh
./configure
make -j$(nproc)
chown -R testuser:testuser /testbed
su testuser -c "cd /testbed && make check"

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /testbed
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || patch --batch --fuzz=5 -p1 -i /home/test.patch -i /home/fix.patch || true
./autogen.sh
./configure
make -j$(nproc)
chown -R testuser:testuser /testbed
su testuser -c "cd /testbed && make check"

""",
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


@Instance.register("libuv", "libuv")
class Libuv(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LibuvImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_skip_tests = re.compile(r'^(?:\d+:\s*)?ok\s+\d+\s*-\s*(\S+)\s*#\s*SKIP')
        re_pass_tests = re.compile(r'^(?:\d+:\s*)?ok\s+\d+\s*-\s*(\S+)\s*$')
        re_fail_tests = re.compile(r'^(?:\d+:\s*)?not ok\s+\d+\s*-\s*(\S+)')

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            matched = False

            skip_match = re_skip_tests.match(line)
            if skip_match:
                test = skip_match.group(1)
                skipped_tests.add(test)
                matched = True

            if not matched:
                pass_match = re_pass_tests.match(line)
                if pass_match:
                    test = pass_match.group(1)
                    passed_tests.add(test)
                    matched = True

            if not matched:
                fail_match = re_fail_tests.match(line)
                if fail_match:
                    test = fail_match.group(1)
                    failed_tests.add(test)

        # Detect build/infrastructure failures
        linker_error_pattern = re.compile(r"undefined reference to `([^']+)'")
        compile_error_pattern = re.compile(r'^(/\S+\.\w+):\d+:\d+: error:')
        make_error_pattern = re.compile(r'^make(?:\[\d+\])?: \*\*\* .+ Error \d+')
        patch_fail_pattern = re.compile(r'^error: patch failed: (\S+)')
        patch_no_apply_pattern = re.compile(r'^error: (\S+): patch does not apply')
        cmake_error_pattern = re.compile(r'^CMake Error')
        syntax_error_pattern = re.compile(r'syntax error')
        hunk_fail_pattern = re.compile(r'FAILED --')
        configure_error_pattern = re.compile(r'^configure: error:')

        detected_failures = set()
        for line in test_log.splitlines():
            line = line.strip()

            m = linker_error_pattern.search(line)
            if m:
                detected_failures.add(f"__linker_error_{m.group(1)}")

            m = compile_error_pattern.search(line)
            if m:
                detected_failures.add(f"__compile_error_{m.group(1)}")

            m = patch_fail_pattern.search(line)
            if m:
                detected_failures.add(f"__patch_failed_{m.group(1)}")

            m = patch_no_apply_pattern.search(line)
            if m:
                detected_failures.add(f"__patch_no_apply_{m.group(1)}")

            m = make_error_pattern.search(line)
            if m:
                detected_failures.add("__make_error__")

            m = cmake_error_pattern.search(line)
            if m:
                detected_failures.add("__cmake_error__")

            m = syntax_error_pattern.search(line)
            if m:
                detected_failures.add("__syntax_error__")

            m = hunk_fail_pattern.search(line)
            if m:
                detected_failures.add("__hunk_failed__")

            m = configure_error_pattern.search(line)
            if m:
                detected_failures.add("__configure_error__")

        if detected_failures and len(passed_tests) == 0 and len(failed_tests) == 0:
            failed_tests = detected_failures

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
