from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for
# carbon-language/carbon-lang.
#
# Every record is a BUNDLE of PRs. The required value is the dash-JOINED
# EXPLICIT list of the bundled PR numbers -- never a "start-end" range:
#
#     prs_in_bundle: [146, 147, 150, 155, 157]
#     number_interval: "146-147-150-155-157"      (NOT "146-157")
#
# A range would claim every PR in between, which is wrong: these bundles are
# sparse (e.g. "5545-6694" bundles exactly two PRs 1,149 apart, and
# "6518-6667-6701-6726-6741-6745-6762-6764" skips hundreds of intervening
# numbers). A two-PR bundle therefore renders as "A-B" legitimately -- that is
# explicit membership, not a range.
#
# SOURCE IS `prs_in_bundle` ONLY -- deliberately NOT `resolved_issues`.
# Unlike the nanopb dump, carbon-lang's `resolved_issues` mixes the bundled PRs
# with LINKED issues that are not part of the bundle (18 of the 28 curated
# records carry such extras, e.g. issue #6717 / #6280). Deriving the interval
# from it would inject non-bundled numbers and produce a key that matches no
# registered bundle. When `prs_in_bundle` is absent we emit nothing rather than
# guess wrong.
#
# Why patch Dataset.build rather than set pr.number_interval at load time:
# `number_interval` is also the ROUTING key -- Instance.create (instance.py:41)
# builds the registry name as f"{org}/{number_interval}" whenever it is
# non-empty. Stamping the value in Dataset.build lands it on the OUTPUT only,
# so routing keeps using whatever the raw record supplied: a dash-joined bundle
# (all are registered in _BUNDLE_NIS_CarbonLang below) or, when the field is
# empty, the plain "carbon-language/carbon-lang" key registered on CarbonLang.
# Both paths resolve, so a re-dump without `number_interval` still routes.
# ---------------------------------------------------------------------------
from multi_swe_bench.harness.dataset import Dataset as _Dataset

_CARBON_ORG = "carbon-language"
_CARBON_REPO = "carbon-lang"


def _carbon_bundle_numbers(pr) -> list[int]:
    """The PR numbers bundled into this record, ascending and de-duplicated."""
    numbers: list[int] = []
    seen: set[int] = set()
    for entry in getattr(pr, "prs_in_bundle", None) or []:
        if isinstance(entry, dict):
            entry = entry.get("number")
        if entry is None:
            continue
        try:
            number = int(entry)
        except (TypeError, ValueError):
            continue
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    # Sorted so the value is deterministic regardless of raw ordering; the
    # registered _BUNDLE_NIS_CarbonLang keys are all ascending, so a sorted join
    # reproduces them exactly.
    return sorted(numbers)


def carbon_number_interval(pr) -> str:
    """Dash-joined explicit bundle list, e.g. "146-147-150-155-157"."""
    return "-".join(str(number) for number in _carbon_bundle_numbers(pr))


# NOTE: Dataset subclasses PullRequest, so a plain getattr() flag check would
# see a flag inherited from another registry's patch and wrongly skip this one;
# check the class's OWN __dict__. Chaining is safe in either import order --
# each registry's wrapper is scoped to its own org/repo and delegates onward.
if not _Dataset.__dict__.get("_carbon_build_patched", False):
    _carbon_orig_build = _Dataset.build.__func__

    def _carbon_build(cls, pr, report):
        ds = _carbon_orig_build(cls, pr, report)
        # Never clobber a value the raw dump already supplied (it is
        # routing-relevant); only fill an empty one.
        if (
            pr.org == _CARBON_ORG
            and pr.repo == _CARBON_REPO
            and not ds.number_interval
        ):
            ds.number_interval = carbon_number_interval(pr)
        return ds

    _Dataset.build = classmethod(_carbon_build)
    _Dataset._carbon_build_patched = True



