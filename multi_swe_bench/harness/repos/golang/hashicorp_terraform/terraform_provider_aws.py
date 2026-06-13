import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# hashicorp/terraform-provider-aws — the AWS Terraform provider (large Go repo).
#
# Discovery (GitHub API + dataset analysis):
#  - 189-PR range #12145..#46760. go.mod spans `go 1.20` -> `go 1.26`; a
#    recent Go toolchain with GOTOOLCHAIN=auto builds the whole range (Go is
#    backward compatible and auto-fetches a newer toolchain when a go.mod asks).
#  - Tests are Go's `go test`. `TestAcc*` are acceptance tests that create real
#    AWS resources and only run when TF_ACC=1 — the registry never sets it, so
#    they SKIP and the unit `Test*` functions run. A PR resolves on a unit
#    test going !PASS->PASS.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` is run on each. Runs are fenced with a `### TFPKG ###`
#    marker so test ids stay unique across packages.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class TerraformProviderAwsImageBase(Image):
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
        return "golang:1-bookworm"

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

ENV DEBIAN_FRONTEND=noninteractive
# Let Go auto-fetch whatever toolchain a given PR's go.mod requests.
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class TerraformProviderAwsImageDefault(Image):
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
        return TerraformProviderAwsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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
"""

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the Go module cache at the base commit (cached into the image layer).
go mod download 2>/dev/null || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Per-package `go test`. No TF_ACC -> TestAcc* skip, unit Test* run.
        # -vet=off matches the upstream Makefile and avoids vet-only failures.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### TFPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.woff --exclude=*.woff2"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("hashicorp", "terraform-provider-aws")
class TerraformProviderAws(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TerraformProviderAwsImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestFlattenFoo (0.00s)
        #   --- FAIL: TestExpandBar (0.01s)
        #   --- SKIP: TestAccBaz (0.00s)
        # Fenced by `### TFPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### TFPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
