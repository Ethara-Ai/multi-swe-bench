from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
ENV LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl wget git gnupg lsb-release software-properties-common \\
    build-essential make pkg-config zip unzip sudo \\
    python3 python3-pip python-is-python3 \\
    openjdk-21-jdk-headless \\
    libtinfo5 libxml2 m4 \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fSL https://apt.llvm.org/llvm.sh -o /tmp/llvm.sh && \\
    bash /tmp/llvm.sh 19 all && \\
    apt-get install -y --no-install-recommends libc++-19-dev libc++abi-19-dev lld-19 lldb-19 && \\
    ln -sf /usr/bin/clang-19 /usr/bin/clang && \\
    ln -sf /usr/bin/clang++-19 /usr/bin/clang++ && \\
    ln -sf /usr/bin/lld-19 /usr/bin/lld && \\
    ln -sf /usr/bin/ld.lld-19 /usr/bin/ld.lld && \\
    ln -sf /usr/bin/llvm-ar-19 /usr/bin/llvm-ar && \\
    ln -sf /usr/bin/llvm-nm-19 /usr/bin/llvm-nm && \\
    ln -sf /usr/bin/llvm-strip-19 /usr/bin/llvm-strip && \\
    ln -sf /usr/bin/lldb-19 /usr/bin/lldb && \\
    rm /tmp/llvm.sh && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(dpkg --print-architecture) && \\
    curl -fSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${{ARCH}}" \\
      -o /tmp/bazel && \\
    install -m 755 /tmp/bazel /usr/local/bin/bazel && \\
    rm /tmp/bazel

RUN useradd -m -d /home/builder -s /bin/bash builder && \\
    chown -R builder:builder /home/

USER builder

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""".format(pr=self.pr),
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

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY --chown=builder:builder {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8

USER builder

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("carbon-language", "carbon-lang")
class CarbonLang(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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
