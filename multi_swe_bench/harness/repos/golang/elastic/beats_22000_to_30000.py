import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BeatsEarlyImageBase(Image):
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
        return "golang:1.17-bullseye"

    def image_tag(self) -> str:
        return "beats-22000-30000-base"

    def workdir(self) -> str:
        return "beats-22000-30000-base"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make sudo wget unzip \\
    python3 python3-dev python3-venv python3-pip \\
    libpcap-dev librpm-dev libaio-dev libssl-dev libffi-dev \\
    iproute2 netcat-openbsd \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class BeatsEarlyImageDefault(Image):
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
        return BeatsEarlyImageBase(self.pr, self.config)

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
                "filter_binary_diffs.sh",
                """#!/bin/bash
# Reads a patch from $1 (or stdin if "-"), drops every "diff --git" block that
# contains an abbreviated binary marker ("Binary files X and Y differ" or
# "GIT binary patch"). Emits the remainder on stdout. ~19% of beats PRs in
# this dataset include binary diffs (.png, .zip, .log) without delta data,
# which would otherwise make `git apply` fail outright.
exec awk '
function emit() {
  if (block != "" && !skip_block) printf "%s", block
  block = ""
  skip_block = 0
}
/^diff --git / { emit() }
/^Binary files .* and .* differ$/ { skip_block = 1 }
/^GIT binary patch$/ { skip_block = 1 }
{ block = block $0 "\\n" }
END { emit() }
' "${1:-/dev/stdin}"
""",
            ),
            File(
                ".",
                "compute_scope.sh",
                """#!/bin/bash
# Computes SCOPE = list of changed Go package directories from /home/test.patch
# and /home/fix.patch. Sourced by run.sh / test-run.sh / fix-run.sh so the
# monorepo doesn't try to compile every sub-beat on every PR.
PATCH_LIST=""
[ -f /home/test.patch ] && PATCH_LIST="$PATCH_LIST /home/test.patch"
[ -f /home/fix.patch ] && PATCH_LIST="$PATCH_LIST /home/fix.patch"

SCOPE=""
if [ -n "$PATCH_LIST" ]; then
  SCOPE=$(cat $PATCH_LIST 2>/dev/null \\
    | grep -E "^diff --git" \\
    | awk '{print $3}' \\
    | sed 's|^a/||' \\
    | grep '\\.go$' \\
    | xargs -I{} dirname {} 2>/dev/null \\
    | sort -u \\
    | sed 's|^|./|' \\
    | tr '\\n' ' ')
fi
export SCOPE
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch Go modules so test runs don't redo network work each invocation.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test in baseline."
  exit 0
fi
echo "Baseline test scope: $SCOPE"
# Run each package independently so one unbuildable package (e.g. Linux-only
# build tags on ARM64) doesn't abort the full sweep before producing markers.
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/filter_binary_diffs.sh /home/test.patch > /tmp/test.patch.filtered
git apply --whitespace=nowarn /tmp/test.patch.filtered
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test."
  exit 0
fi
echo "test-patch test scope: $SCOPE"
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/filter_binary_diffs.sh /home/test.patch > /tmp/test.patch.filtered
bash /home/filter_binary_diffs.sh /home/fix.patch  > /tmp/fix.patch.filtered
git apply --whitespace=nowarn /tmp/test.patch.filtered /tmp/fix.patch.filtered
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test."
  exit 0
fi
echo "fix-patch test scope: $SCOPE"
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

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


@Instance.register("elastic", "beats_22000_to_30000")
class BEATS_22000_TO_30000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BeatsEarlyImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
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
