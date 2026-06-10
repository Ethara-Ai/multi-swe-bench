from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CarbonLangImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it installs the default
        # + extra_packages, clones "${REPO_URL}", checks out "${BASE_COMMIT}",
        # runs extra_setup(), and appends the _HARDENING_BLOCK that strips every
        # other ref/commit so the fix can't be read out of git history.
        # DockerfileEnhancer then injects the proxy/cert infra and the final
        # sanitize pass. None of that fires when dockerfile() is overridden,
        # which is why the previous two-stage build bypassed it.
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # Appended to image.py's default_packages (ca-certificates, curl,
        # build-essential, git, gnupg, make, python3, sudo, wget). These are the
        # carbon-lang specific build deps + what apt.llvm.org's llvm.sh needs
        # (lsb-release, software-properties-common, gnupg).
        return [
            "lsb-release",
            "software-properties-common",
            "pkg-config",
            "zip",
            "unzip",
            "python3-pip",
            "python-is-python3",
            "openjdk-21-jdk-headless",
            "libtinfo5",
            "libxml2",
            "m4",
        ]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Installs the LLVM 19 toolchain + Bazelisk (as bazel), then
        # stages the runtime helper scripts + patches into /home/. The copied
        # files live outside /home/{repo}, so the hardening pass (which only
        # operates inside the git tree) leaves them untouched. Everything runs
        # as root; Bazel builds fine as root in this harness (see cpp/grpc).
        return (
            "# Install the LLVM 19 toolchain and symlink the unversioned names.\n"
            "RUN curl -fSL https://apt.llvm.org/llvm.sh -o /tmp/llvm.sh && \\\n"
            "    bash /tmp/llvm.sh 19 all && \\\n"
            "    apt-get install -y --no-install-recommends "
            "libc++-19-dev libc++abi-19-dev lld-19 lldb-19 && \\\n"
            "    ln -sf /usr/bin/clang-19 /usr/bin/clang && \\\n"
            "    ln -sf /usr/bin/clang++-19 /usr/bin/clang++ && \\\n"
            "    ln -sf /usr/bin/lld-19 /usr/bin/lld && \\\n"
            "    ln -sf /usr/bin/ld.lld-19 /usr/bin/ld.lld && \\\n"
            "    ln -sf /usr/bin/llvm-ar-19 /usr/bin/llvm-ar && \\\n"
            "    ln -sf /usr/bin/llvm-nm-19 /usr/bin/llvm-nm && \\\n"
            "    ln -sf /usr/bin/llvm-strip-19 /usr/bin/llvm-strip && \\\n"
            "    ln -sf /usr/bin/lldb-19 /usr/bin/lldb && \\\n"
            "    rm /tmp/llvm.sh && rm -rf /var/lib/apt/lists/*\n"
            "\n"
            "# Install Bazelisk as bazel (auto-selects the version from\n"
            "# .bazelversion / tools/bazel wrapper per checkout).\n"
            "RUN ARCH=$(dpkg --print-architecture) && \\\n"
            '    curl -fSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${ARCH}" \\\n'
            "      -o /tmp/bazel && \\\n"
            "    install -m 755 /tmp/bazel /usr/local/bin/bazel && \\\n"
            "    rm /tmp/bazel\n"
            "\n"
            "# Stage runtime helper scripts + patches.\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY bazel_utils.sh /home/bazel_utils.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh"
        )

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
                "bazel_utils.sh",
                "#!/bin/bash\n"
                "\n"
                "derive_test_targets() {\n"
                "    local files=\"$1\"\n"
                "    local cc_targets=\"\"\n"
                "    local carbon_tests=\"\"\n"
                "    local need_file_test=0\n"
                "    for f in $files; do\n"
                "        case \"$f\" in\n"
                "            toolchain/*/testdata/*.carbon|toolchain/*/testdata/**/*.carbon)\n"
                "                carbon_tests=\"${carbon_tests:+$carbon_tests,}$f\"\n"
                "                need_file_test=1\n"
                "                ;;\n"
                "            toolchain/*/testdata/*.tmpl|toolchain/*/testdata/**/*.tmpl)\n"
                "                need_file_test=1\n"
                "                ;;\n"
                "            testing/file_test/testdata/*.carbon|testing/file_test/testdata/**/*.carbon)\n"
                "                cc_targets=\"$cc_targets //testing/file_test:file_test_base_test\"\n"
                "                ;;\n"
                "            utils/tree_sitter/testdata/*.carbon|utils/tree_sitter/testdata/**/*.carbon \\\n"
                "                |utils/tree_sitter/*.cpp|utils/tree_sitter/*.h)\n"
                "                cc_targets=\"$cc_targets //utils/tree_sitter/...\"\n"
                "                ;;\n"
                "            common/*_test.cpp|common/*_test.h \\\n"
                "                |testing/*/*_test.cpp|testing/*/*_test.h \\\n"
                "                |toolchain/*/*_test.cpp|toolchain/*/*_test.h)\n"
                "                local dir base stem\n"
                "                dir=$(dirname \"$f\")\n"
                "                base=$(basename \"$f\")\n"
                "                stem=${base%.cpp}\n"
                "                stem=${stem%.h}\n"
                "                cc_targets=\"$cc_targets //$dir:$stem\"\n"
                "                ;;\n"
                "            bazel/*/*_test.py|github_tools/*_test.py \\\n"
                "                |proposals/scripts/*_test.py|toolchain/*/*_test.py)\n"
                "                local dir base\n"
                "                dir=$(dirname \"$f\")\n"
                "                base=$(basename \"$f\" .py)\n"
                "                cc_targets=\"$cc_targets //$dir:$base\"\n"
                "                ;;\n"
                "            common/BUILD|testing/*/BUILD|toolchain/*/BUILD|toolchain/BUILD)\n"
                "                local dir\n"
                "                dir=$(dirname \"$f\")\n"
                "                cc_targets=\"$cc_targets //$dir/...\"\n"
                "                ;;\n"
                "            testing/file_test/*.cpp|testing/file_test/*.h)\n"
                "                cc_targets=\"$cc_targets //testing/file_test/...\"\n"
                "                need_file_test=1\n"
                "                ;;\n"
                "            testing/base/*.cpp|testing/base/*.h)\n"
                "                cc_targets=\"$cc_targets //testing/base/...\"\n"
                "                ;;\n"
                "            toolchain/*.cpp|toolchain/*.h|toolchain/*/*.cpp|toolchain/*/*.h \\\n"
                "                |toolchain/*/*/*.cpp|toolchain/*/*/*.h)\n"
                "                need_file_test=1\n"
                "                ;;\n"
                "            *.md|*.yaml|*.yml|*.json|*.toml|*.txt|.bazelrc|.bazelversion|*/SKILL.md)\n"
                "                ;;\n"
                "        esac\n"
                "    done\n"
                "    if [ -n \"$cc_targets\" ]; then\n"
                "        cc_targets=$(printf '%s\\n' $cc_targets | sort -u | tr '\\n' ' ')\n"
                "        cc_targets=${cc_targets% }\n"
                "    fi\n"
                "    DERIVED_CC_TARGETS=\"$cc_targets\"\n"
                "    DERIVED_CARBON_TESTS=\"$carbon_tests\"\n"
                "    DERIVED_NEED_FILE_TEST=\"$need_file_test\"\n"
                "}\n"
                "\n"
                "run_carbon_tests() {\n"
                "    local repo_dir=\"$1\"\n"
                "    cd \"$repo_dir\"\n"
                "    local exit_code=0\n"
                "\n"
                "    if [ \"$DERIVED_NEED_FILE_TEST\" = 1 ]; then\n"
                "        local extra_args=\"\"\n"
                "        if [ -n \"$DERIVED_CARBON_TESTS\" ]; then\n"
                "            extra_args=\"--test_arg=--file_tests=$DERIVED_CARBON_TESTS\"\n"
                "        fi\n"
                "        echo \"=== Running //toolchain/testing:file_test ${extra_args:+with $extra_args} ===\"\n"
                "        bazel test //toolchain/testing:file_test \\\n"
                "            --test_output=all \\\n"
                "            --test_summary=detailed \\\n"
                "            --keep_going \\\n"
                "            --test_arg=--gtest_brief=0 \\\n"
                "            $extra_args || exit_code=$?\n"
                "    fi\n"
                "\n"
                "    if [ -n \"$DERIVED_CC_TARGETS\" ]; then\n"
                "        echo \"=== Running test targets: $DERIVED_CC_TARGETS ===\"\n"
                "        bazel test $DERIVED_CC_TARGETS \\\n"
                "            --test_output=all \\\n"
                "            --test_summary=detailed \\\n"
                "            --keep_going || exit_code=$?\n"
                "    fi\n"
                "\n"
                "    return $exit_code\n"
                "}\n",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
