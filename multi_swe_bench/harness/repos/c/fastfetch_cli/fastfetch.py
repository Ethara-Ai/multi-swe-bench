import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FastfetchImageBase(Image):
    """Level 1: toolchain-only base image (shared per era).

    dependency() returns a *string* (the Debian toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives in FastfetchImageDefault (an Image dependency, left
    verbatim by the enhancer), done per-PR. This image only provides the C/CMake
    build toolchain.

    debian:bookworm (GCC 12), NOT debian:latest (GCC 14): this dataset spans
    fastfetch releases 1.5 .. 2.52. Legacy C in the old releases (e.g.
    tests/strbuf.c calling exit() without <stdlib.h>) only WARNS on GCC 12 but
    is a hard ERROR on GCC 14 (-Werror=implicit-function-declaration is the
    default there), so the oldest PRs fail to compile at baseline on
    debian:latest. bookworm builds every release in range.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "debian:bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # fastfetch is a CMake/C project; build-essential and git are in the
        # default package set below.
        return ["cmake", "pkg-config"]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{apt_command}

CMD ["/bin/bash"]
"""


class FastfetchImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return FastfetchImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

    def files(self) -> list[File]:
        cmake_configure = "cmake -DBUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug .."

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
                "install.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at the base commit inline in the
# Dockerfile and hardened there; install.sh only resets the working tree and
# creates the out-of-source CMake build dir (runs before the hardening strip).
set -e

cd /home/{pr.repo}
git reset --hard || true

mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

mkdir -p build
cd build
{cmake_configure}
cmake --build . -j$(nproc)
ctest --output-on-failure || true
""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

mkdir -p build
cd build
{cmake_configure} || true
cmake --build . -j$(nproc) -- -k 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

mkdir -p build
cd build
{cmake_configure} || true
cmake --build . -j$(nproc) -- -k 2>&1 || true
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr, cmake_configure=cmake_configure),
            ),
        ]


@Instance.register("fastfetch-cli", "fastfetch")
class Fastfetch(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FastfetchImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

        # Deduplicate: if a test appears in both passed and failed, treat as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# number_interval values are the hyphen-joined prs_in_bundle for each selected
# PR (from fastfetch-cli__fastfetch_lht_final.jsonl). Instance.create() routes on
# f"{org}/{number_interval}", so every interval that a PR can carry must be
# registered to the same Fastfetch config (in addition to "fastfetch-cli/fastfetch").
_NUMBER_INTERVALS = [
    "361-362-363",
    "1785-1789-1796-1802-1804-1806-1811-1813",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("fastfetch-cli", _interval)(Fastfetch)
