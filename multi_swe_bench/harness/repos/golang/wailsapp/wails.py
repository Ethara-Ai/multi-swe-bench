import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class WailsImageBase(Image):
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
        return "golang:1.22"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "libgtk-3-dev",
            "libwebkit2gtk-4.0-dev",
            "pkg-config",
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y libgtk-3-dev libwebkit2gtk-4.0-dev build-essential pkg-config

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class WailsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return WailsImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

# Create dummy frontend/dist dirs for go:embed patterns that reference them
grep -rn 'go:embed.*frontend/dist' v2/ 2>/dev/null | grep '\\.go:' | while IFS=: read -r f rest; do
  dir=$(dirname "$f")/frontend/dist
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done

cd v2
go mod download

# Get buildable packages (exclude windows-only packages that can't build on linux)
PKGS=$(go list ./... 2>/dev/null | grep -v '/frontend/desktop/windows' | grep -v 'go-webview2' | grep -v 'go-common-file-dialog')
go test -v -count=1 $PKGS || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
# Create dummy frontend/dist dirs for go:embed patterns that reference them
grep -rn 'go:embed.*frontend/dist' v2/ 2>/dev/null | grep '\\.go:' | while IFS=: read -r f rest; do
  dir=$(dirname "$f")/frontend/dist
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done
cd v2
# Get buildable packages (exclude windows-only packages that can't build on linux)
PKGS=$(go list ./... 2>/dev/null | grep -v '/frontend/desktop/windows' | grep -v 'go-webview2' | grep -v 'go-common-file-dialog')
go test -v -count=1 $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}

# Apply patches tolerantly: filter out binary hunks, use --3way for context mismatches
apply_patch_tolerant() {{
    local patch="$1"
    if [ ! -s "$patch" ]; then
        return 0
    fi
    # First try normal apply
    if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
        return 0
    fi
    # Filter out binary patch hunks and retry
    local filtered=$(mktemp)
    awk '
        /^diff --git/ {{ in_binary=0; buf=$0"\\n"; next }}
        /^(GIT binary patch|Binary files)/ {{ in_binary=1; buf=""; next }}
        in_binary {{ next }}
        buf != "" {{ printf "%s", buf; buf="" }}
        {{ print }}
    ' "$patch" > "$filtered"
    if [ -s "$filtered" ]; then
        git apply --whitespace=nowarn --3way "$filtered" 2>/dev/null || \
        git apply --whitespace=nowarn --reject "$filtered" 2>/dev/null || true
    fi
    rm -f "$filtered"
}}

apply_patch_tolerant /home/test.patch

# Create dummy frontend/dist dirs for go:embed patterns that reference them
grep -rn 'go:embed.*frontend/dist' v2/ 2>/dev/null | grep '\\.go:' | while IFS=: read -r f rest; do
  dir=$(dirname "$f")/frontend/dist
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done
cd v2
# Get buildable packages (exclude windows-only packages that can't build on linux)
PKGS=$(go list ./... 2>/dev/null | grep -v '/frontend/desktop/windows' | grep -v 'go-webview2' | grep -v 'go-common-file-dialog')
go test -v -count=1 $PKGS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}

# Apply patches tolerantly: filter out binary hunks, use --3way for context mismatches
apply_patch_tolerant() {{
    local patch="$1"
    if [ ! -s "$patch" ]; then
        return 0
    fi
    # First try normal apply
    if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
        return 0
    fi
    # Filter out binary patch hunks and retry
    local filtered=$(mktemp)
    awk '
        /^diff --git/ {{ in_binary=0; buf=$0"\\n"; next }}
        /^(GIT binary patch|Binary files)/ {{ in_binary=1; buf=""; next }}
        in_binary {{ next }}
        buf != "" {{ printf "%s", buf; buf="" }}
        {{ print }}
    ' "$patch" > "$filtered"
    if [ -s "$filtered" ]; then
        git apply --whitespace=nowarn --3way "$filtered" 2>/dev/null || \
        git apply --whitespace=nowarn --reject "$filtered" 2>/dev/null || true
    fi
    rm -f "$filtered"
}}

apply_patch_tolerant /home/test.patch
apply_patch_tolerant /home/fix.patch

# Create dummy frontend/dist dirs for go:embed patterns that reference them
grep -rn 'go:embed.*frontend/dist' v2/ 2>/dev/null | grep '\\.go:' | while IFS=: read -r f rest; do
  dir=$(dirname "$f")/frontend/dist
  mkdir -p "$dir"
  touch "$dir/.gitkeep"
done
cd v2
# Get buildable packages (exclude windows-only packages that can't build on linux)
PKGS=$(go list ./... 2>/dev/null | grep -v '/frontend/desktop/windows' | grep -v 'go-webview2' | grep -v 'go-common-file-dialog')
go test -v -count=1 $PKGS

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


@Instance.register("wailsapp", "wails")
class Wails(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WailsImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        run_tests = set(re.findall(r"=== RUN\s+([\S]+)", log))
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", log))
        skipped_tests = set(re.findall(r"--- SKIP: ([\S]+)", log))
        failed_tests = run_tests - passed_tests - skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