source /home/bazel_utils.sh

cd /home/{pr.repo}

PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
derive_test_targets "$PATCH_FILES"

if [ "$DERIVED_NEED_FILE_TEST" = 0 ] && [ -z "$DERIVED_CC_TARGETS" ]; then
    echo "No test targets detected from test patch"
    echo "Patch files: $PATCH_FILES"
    exit 0
fi

echo "Patch files     : $PATCH_FILES"
echo "Carbon testdata : $DERIVED_CARBON_TESTS"
echo "cc_test targets : $DERIVED_CC_TARGETS"

run_carbon_tests /home/{pr.repo} || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
source /home/bazel_utils.sh

strip_binary_diffs() {{
    python3 -c "
import sys, re
content = open(sys.argv[1]).read()
parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
for part in parts:
    if part and 'Binary files' not in part:
        sys.stdout.write(part)
" "$1"
}}

cd /home/{pr.repo}
strip_binary_diffs /home/test.patch > /tmp/test_text.patch
git apply --whitespace=nowarn /tmp/test_text.patch

PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
derive_test_targets "$PATCH_FILES"

if [ "$DERIVED_NEED_FILE_TEST" = 0 ] && [ -z "$DERIVED_CC_TARGETS" ]; then
    echo "No test targets detected from test patch"
    echo "Patch files: $PATCH_FILES"
    exit 0