class CarbonLangImageBase(Image):
    """Shared base for carbon-language/carbon-lang (single-era). Installs the
    LLVM 19 toolchain + Bazelisk ONCE and clones the full repo history; every PR
    image builds `FROM` this base. `# syntax` opts the base out of the
    DockerfileEnhancer so it is NOT pruned/checked-out to a single PR's base.sha
    here — the per-PR anti-reward-hack hardening runs in CarbonLangImageDefault
    at that PR's literal base.sha."""

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
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f'''# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

# Base build deps (image.py defaults + carbon-lang / llvm.sh requirements).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    __APT__ && \\
    rm -rf /var/lib/apt/lists/*

# Install the LLVM 19 toolchain ONCE (shared by every PR image) and symlink the
# unversioned names.
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

# Install Bazelisk as bazel (auto-selects the version from .bazelversion per checkout).
RUN ARCH=$(dpkg --print-architecture) && \\
    curl -fSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${{TARGETARCH:-$ARCH}}" \\
      -o /tmp/bazel && \\
    install -m 755 /tmp/bazel /usr/local/bin/bazel && \\
    rm /tmp/bazel

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
'''.replace("__APT__", r"""ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    python3 \
    sudo \
    wget \
    lsb-release \
    software-properties-common \
    pkg-config \
    zip \
    unzip \
    python3-pip \
    python-is-python3 \
    openjdk-21-jdk-headless \
    libtinfo5 \
    libxml2 \
    m4""")


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

    def dependency(self) -> Optional[Image]:
        return CarbonLangImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set +e

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
            )
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        # Per-PR anti-cheat hardening at the LITERAL base.sha. The shared base
        # keeps full history so every PR's base.sha is reachable; prepare.sh
        # checks out this PR's base.sha, then the hardening block detaches at that
        # literal sha and strips every other ref/reflog so the fix is unreachable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Single-era repo: every bundle key routes to the one CarbonLang class. Each key
