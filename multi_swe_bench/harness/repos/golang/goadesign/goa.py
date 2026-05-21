"""goadesign/goa — v3 era (module `goa.design/goa/v3`).

This is the DEFAULT config: records whose `number_interval` is empty (or "goa")
route here. It covers every `base.ref == "v3"` bundle (82 of 101 instances,
release tags v3.0.0 .. v3.26.0, go.mod directives go1.12 .. go1.24).

Verified interactively in Docker (golang:1.24-bookworm, linux/arm64):
  - latest base 57351f1 (v3.25.3..v3.26.0): `go test ./...` => 328 PASS / 0 FAIL / 2 SKIP, RC=0
  - oldest base 922f55e (v3.0.0..v3.0.1, go 1.12): builds & runs under modern Go
    via GOTOOLCHAIN=auto + GOFLAGS=-mod=mod (mixed pass/fail baseline — expected;
    the harness scores fix-induced transitions, not absolute pass).
A single golang:1.24 image covers the whole v3 range because a newer Go
toolchain builds older `go 1.x` modules, and GOTOOLCHAIN=auto fetches a newer
toolchain for any commit whose go.mod requires > 1.24. Hence: no v3 sub-eras.

protoc + protoc-gen-go + protoc-gen-go-grpc are installed in the base image:
goa's gRPC codegen tests shell out to `protoc` (e.g. grpc/codegen tests).
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GoaImageBase(Image):
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
        # Go 1.24 + GOTOOLCHAIN=auto: builds the whole v3 range (go1.12..go1.24)
        # and auto-fetches a newer toolchain if a commit's go.mod requires it.
        # golang:1.24-bookworm is a multi-arch manifest (linux/amd64 + linux/arm64).
        return "golang:1.24-bookworm"

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

        # Canonical, enhancer-aware repo fetch: ${{REPO_URL}} is injected by
        # DockerfileEnhancer (ARG REPO_URL defaults to the GitHub URL). Writing
        # it in this exact form means _standardize_repo_fetch() leaves it
        # untouched (its negative lookahead skips "${{REPO_URL}}") — no
        # hardcoded URL, multi-arch/proxy infra still injected by the enhancer.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
# Later v3 commits (≈v3.18+) introduce a `go.work` file. With go.work present,
# `go` refuses the `-mod=mod` flag from GOFLAGS ("-mod may only be set to
# readonly or vendor when in workspace mode"), so `go test ./...` aborts before
# producing any test output (run/test/fix all = 0,0,0). Disabling workspace
# mode keeps `-mod=mod` valid; modules still resolve via the repo's go.mod.
ENV GOWORK=off
ENV CI=true
# Go's signal-based async preemption (Go 1.14+) crashes under QEMU user-mode
# emulation (SIGSEGV/SIGILL with a register dump) when building linux/amd64 on
# an arm64 host. Disabling it makes cross-arch (QEMU) multiarch builds robust;
# native-arch builds are unaffected. Inherited by prepare/run/test/fix stages.
ENV GODEBUG=asyncpreemptoff=1

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates protobuf-compiler && rm -rf /var/lib/apt/lists/*

# goa's gRPC codegen tests invoke `protoc` with these plugins on PATH.
RUN GOTOOLCHAIN=auto go install google.golang.org/protobuf/cmd/protoc-gen-go@latest \\
 && GOTOOLCHAIN=auto go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest \\
 || true
ENV PATH="/go/bin:${{PATH}}"

WORKDIR /home/

{code}

{self.clear_env}

"""


class GoaImageDefault(Image):
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
        return GoaImageBase(self.pr, self.config)

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
git config --global --add safe.directory /home/{pr.repo} 2>/dev/null || true
git config user.email "msb@build" >/dev/null
git config user.name "msb-build" >/dev/null

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# v3 ships a root go.mod (module goa.design/goa/v3). Pre-warm the module and
# build caches so the scored runs don't pay download/compile latency.
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod
go mod download || true
go build ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true

git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn -3 /home/test.patch || true
go mod download || true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || ( git apply --whitespace=nowarn /home/fix.patch && git apply --whitespace=nowarn /home/test.patch ) \\
  || git apply --whitespace=nowarn -3 /home/test.patch /home/fix.patch || true
go mod download || true
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

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


@Instance.register("goadesign", "goa")
class GOA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GoaImageDefault(self.pr, self._config)

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
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        # `go test` without a TTY is normally clean, but strip ANSI just in case
        # a dependency's test reporter injects color codes.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Standard `go test -v` lines (subtests appear indented; .strip() handles it):
        #   --- PASS: TestName (0.00s)
        #   --- FAIL: TestName (0.00s)
        #   --- SKIP: TestName (0.00s)
        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