fi

echo "Patch files     : $PATCH_FILES"
echo "Carbon testdata : $DERIVED_CARBON_TESTS"
echo "cc_test targets : $DERIVED_CC_TARGETS"

run_carbon_tests /home/{pr.repo} || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
source /home/bazel_utils.sh

strip_binary_diffs() {{
    python3 -c "
import sys, re
content = open(sys.argv[1]).read()
parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
for part in parts:
    if part and 'Binary files' not in part:
        sys.stdout.write(part)
" "$1"
}}

cd /home/{pr.repo}
strip_binary_diffs /home/test.patch > /tmp/test_text.patch
strip_binary_diffs /home/fix.patch > /tmp/fix_text.patch
git apply --whitespace=nowarn /tmp/test_text.patch /tmp/fix_text.patch

PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
derive_test_targets "$PATCH_FILES"

if [ "$DERIVED_NEED_FILE_TEST" = 0 ] && [ -z "$DERIVED_CC_TARGETS" ]; then
    echo "No test targets detected from test patch"
    echo "Patch files: $PATCH_FILES"
    exit 0
fi

echo "Patch files     : $PATCH_FILES"
echo "Carbon testdata : $DERIVED_CARBON_TESTS"
echo "cc_test targets : $DERIVED_CC_TARGETS"

run_carbon_tests /home/{pr.repo} || true

""".format(pr=self.pr),
            ),
        ]


@Instance.register("carbon-language", "carbon-lang")
class CarbonLang(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CarbonLangImageDefault(self.pr, self._config)

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

        bazel_passed: set[str] = set()
        bazel_failed: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_gtest_pass = re.compile(r"^\[\s+OK\s+\]\s+(\S+)\s+\(\d+\s*ms\)")
        re_gtest_fail = re.compile(r"^\[\s+FAILED\s+\]\s+(\S+)\s+\(\d+\s*ms\)")
        re_gtest_skip = re.compile(r"^\[\s+SKIPPED\s+\]\s+(\S+)\s+\(\d+\s*ms\)")

        re_detailed_pass = re.compile(r"^PASSED\s+(\S+)\s+\(\S+\)")
        re_detailed_fail = re.compile(r"^FAILED\s+(\S+)\s+\(\S+\)")

        re_bazel_pass = re.compile(r"^(//\S+)\s+(?:\(cached\)\s+)?PASSED\s+in\s+\S+")
        re_bazel_fail = re.compile(r"^(//\S+)\s+FAILED\s+in\s+\S+")
        re_bazel_timeout = re.compile(r"^(//\S+)\s+TIMEOUT\s+in\s+\S+")

        for raw_line in clean_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            m = re_gtest_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
            m = re_gtest_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
            m = re_gtest_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))

            m = re_detailed_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
            m = re_detailed_fail.match(line)
            if m:
                failed_tests.add(m.group(1))

            m = re_bazel_pass.match(line)
            if m:
                bazel_passed.add(m.group(1))
            m = re_bazel_fail.match(line)
            if m:
                bazel_failed.add(m.group(1))
            m = re_bazel_timeout.match(line)
            if m:
                bazel_failed.add(m.group(1))

        if not passed_tests and not failed_tests:
            passed_tests = bazel_passed
            failed_tests = bazel_failed

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