# is the EXACT dash-joined prs_in_bundle of one instance -- never a range (§11a).
# Bundle-level, not PR-level: #keys == #instances. Data-derived -- REGENERATE
# whenever the bundles change (§11b).
_BUNDLE_NIS_CarbonLang = [
    "5545-6694",
    "6235-6245-6256-6257-6259-6260-6262-6263-6266-6267",
    "6254-6571-6572",
    "6255-6264-6265-6269",
    "6273-6274",
    "6278-6337",
    "6286-6304-6305-6308",
    "6320-6336",
    "6357-6361-6375-6376-6380-6381-6388-6390-6393",
    "6435-6437-6443-6447",
    "6449-6463-6467-6478-6482-6483-6487",
    "6453-6465-6466-6480-6481",
    "6474-6491-6495-6499",
    "6475-6484-6497-6513",
    "6489-6493",
    "6518-6667-6701-6726-6741-6745-6762-6764",
    "6519-6520-6521",
    "6522-6523-6550-6555-6559-6560",
    "6544-6545",
    "6546-6547-6552",
    "6548-6549-6556-6558",
    "6557-6583-6587-6588-6592-6594-6596-6597-6598-6599-6600-6602",
    "6561-6563-6564-6565-6566-6568",
    "6567-6578-6582",
    "6569-6574-6577-6579-6586-6589-6591",
    "6595-6609-6610-6611-6612-6613-6614-6618",
    "6623-6686-6687-6688-6689-6692",
    "6642-6645",
    "6648-6649-6650",
    "6664-6672",
    "6668-6700-6702-6704",
    "6679-6765",
    "6684-6685",
    "6690-6693-6696",
    "6699-6716-6718-6719-6720-6723-6724",
    "6705-6707-6708-6711",
    "6725-6750-6757-6759",
    "6760-6771-6775-6778",
    "6805-6863-6869-6870",
    "6816-6826-6828-6829",
    "6827-6831-6835-6838-6839-6840-6842",
    "6872-6878-6879-6880-6881-6882-6889",
    "6877-6890-6894-6899-6903",
    "6902-6915-6963-6965-6967",
    "6917-6918-6921-6922",
    "6923-6925-6933-6935",
    "6929-6936-6937-6939-6941-6945",
    "6940-6942-6956-6957-6959",
    "6947-6995-6996-7000-7002-7003-7004-7007-7011",
    "6950-7049-7051-7053-7059-7061-7066-7067-7068-7069-7070-7071-7072-7073-7074-7075-7079-7080-7084",
    "6951-7093-7094-7099-7102-7103-7106-7107",
    "6954-6955-6960-6962-6964-6966-6968-6969",
    "6979-6981",
    "7006-7022-7024-7032-7034-7035",
    "7009-7010-7019-7021",
    "7012-7039-7044",
    "7016-7135-7185-7187-7190-7193-7195",
    "7023-7122-7125-7127-7128-7131-7136-7137",
    "7025-7026",
    "7029-7033-7037-7040-7041",
    "7036-7042-7043-7058",
    "7038-7048",
    "7052-7057-7060",
    "7076-7078-7087-7088-7089-7090-7092-7095-7096-7098",
    "7081-7083-7085-7086",
    "7100-7115-7119-7130",
    "7126-7174-7181-7182-7189",
    "7132-7141-7145-7148-7149-7150-7153-7154-7155-7156-7158",
    "7133-7139-7143-7147-7161-7166",
    "7164-7172",
    "7170-7173-7177-7178",
    "7175-7176-7179-7180",
    "7183-7188-7191-7194-7196-7198-7200-7202-7204-7206",
    "6177-6241-6243-6246-6247-6251",
    "6225-6293-6309-6315-6322",
    "6231-6238-6253-6258-6261",
    "6234-6281-6287",
    "6236-6395-6410-6422-6432-6433",
    "6268-6271-6272-6276-6277",
    "6279-6283-6284-6288-6289-6292-6294-6295-6296-6299",
    "6290-6297-6298-6300-6301",
    "6302-6311-6313",
    "6306-6316-6317-6319-6323-6327-6328",
    "6312-6318-6321-6326-6345-6348-6350",
    "6325-6332-6334-6339-6340",
    "6329-6468-6469-6470-6486-6488",
    "6333-6425-6616-6620-6628-6632-6634-6635-6636-6637-6638-6639-6640",
    "6338-6352-6353-6355-6356",
    "6344-6364-6368-6369-6372-6374",
    "6349-6351-6354-6359-6360-6363-6365-6367",
    "6358-6729-6810-6814-6815-6818-6819-6820",
    "6371-6383-6392-6394-6398-6399-6400-6401-6403-6404",
    "6377-6384-6387-6389",
    "6385-6408-6413-6417",
    "6386-6424-6431-6436",
    "6391-6405-6406-6407-6409-6412-6414",
    "6415-6416-6418-6419-6423-6426-6427-6428",
    "6434-6438-6444-6445-6452",
    "6440-6442",
    "6441-6454-6455-6457-6458",
    "6460-6479-6485-6496-6515-6516-6517",
    "6477-6584-6593-6601-6604-6605-6606-6607-6608",
    "6490-6537-6539-6542",
    "6512-6524-6525-6526-6527-6528",
    "6540-6541",
    "6543-6641-6721-6722-6730-6731-6732-6734-6737-6738",
    "6570-6573-6575-6580-6581",
    "6621-6622-6625",
    "6627-6629-6630-6631-6633",
    "6643-6654",
    "6644-6659-6660",
    "6652-6653-6657-6662-6663-6666",
    "6661-6665-6670",
    "6674-6747-6781-6787-6794-6796-6797-6800-6801-6802-6803",
    "6675-6770-6779-6780-6788",
    "6676-6761-6769-6782-6784-6790-6791-6792-6795",
    "6740-6744-6746-6751-6754-6758",
    "6743-6749",
    "6798-6804-6808-6809-6811-6813",
    "6812-6817-6841-6843-6844-6845-6846-6847-6848-6849-6850-6851-6852-6853-6854-6855-6856-6859-6865-6866",
    "6834-6930-6938-6944-6946-6948",
    "6896-6897-6900-6901-6904-6906-6910",
    "6907-6908-6909-6911-6914",
    "6916-6924-6926-6927-6928-6934",
    "6943-7005-7008-7013-7014-7015-7017-7018-7020",
    "6958-6987-6988-6989-6992-6994-6997-6998-7001",
    "6971-6972-6973-6974-6975-6976-6977-6978-6980-6982-6984-6985-6986",
    "7091-7097-7101-7104-7105-7108-7109-7110-7111-7112",
    "7114-7117-7121-7123-7124",
]
for _ni in _BUNDLE_NIS_CarbonLang:
    Instance.register("carbon-language", _ni)(CarbonLang)

