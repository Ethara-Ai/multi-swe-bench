import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared shell snippets.
#
# gVisor is built and tested with Bazel.  The Bazel version a given commit
# expects is discovered dynamically at run time:
#   * WORKSPACE era  -> the version is embedded in images/default/Dockerfile
#   * bzlmod era     -> the version lives in images/default/bazelversion
# bazelisk (installed as `bazel`) downloads whatever version is requested
# through USE_BAZEL_VERSION, so a single config covers the whole PR range.
#
# Tests are scoped to the Bazel packages touched by the test/fix patches.
# Running the full `//pkg/... //tools/...` tree per PR is not tractable, and
# the patch-affected packages are exactly the ones relevant for fixed-test
# detection.  Results are reported at Bazel target granularity.
# ---------------------------------------------------------------------------

COMMON_SH = """#!/bin/bash
# Sourced by run.sh / test-run.sh / fix-run.sh / prepare.sh.
cd /home/gvisor

# Discover the Bazel version this commit expects.
BZL=""
if [ -f images/default/bazelversion ]; then
    BZL="$(tr -d '[:space:]' < images/default/bazelversion)"
fi
if [ -z "$BZL" ] && [ -f images/default/Dockerfile ]; then
    BZL="$(grep -oE 'bazel-[0-9.]+-linux' images/default/Dockerfile | head -1 \\
        | sed 's/bazel-//; s/-linux//')"
fi
if [ -z "$BZL" ] && [ -f .bazelversion ]; then
    BZL="$(tr -d '[:space:]' < .bazelversion)"
fi
[ -z "$BZL" ] && BZL="7.5.0"
export USE_BAZEL_VERSION="$BZL"

BAZEL_FLAGS="--test_tag_filters=-nogo,-requires-kvm \\
--build_tag_filters=-network_plugins --test_output=errors --keep_going"

# Echo the Bazel test targets for the packages touched by the patches.
compute_targets() {
    local files pkgs
    files="$(cat /home/test.patch /home/fix.patch 2>/dev/null \\
        | grep -oE '^\\+\\+\\+ b/[^[:space:]]+' | sed 's#^[+][+][+] b/##')"
    pkgs="$(for f in $files; do
        d="$(dirname "$f")"
        case "$d" in
            pkg/*|tools/*|runsc/*|vdso/*|test/*) echo "//$d:all" ;;
        esac
    done | sort -u)"
    [ -z "$pkgs" ] && return 0
    bazel query "tests(set($pkgs))" --keep_going 2>/dev/null \\
        | grep '^//' | grep -vE '_nogo$' | sort -u
}

# Apply patches, tolerating non-code files.  git apply is all-or-nothing, so
# binary assets / docs that don't apply cleanly would otherwise abort the whole
# patch; excluding them is harmless since they never affect test execution.
apply_patches() {
    git apply --whitespace=nowarn \\
        --exclude='g3doc/*' --exclude='website/*' \\
        --exclude='*.png' --exclude='*.svg' --exclude='*.gif' \\
        --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.webp' \\
        --exclude='*.pdf' --exclude='*.ico' --exclude='*.mp4' \\
        "$@"
}
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

RUN_SH = """#!/bin/bash
source /home/common.sh

echo "gvisor: using Bazel ${USE_BAZEL_VERSION}"
TARGETS=$(compute_targets)
if [ -z "$TARGETS" ]; then
    echo "gvisor: no affected Bazel test targets"
    exit 0
fi
echo "gvisor: test targets:"
echo "$TARGETS"
bazel test $TARGETS $BAZEL_FLAGS || true
"""

TEST_RUN_SH = """#!/bin/bash
source /home/common.sh
apply_patches /home/test.patch

echo "gvisor: using Bazel ${USE_BAZEL_VERSION}"
TARGETS=$(compute_targets)
if [ -z "$TARGETS" ]; then
    echo "gvisor: no affected Bazel test targets"
    exit 0
