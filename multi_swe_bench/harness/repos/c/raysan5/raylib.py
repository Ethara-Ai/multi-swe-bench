import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# raylib is a C graphics library with no assertion-based unit test suite.
# Its `examples/**/*.c` are interactive GUI demos. The viable "test" the
# harness can measure is COMPILATION: whether each example .c file the PR
# touches compiles and links against the (fix-patched) raylib library.
# A PR resolves when an example fails to compile in the test stage but
# compiles in the fix stage (i.e. the library fix supplied a missing or
# corrected API the example depends on).


def _extract_example_sources(pr: PullRequest) -> list[str]:
    """Collect example .c paths touched by the test/fix patches."""
    sources: set[str] = set()
    for patch in (pr.test_patch or "", pr.fix_patch or ""):
        for line in patch.split("\n"):
            if not line.startswith("diff --git "):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            path = parts[2]
            if path.startswith("a/"):
                path = path[2:]
            if path.startswith("examples/") and path.endswith(".c"):
                sources.add(path)
    return sorted(sources)


class RaylibImageBase(Image):
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

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential git ca-certificates make \\
    libasound2-dev libx11-dev libxrandr-dev libxi-dev \\
    libgl1-mesa-dev libglu1-mesa-dev libxcursor-dev \\
    libxinerama-dev libwayland-dev libxkbcommon-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class RaylibImageDefault(Image):
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
        return RaylibImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        example_sources = _extract_example_sources(self.pr)
        candidates = "\n".join(example_sources)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "example_sources.txt",
                candidates + ("\n" if candidates else ""),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Pre-warm: build the raylib static library once from the base commit.
cd /home/{repo}/src
timeout --kill-after=30 900 make PLATFORM=PLATFORM_DESKTOP RAYLIB_LIBTYPE=STATIC || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Build the raylib library, then compile each candidate example.
# Emits `PASS: <example>` / `FAIL: <example>` lines for parse_log.
set -uo pipefail

REPO=/home/{repo}
cd "$REPO/src"

# Rebuild the library (fix.patch may modify src/*). Clean first so a
# stale base-commit libraylib.a never masks a compile regression.
make clean >/dev/null 2>&1 || true
if timeout --kill-after=30 900 make PLATFORM=PLATFORM_DESKTOP RAYLIB_LIBTYPE=STATIC > /tmp/raylib_build.log 2>&1; then
  echo "PASS: raylib-library-build"
else
  echo "FAIL: raylib-library-build"
  tail -20 /tmp/raylib_build.log
fi

LIBDIR="$REPO/src"
GCC_FLAGS="-I $REPO/src -L $LIBDIR -lraylib -lGL -lm -lpthread -ldl -lrt -lX11"

cd "$REPO"
while IFS= read -r ex; do
  [ -z "$ex" ] && continue
  [ -f "$ex" ] || {{ echo "FAIL: $ex"; continue; }}
  if timeout --kill-after=15 300 gcc "$ex" -o /tmp/ex_out $GCC_FLAGS > /tmp/ex_compile.log 2>&1; then
    echo "PASS: $ex"
  else
    echo "FAIL: $ex"
    tail -15 /tmp/ex_compile.log
  fi
  rm -f /tmp/ex_out
done < /home/example_sources.txt

# Always exit 0: per-example results are reported via PASS:/FAIL: lines,
# so the calling run script (set -e) must not abort on a compile failure.
exit 0
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
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


@Instance.register("raysan5", "raylib")
class Raylib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RaylibImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        re_pass = re.compile(r"^PASS:\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAIL:\s+(.+?)\s*$")

        for line in clean.splitlines():
            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())

        # Disjoint sets: passed > failed.
        failed_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
