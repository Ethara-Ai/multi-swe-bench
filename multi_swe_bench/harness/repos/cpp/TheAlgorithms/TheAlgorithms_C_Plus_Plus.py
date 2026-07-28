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

    # Many of these files are interactive demo main()s (cin >> size, cin >>
    # array[i], ...). Feeding /dev/null makes every cin extraction fail
    # immediately -- on a plain "int size; cin>>size;" that's usually a
    # harmless 0, but "cin>>size; int array[size];" (a stack VLA) turns a
    # failed read into an indeterminate/garbage size and segfaults. A bounded
    # stream of a harmless small number gives every extraction a valid value
    # instead of an undefined one; bounded (not infinite) so a program that
    # loops on a failed read still hits real EOF instead of spinning for the
    # whole 15s timeout.
    for std in gnu++17 gnu++11; do
        if g++ -O0 -w -std=$std -I "$dir" -I . -o "$out" "$f" -lm -fopenmp 2>/dev/null; then
            if timeout 15s "$out" < <(yes 1 2>/dev/null | head -n 500) >/dev/null 2>&1; then
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
                if timeout 15s "$out" < <(yes 1 2>/dev/null | head -n 500) >/dev/null 2>&1; then
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
        """PIPELINE.md §3 reference-format base (SINGLE, shared across the era).

        The leading ``# syntax=docker/dockerfile:1.6`` is the documented §2
        opt-out: DockerfileEnhancer.enhance() returns this Dockerfile verbatim
        once it sees that directive. That is required here -- this image has a
        *string* dependency AND clones the repo, so without the opt-out the
        enhancer would rewrite the clone into ``checkout ${BASE_COMMIT}`` plus
        the strict hardening block, force-pinning this SHARED base to one PR's
        commit and destroying the history every other bundle needs.

        Hardening here is deliberately LIGHT so the base keeps FULL history;
        each PR layer checks out its own base.sha and then applies the strict
        canonical hardening (§4).
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential g++ cmake libomp-dev \\
    git ca-certificates python3 coreutils \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
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
        """PIPELINE.md §4 PR-image format.

        The shared base already cloned FULL history, so this layer does NOT
        clone -- prepare.sh resets and checks out this PR's base.sha out of
        that history. dependency() is an Image, so DockerfileEnhancer.enhance()
        returns this Dockerfile verbatim, which means the anti-reward-hacking
        hardening is NOT auto-injected and must be spelled out here (it was
        missing entirely before, leaving the fix commit reachable via
        `git log`/`git checkout` on the still-present branch refs).
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

"""
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        # PIPELINE.md §4: hardening must use the LITERAL base.sha, not a
        # variable -- an ARG could be overridden at build time
        # (--build-arg BASE_COMMIT=...) and re-pin the image elsewhere.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        )
        return header + hardening + tail


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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md §11b: the JSONL and this registry ship together, and the
# trajectory harness routes via Instance.create() -> f"{org}/{number_interval}".
# Every dash-joined bundle value must be a registered key in addition to the
# era key above, else create() raises "not registered" before any build.
_BUNDLE_NIS_THEALGORITHMS_CPLUSPLUS = [
    "197-609-694-695-711-715-718-720-721-722-723-724-725-730-731-732-733",
    "1984-2020-2136-2224-2242-2400-2413-2416-2417-2429-2432",
    "2235-2410",
    "277-517-561-585-589-604-606-607-622-625-634-638-639-641-642-643-644-645-646-647-648-649-650-651-652-653-654-655-656-657-671-673-674-676-677-678-679-680-681-683-684-687-688-691",
    "280-281-286-288-289",
    "287-696-698-700-701-704",
    "51-58-88",
    "860-861",
    "31-32-44-45-48-57-59-65-70-73-74-75-77-79-80-81-82-83-85-86",
    "803-805-816-835-845-855-856-864-871-872-873-874-878-879-881-882-884-885-886-888-889-890-893-894-899-901-902-904-905-906-907-912-913-914-917-927-929-930-933-936-941-942-943-945-947-948-949-950-952-953-956-957-960-961",
    "916-954-958-962-964-969-970-972-973-975-976-977-978-979-980-986-990-991-992-993-994-997-998-1000-1016-1018-1021-1023-1024-1025-1027-1030-1033-1034-1035-1036",
    "94-97-101-102-103-104-105-114-117-121-123-140-142-143-145-146-148-155-156-166-167-169-171-176-180",
]
for _ni in _BUNDLE_NIS_THEALGORITHMS_CPLUSPLUS:
    Instance.register("TheAlgorithms", _ni)(TheAlgorithmsCPlusPlus)
