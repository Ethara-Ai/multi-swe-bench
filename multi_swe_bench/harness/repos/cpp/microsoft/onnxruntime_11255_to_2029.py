import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs (e.g., for .onnx model files) cause 'cannot apply binary patch
    without full index line' errors with git apply. These are typically
    model assets not needed for compilation or testing.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            # Collect entire diff section and check for binary content
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            # Only include non-binary diff sections
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


class EraAImageBase(Image):
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
        return "gcc:12"

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

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED base (tag "base", ONE image reused by every PR in this era).
        # Light hardening only — no gc/prune — so each PR's prepare.sh can still
        # `git checkout <base.sha>`; the strict per-PR pass lives in
        # EraAImageDefault. The `# syntax` directive makes
        # DockerfileEnhancer.enhance() skip this file, which is what stops it
        # auto-injecting a ${BASE_COMMIT} checkout + prune (which would pin this
        # shared base to one PR) and the proxy/CA-cert scaffolding.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

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
    python3 \\
    python3-pip \\
    python3-dev \\
    ninja-build \\
    pkg-config \\
    libssl-dev \\
    git \\
    ca-certificates \\
    curl \\
    wget \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages 'cmake<4'

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class EraAImageDefault(Image):
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
        return EraAImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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

# Initialize submodules (eigen, protobuf, etc. are git submodules in this era)
git submodule update --init --recursive

# Remove -Werror from cmake/CMakeLists.txt to prevent external dep warnings
# from failing the build under GCC 12
sed -i '/-Werror/d' cmake/CMakeLists.txt || true

# Remove platform.linux_distribution() calls that fail on Python 3.12+
# (only affects PRs 2029-3043 but harmless for others)
sed -i '/linux_distribution/d' tools/ci_build/build.py || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Detect which optional build.py flags are supported by this version
BUILD_HELP=$(python3 tools/ci_build/build.py --help 2>&1 || true)
OPTIONAL_FLAGS=""
for flag in --allow_running_as_root --skip_tests --compile_no_warning_as_error; do
    if echo "$BUILD_HELP" | grep -q -- "$flag"; then
        OPTIONAL_FLAGS="$OPTIONAL_FLAGS $flag"
    fi
done

python3 tools/ci_build/build.py \\
    --build_dir build \\
    --config Release \\
    --update \\
    --build \\
    --parallel \\
    --build_shared_lib \\
    $OPTIONAL_FLAGS
cd build/Release
ctest --output-on-failure || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

# Detect which optional build.py flags are supported by this version
BUILD_HELP=$(python3 tools/ci_build/build.py --help 2>&1 || true)
OPTIONAL_FLAGS=""
for flag in --allow_running_as_root --skip_tests --compile_no_warning_as_error; do
    if echo "$BUILD_HELP" | grep -q -- "$flag"; then
        OPTIONAL_FLAGS="$OPTIONAL_FLAGS $flag"
    fi
done

python3 tools/ci_build/build.py \\
    --build_dir build \\
    --config Release \\
    --update \\
    --build \\
    --parallel \\
    --build_shared_lib \\
    $OPTIONAL_FLAGS 2>&1 || true
cd build/Release
ctest --output-on-failure 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

# Detect which optional build.py flags are supported by this version
BUILD_HELP=$(python3 tools/ci_build/build.py --help 2>&1 || true)
OPTIONAL_FLAGS=""
for flag in --allow_running_as_root --skip_tests --compile_no_warning_as_error; do
    if echo "$BUILD_HELP" | grep -q -- "$flag"; then
        OPTIONAL_FLAGS="$OPTIONAL_FLAGS $flag"
    fi
done

python3 tools/ci_build/build.py \\
    --build_dir build \\
    --config Release \\
    --update \\
    --build \\
    --parallel \\
    --build_shared_lib \\
    $OPTIONAL_FLAGS 2>&1 || true
cd build/Release
ctest --output-on-failure 2>&1 || true

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

        # Per-PR anti-reward-hacking hardening (PIPELINE.md §4). dependency()
        # returns an Image, so DockerfileEnhancer emits this Dockerfile verbatim
        # (it only auto-injects into str-dependency images) — so the canonical
        # block from image.py is embedded here, after prepare.sh has checked out
        # base.sha and initialised submodules, with ${BASE_COMMIT} bound to the
        # literal sha. Referenced from image.py rather than copied so it cannot
        # drift from the source of truth.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("microsoft", "onnxruntime_11255_to_2029")
class Onnxruntime_11255_to_2029(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EraAImageDefault(self.pr, self._config)

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

        re_pass_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+.*\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
