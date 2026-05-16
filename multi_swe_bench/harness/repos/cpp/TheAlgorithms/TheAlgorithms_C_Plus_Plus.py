import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RUN_TESTS_SH = r"""
# Source this from each run/test/fix script.
# Provides: run_patch_tests <patch1> [patch2 ...]
# TheAlgorithms/C-Plus-Plus has no test framework -- every .cpp is a standalone
# program whose main() self-checks via <cassert>. Each modified .cpp becomes a
# test named by its repo-relative path. Emits "PASS: <path>" / "FAIL: <path>".

set +e

compile_and_run() {
    local f="$1"
    local dir fb out std
    dir=$(dirname "$f")
    fb=$(basename "$f")
    out=$(mktemp -u /tmp/ta_cpp_XXXXXX)

    # Phase 1: standalone compile. If the file links on its own it has a main()
    # and all symbols resolved -> the run result is DEFINITIVE, decide now.
    # (Falling through to sibling-linking here is what caused an O(n^2)
    # bulk-recompile of every main()-bearing file in the directory.)
    for std in gnu++17 gnu++11; do
        if g++ -O0 -w -std=$std -I "$dir" -I . -o "$out" "$f" -lm -fopenmp 2>/dev/null; then
            if timeout 15s "$out" </dev/null >/dev/null 2>&1; then
                rm -f "$out"
                return 0
            fi
            rm -f "$out"
            return 1
        fi
    done

    # Phase 2: standalone failed for every standard -> the file likely depends
    # on a sibling translation unit (e.g. test_stack.cpp + stack.cpp). Link in
    # ONLY same-directory helpers that do NOT define their own main(), so we
    # never hit "multiple definition of main" and never bulk-compile programs.
    local link_sources=("$f") s
    while IFS= read -r -d $'\0' s; do
        [ "$(basename "$s")" = "$fb" ] && continue
        if grep -Eq '(int|void)[[:space:]]+main[[:space:]]*\(' "$s" 2>/dev/null; then
            continue
        fi
        link_sources+=("$s")
    done < <(find "$dir" -maxdepth 1 \( -name "*.cpp" -o -name "*.cc" -o -name "*.cxx" \) -print0 2>/dev/null)

    if [ "${#link_sources[@]}" -gt 1 ]; then
        for std in gnu++17 gnu++11; do
            if g++ -O0 -w -std=$std -I "$dir" -I . -o "$out" "${link_sources[@]}" -lm -fopenmp 2>/dev/null; then
                if timeout 15s "$out" </dev/null >/dev/null 2>&1; then
                    rm -f "$out"
                    return 0
                fi
                rm -f "$out"
                return 1
            fi
        done
    fi

    rm -f "$out"
    return 1
}

list_patch_files() {
    python3 - "$@" <<'PY'
import re, os, sys
files = set()
for path in sys.argv[1:]:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        continue
    with open(path, 'r', errors='replace') as fh:
        for line in fh:
            m = re.match(r'^diff --git a/(.+?) b/(.+?)\s*$', line)
            if m:
                files.add(m.group(2))
for f in sorted(files):
    if f.endswith(('.cpp', '.cc', '.cxx')):
        print(f)
PY
}

run_patch_tests() {
    # Build the canonical test-name set from BOTH patches so all 3 stages
    # report on the same names (a file missing at this stage is simply omitted
    # -> TestStatus.NONE in the harness).
    local file_list
    file_list=$(list_patch_files "$@")
    if [ -z "$file_list" ]; then
        return 0
    fi
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if [ ! -f "$f" ]; then
            continue
        fi
        if compile_and_run "$f"; then
            echo "PASS: $f"
        else
            echo "FAIL: $f"
        fi
    done <<EOF
$file_list
EOF
}
"""


class TheAlgorithmsCppImageBase(Image):
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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        build-essential g++ cmake libomp-dev \\
        git ca-certificates python3 coreutils \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class TheAlgorithmsCppImageDefault(Image):
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
        return TheAlgorithmsCppImageBase(self.pr, self._config)

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
                "testlib.sh",
                _RUN_TESTS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/testlib.sh

# Baseline: no patches applied
run_patch_tests /home/test.patch /home/fix.patch
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/testlib.sh

if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
run_patch_tests /home/test.patch /home/fix.patch
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/testlib.sh

if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
run_patch_tests /home/test.patch /home/fix.patch
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


@Instance.register("TheAlgorithms", "C-Plus-Plus")
class TheAlgorithmsCPlusPlus(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsCppImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^PASS:\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAIL:\s+(.+?)\s*$")
        re_skip = re.compile(r"^SKIP:\s+(.+?)\s*$")

        for line in test_log.splitlines():
            line = line.rstrip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

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
