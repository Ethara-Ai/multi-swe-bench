from __future__ import annotations
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FiberImageBase(Image):
    """Level 1: toolchain-only base image (shared per era).

    dependency() returns a *string* (the Go toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives in FiberImageDefault (an Image dependency, left
    verbatim by the enhancer), done per-PR. This image only provides the Go
    toolchain + apt deps + Go build env.

    go 1.16 / bullseye matches the v2.1.x era (go.mod `go 1.14`, Nov 2020).
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

    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.16-bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        packages_str = " \\\n    ".join(
            ["ca-certificates", "git", "build-essential"]
        )
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{apt_command}

ENV GOFLAGS=-mod=mod
ENV GOTOOLCHAIN=local

CMD ["/bin/bash"]
"""


class FiberImageDefault(Image):
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
        return FiberImageBase(self.pr, self._config)

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
                "install.sh",
                # Checkout is done inline in the Dockerfile; install.sh warms the
                # Go module cache (runs before the hardening strip, full clone
                # present). The cache lives in GOPATH, outside the git tree, so
                # hardening leaves it intact for offline test runs.
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

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


@Instance.register("gofiber", "fiber")
class Fiber(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FiberImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
