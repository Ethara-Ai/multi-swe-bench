import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SanguineImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "golang:1.22-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        if self.config.need_clone:
            fetch_block = f"""RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
{self.clear_env}
CMD ["/bin/bash"]"""
        else:
            fetch_block = f"{self.clear_env}\nCOPY {repo} /home/{repo}"

        return f"""FROM {self.dependency()}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libc6-dev pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN go env -w CGO_ENABLED=1

{fetch_block}
"""


class SanguineImageDefault(Image):
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
        return SanguineImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_dirs(self) -> list[str]:
        dirs: set[str] = set()
        for m in re.finditer(r"diff --git a/(.+?) b/(\S+)", self.pr.test_patch or ""):
            path = m.group(2)
            if not path.endswith("_test.go"):
                continue
            dirs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
        return sorted(dirs)

    def _go_test_cmd(self) -> str:
        test_dirs = " ".join(self._test_dirs()) or "."
        return f"""TEST_DIRS="{test_dirs}"

export GOWORK=off
export GOFLAGS=-mod=mod
export CI=true

for dir in $TEST_DIRS; do
  mod="$dir"
  while [ "$mod" != "." ] && [ "$mod" != "/" ] && [ ! -f "$mod/go.mod" ]; do
    mod=$(dirname "$mod")
  done
  if [ ! -f "$mod/go.mod" ]; then
    echo "no go.mod found for $dir, skipping"
    continue
  fi
  rel="."
  if [ "$mod" != "$dir" ]; then
    rel="./${{dir#$mod/}}"
  fi
  echo "=== go test $mod $rel ==="
  LIST=$(cd "$mod" && go test -list '.*' "$rel" 2>/dev/null \\
           | grep -E '^Test') || LIST=""
  if [ -n "$LIST" ]; then
    for t in $LIST; do
      rc=0
      (cd "$mod" && go test -v -count=1 -timeout 900s -run "^${{t}}\\$" "$rel") 2>&1 || rc=$?
      [ "$rc" -eq 0 ] || echo "go test $t exited with status $rc"
    done
  else
    rc=0
    (cd "$mod" && go test -v -count=1 -timeout 900s "$rel") 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || echo "go test exited with status $rc"
    if [ -f /home/test.patch ]; then
      ADDED=$(grep -oE '^\\+func (Test[A-Za-z0-9_]+)' /home/test.patch \\
                | awk '{{print $2}}' | sort -u) || ADDED=""
      for t in $ADDED; do echo "BUILD_FAILED_TEST: $t"; done
    fi
  fi
done"""

    def files(self) -> list[File]:
        go_test = self._go_test_cmd()

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

export GOWORK=off
export GOFLAGS=-mod=mod

TEST_DIRS="{test_dirs}"
for dir in $TEST_DIRS; do
  mod="$dir"
  while [ "$mod" != "." ] && [ "$mod" != "/" ] && [ ! -f "$mod/go.mod" ]; do
    mod=$(dirname "$mod")
  done
  [ -f "$mod/go.mod" ] || continue
  rel="."
  if [ "$mod" != "$dir" ]; then
    rel="./${{dir#$mod/}}"
  fi
  (cd "$mod" && go mod download) || true
  (cd "$mod" && go test -run '^$' -count=1 "$rel") || true
done

""".format(pr=self.pr, test_dirs=" ".join(self._test_dirs()) or "."),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{go_test}
exit 0
""".format(pr=self.pr, go_test=go_test),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{go_test}
exit 0
""".format(pr=self.pr, go_test=go_test),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{go_test}
exit 0
""".format(pr=self.pr, go_test=go_test),
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


@Instance.register("synapsecns", "sanguine")
class Sanguine(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SanguineImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_pattern = re.compile(r"--- PASS: (\S+)")
        failed_pattern = re.compile(r"--- FAIL: (\S+)")
        skipped_pattern = re.compile(r"--- SKIP: (\S+)")
        build_failed_test_pattern = re.compile(r"^BUILD_FAILED_TEST:\s+(\S+)$")
        build_failed_pkg_pattern = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")
        build_failed_pkgs: set[str] = set()
        named_build_failures = False

        for line in log.splitlines():
            line = line.strip()

            m = build_failed_test_pattern.match(line)
            if m:
                named_build_failures = True
                passed_tests.discard(m.group(1))
                failed_tests.add(m.group(1))
                continue

            m = build_failed_pkg_pattern.match(line)
            if m:
                build_failed_pkgs.add(m.group(1))
                continue

            m = passed_pattern.search(line)
            if m:
                if m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))
                continue

            m = failed_pattern.search(line)
            if m:
                passed_tests.discard(m.group(1))
                failed_tests.add(m.group(1))
                continue

            m = skipped_pattern.search(line)
            if m:
                if m.group(1) not in passed_tests and m.group(1) not in failed_tests:
                    skipped_tests.add(m.group(1))

        if not named_build_failures:
            failed_tests |= build_failed_pkgs

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
