import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        # Returning a string (rather than a chained Image) keeps this a single,
        # self-contained per-PR image and lets DockerfileEnhancer inject the
        # shared infra (ARG REPO_URL / BASE_COMMIT / TARGETARCH, env, labels)
        # after the FROM line. The build itself is owned by dockerfile() below,
        # which clones "${REPO_URL}", checks out "${BASE_COMMIT}", runs
        # extra_setup(), and embeds Image._HARDENING_BLOCK verbatim so the fix
        # commit can't be read out of git history at eval time.
        #
        # debian:bookworm (GCC 12), NOT debian:latest (GCC 14): this dataset
        # spans fastfetch releases 1.5 .. 2.52. Legacy C in the old releases
        # (e.g. tests/strbuf.c calling exit() without <stdlib.h>) only WARNS on
        # GCC 12 but is a hard ERROR on GCC 14 (-Werror=implicit-function-
        # declaration is the default there), so the oldest PRs fail to compile
        # at baseline on debian:latest. bookworm builds every release in range.
        return "debian:bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # fastfetch is a CMake/C project; build-essential and git are already in
        # the default package set in Image.dockerfile().
        return ["cmake", "pkg-config"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the runtime helper scripts + patches into /home/ and
        # creates the CMake build dir so the eval scripts run offline. The copied
        # files live outside /home/{repo}, so the hardening pass (which only
        # operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def dockerfile(self) -> str:
        # Self-contained Dockerfile generation for this registry. We deliberately
        # do NOT defer to the inherited Image.dockerfile(): the full build is
        # spelled out here so the hardening pass is explicit and guaranteed at
        # this layer. The hardening text is pulled verbatim from image.py
        # (Image._HARDENING_BLOCK) so it stays in lockstep with the shared policy
        # instead of drifting from a hand-copied duplicate.
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

        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )

        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        if extra_setup:
            sections.append(extra_setup)

        # Hardening block sourced verbatim from image.py. Strips every ref,
        # remote, and unreachable object (incl. submodules) so the fix commit
        # cannot be recovered from git history. MUST run after clone/checkout
        # and extra_setup so the working tree is at ${BASE_COMMIT} first.
        sections.append(Image._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

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
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# only resets the tree and creates the CMake build dir.
set -e

mkdir -p /home/{pr.repo}
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