fi
echo "gvisor: test targets:"
echo "$TARGETS"
bazel test $TARGETS $BAZEL_FLAGS || true
"""

FIX_RUN_SH = """#!/bin/bash
source /home/common.sh
apply_patches /home/test.patch /home/fix.patch

echo "gvisor: using Bazel ${USE_BAZEL_VERSION}"
TARGETS=$(compute_targets)
if [ -z "$TARGETS" ]; then
    echo "gvisor: no affected Bazel test targets"
    exit 0
fi
echo "gvisor: test targets:"
echo "$TARGETS"
bazel test $TARGETS $BAZEL_FLAGS || true
"""


class GvisorImageBase(Image):
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
        return "golang:1.25-bookworm"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        curl unzip zip g++ python3 make ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

# bazelisk is used as the `bazel` entrypoint; it downloads the exact Bazel
# version each gVisor commit pins (USE_BAZEL_VERSION).
RUN ARCH="$(uname -m | sed s/aarch64/arm64/ | sed s/x86_64/amd64/)" \\
    && curl -fsSL -o /usr/local/bin/bazel \\
        "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${{ARCH}}" \\
    && chmod +x /usr/local/bin/bazel

{code}

{self.clear_env}

"""


class GvisorImageDefault(Image):
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
        return GvisorImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n\n"
            "cd /home/gvisor\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n\n"
            "source /home/common.sh\n\n"
            "# Warm the Bazel build/test cache so run/test/fix are fast.\n"
            "# Only on a native build: under QEMU emulation (multi-arch build of\n"
            "# the non-host arch) a full gVisor Bazel build hangs/crashes, so the\n"
            "# emulated image variant skips warm-up and builds its cache at eval\n"
            "# time instead (the run scripts always execute on native hardware).\n"
            'if [ "$(uname -m)" = "aarch64" ]; then\n'
            '    echo "gvisor: warming Bazel ${USE_BAZEL_VERSION} cache (native)"\n'
            "    bazel version || true\n"
            "    TARGETS=$(compute_targets)\n"
            '    if [ -n "$TARGETS" ]; then\n'
            "        bazel test $TARGETS $BAZEL_FLAGS || true\n"
            "    fi\n"
            "else\n"
            '    echo "gvisor: emulated arch ($(uname -m)) - skipping Bazel warm-up"\n'
            "fi\n"
        )

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
                CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "common.sh",
                COMMON_SH,
            ),
            File(
                ".",
                "prepare.sh",
                prepare_sh,
            ),
            File(
                ".",
                "run.sh",
                RUN_SH,
            ),
            File(
                ".",
                "test-run.sh",
                TEST_RUN_SH,
            ),
            File(
                ".",
                "fix-run.sh",
                FIX_RUN_SH,
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


@Instance.register("google", "gvisor")
class Gvisor(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GvisorImageDefault(self.pr, self._config)

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

        # Strip ANSI escape sequences.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Bazel target-level result lines, e.g.
        #   //pkg/sync:sync_test                  (cached) PASSED in 6.4s
        #   //pkg/metric:metric_test                       FAILED in 5.4s
        #   //pkg/sentry/kernel:kernel_test                FAILED TO BUILD
        #   //pkg/foo:bar_test                             TIMEOUT in 300.0s
        #   //pkg/foo:bar_test            FLAKY, failed in 1 out of 2 in 3.0s
        #   //pkg/foo:bar_test                             NO STATUS
        re_pass = re.compile(r"^(//\S+)\s+(?:\(cached\)\s+)?PASSED\s+in\s")
        re_flaky = re.compile(r"^(//\S+)\s+(?:\(cached\)\s+)?FLAKY\b")
        re_fail = re.compile(
            r"^(//\S+)\s+(?:\(cached\)\s+)?"
            r"(?:FAILED\s+in\s|FAILED TO BUILD|TIMEOUT\s+in\s)"
        )
        re_skip = re.compile(r"^(//\S+)\s+NO STATUS\b")

        for line in clean_log.splitlines():
            line = line.strip()
            if not line.startswith("//"):
                continue

            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_flaky.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))
                continue

        # A target should not appear in more than one bucket.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
