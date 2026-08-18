"""CycloneDX/sbom-utility harness (single era, PR #40).

sbom-utility is a pure-Go CLI (``go 1.20`` at the relevant base commit) with no
cgo packages, so no extra system libraries are needed beyond a Go toolchain and
git. All test changes for the PR live in the ``cmd`` package; the fix also
touches supporting packages (``log``, ``schema``, ``utils``) that ``cmd`` depends
on. The test command is scoped to the directories changed by the patches so the
run stays focused on the relevant code.

Base image is ``golang:1.24-bookworm`` with ``GOTOOLCHAIN=auto`` so the
toolchain self-selects for the commit's ``go.mod`` (declared ``go 1.20``).
``go vet`` is disabled during tests -- old-era code and modern toolchains can
disagree on some vet checks.

The tests load JSON schema/config fixtures from paths relative to the repo
root, so all scripts ``cd /home/sbom-utility`` (the WORKDIR / clone location)
before invoking ``go test``.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.24-bookworm"
_TAG_SUFFIX = "go"


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f"RUN git clone "
                f"https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # "fat base / thin pr" layout (matches go-kit/iotex): intentionally omit the
        # "# syntax" directive and use a plain "git clone <url> /home/<repo>" line so
        # DockerfileEnhancer takes over the BASE image -- it injects the syntax
        # directive, TARGETARCH/REPO_URL/BASE_COMMIT ARGs + proxy/cert setup, and
        # rewrites the clone into: clone -> git checkout ${{BASE_COMMIT}} -> hardening
        # (+ CMD). Keep only the Go-specific env the enhancer does not provide.
        return f"""FROM {image_name}

ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod

WORKDIR /home/

{code}
"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = f"""#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
git checkout {sha}
go mod download 2>/dev/null || true
"""

        run_tests = f"""#!/bin/bash
set -uo pipefail
cd /home/{repo}
go mod download 2>/dev/null || true
PKGS=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u | sed 's|^|./|' | grep -v '^\\.\\?$')
PKGS=$(for p in $PKGS; do [ -d "${{p#./}}" ] && echo "$p"; done 2>/dev/null || true)
if [ -z "$PKGS" ]; then
  PKGS="./..."
fi
go test -vet=off -v -count=1 -timeout 20m $PKGS
"""

        run_sh = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
bash /home/run_tests.sh
"""

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
"""

        fix_run = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --3way --whitespace=nowarn {excludes} /home/test.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
git apply --3way --whitespace=nowarn {excludes} /home/fix.patch \\
  || git apply --whitespace=nowarn --reject {excludes} /home/fix.patch \\
  || echo "git apply fix.patch failed (continuing)"
bash /home/run_tests.sh
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Thin pr image (matches go-kit/iotex): the base image already checked out
        # BASE_COMMIT and applied the git hardening (via DockerfileEnhancer), so the
        # pr layer only copies the scripts and runs prepare.sh.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")

    for line in clean.splitlines():
        line = line.rstrip()
        m = re_result.match(line)
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "PASS":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        elif status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "SKIP":
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    passed_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("CycloneDX", "sbom-utility")
class SbomUtility(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
