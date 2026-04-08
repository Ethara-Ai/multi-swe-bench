from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.cpp.envoyproxy.envoy_base import (
    EnvoyImageBase,
)


class EnvoyImageDefault(Image):
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
        return EnvoyImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

# Fix dead external dependency URLs and clang-18 compatibility.
# The quiche-envoy-integration GCS bucket is gone (404);
# googlesource.com archives are non-deterministic (different sha256 each download),
# so we redirect URLs by dep name and disable sha256 checks for affected deps.
LOC_FILE="bazel/repository_locations.bzl"
if [ -f "$LOC_FILE" ]; then
    python3 - "$LOC_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()

# Map dep names to their correct googlesource.com repo
url_map = {{
    "com_googlesource_googleurl": "https://quiche.googlesource.com/googleurl/+archive/{{version}}.tar.gz",
    "com_googlesource_quiche": "https://quiche.googlesource.com/quiche/+archive/{{version}}.tar.gz",
}}

current_dep = None
for i, line in enumerate(lines):
    # Detect dep block start
    for dep_name in url_map:
        if dep_name in line and "dict" in line:
            current_dep = dep_name
            break
    if current_dep:
        # Rewrite GCS URLs to correct googlesource.com repo
        if "quiche-envoy-integration" in line and "urls" in line:
            lines[i] = re.sub(
                r'https://storage\\.googleapis\\.com/quiche-envoy-integration/[^"]*',
                url_map[current_dep],
                line
            )
        # Clear sha256 for affected deps (googlesource archives are non-deterministic)
        if "sha256" in line and "=" in line and "use_category" not in line:
            lines[i] = re.sub(r'sha256\\s*=\\s*"[a-f0-9]+"', 'sha256 = ""', line)
        # End of dep block
        if line.strip() == "),":
            current_dep = None

with open(path, "w") as f:
    f.writelines(lines)
PYEOF
fi

# Patch Bazel's sha256 validation to accept empty hash for affected deps
# v1.17-v1.19 use generated_api_shadow/, v1.20+ use api/
for DEPS_BZL in api/bazel/external_deps.bzl generated_api_shadow/bazel/external_deps.bzl; do
    if [ -f "$DEPS_BZL" ]; then
        sed -i 's/if "sha256" not in location or len(location\\["sha256"\\]) == 0:/if "sha256" not in location:/' "$DEPS_BZL"
    fi
done

# Remove -Werror from Envoy build (clang-18 triggers warnings in older deps' headers)
sed -i 's/"-Werror",//' /home/envoy/bazel/envoy_internal.bzl

# Create compiler wrappers that strip -Werror (handles external deps like upb/abseil)
cat > /home/clang-wrap <<'WRAP'
#!/bin/bash
args=()
for arg in "$@"; do
    case "$arg" in -Werror|-Werror=*) ;; *) args+=("$arg") ;; esac
done
exec /usr/bin/clang -Wno-implicit-function-declaration -DSIGSTKSZ=65536 "${{args[@]}}"
WRAP
cat > /home/clang++-wrap <<'WRAP'
#!/bin/bash
args=()
for arg in "$@"; do
    case "$arg" in -Werror|-Werror=*) ;; *) args+=("$arg") ;; esac
done
exec /usr/bin/clang++ -DSIGSTKSZ=65536 "${{args[@]}}"
WRAP
chmod +x /home/clang-wrap /home/clang++-wrap

""".format(pr=self.pr),
            ),
            File(
                ".",
                "bazel_utils.sh",
                "#!/bin/bash\n"
                "\n"
                "export CC=/home/clang-wrap CXX=/home/clang++-wrap\n"
                "\n"
                "CLANG_CONFIG=\"\"\n"
                "if grep -q '^build:clang ' /home/envoy/.bazelrc 2>/dev/null; then\n"
                "    CLANG_CONFIG=\"--config=clang --action_env=CC=/home/clang-wrap --action_env=CXX=/home/clang++-wrap\"\n"
                "fi\n"
                "\n"
                "derive_test_targets() {\n"
                "    local files=\"$1\"\n"
                "    local targets=\"\"\n"
                "    for f in $files; do\n"
                "        case \"$f\" in\n"
                "            test/*_test.cc|test/*_fuzz_test.cc|contrib/*_test.cc|contrib/*_fuzz_test.cc)\n"
                "                local dir=$(dirname \"$f\")\n"
                "                local base=$(basename \"$f\" .cc)\n"
                "                targets=\"$targets //$dir:$base\"\n"
                "                ;;\n"
                "            test/*_test.h|test/*_test_base.cc|test/*_test_base.h)\n"
                "                local dir=$(dirname \"$f\")\n"
                "                local base=$(basename \"$f\")\n"
                "                local stem=${base%%_test_base.*}_test\n"
                "                targets=\"$targets //$dir:$stem\"\n"
                "                ;;\n"
                "            test/*/BUILD|contrib/*/test/BUILD|contrib/*/test/*/BUILD)\n"
                "                local dir=$(dirname \"$f\")\n"
                "                targets=\"$targets //$dir/...\"\n"
                "                ;;\n"
                "            source/*.cc|source/*.h)\n"
                "                local src_dir=$(dirname \"$f\")\n"
                "                local test_dir=$(echo \"$src_dir\" | sed 's|^source/|test/|')\n"
                "                if [ -d \"/home/envoy/$test_dir\" ]; then\n"
                "                    targets=\"$targets //$test_dir/...\"\n"
                "                fi\n"
                "                ;;\n"
                "        esac\n"
                "    done\n"
                "    # Deduplicate targets\n"
                "    echo \"$targets\" | tr ' ' '\\n' | sort -u | tr '\\n' ' '\n"
                "}\n"
                "\n"
                "run_bazel_test() {\n"
                "    local repo_dir=\"$1\"\n"
                "    shift\n"
                "    local targets=\"$@\"\n"
                "    local max_retries=3\n"
                "    local attempt=0\n"
                "    local outfile=/tmp/bazel_output.log\n"
                "\n"
                "    while [ $attempt -lt $max_retries ]; do\n"
                "        bazel test $CLANG_CONFIG --test_output=all --test_summary=detailed \\\n"
                "            --keep_going --local_test_jobs=1 \\\n"
                "            $targets > \"$outfile\" 2>&1 || true\n"
                "        cat \"$outfile\"\n"
                "\n"
                "        local retry=false\n"
                "\n"
                "        if grep -q 'Checksum was .* but wanted' \"$outfile\"; then\n"
                "            local wanted actual loc_file=\"$repo_dir/bazel/repository_locations.bzl\"\n"
                "            wanted=$(grep -oP 'Checksum was [a-f0-9]+ but wanted \\\\K[a-f0-9]+' \"$outfile\" | head -1)\n"
                "            actual=$(grep -oP 'Checksum was \\\\K[a-f0-9]+' \"$outfile\" | head -1)\n"
                "            if [ -n \"$wanted\" ] && [ -n \"$actual\" ] && [ -f \"$loc_file\" ]; then\n"
                "                echo \"AUTO-FIX: Replacing hash $wanted with $actual in repository_locations.bzl\"\n"
                "                sed -i \"s/$wanted/$actual/g\" \"$loc_file\"\n"
                "                retry=true\n"
                "            fi\n"
                "        fi\n"
                "\n"
                "        if grep -q 'failure_signal_handler.cc.*no matching function for call to.*max' \"$outfile\"; then\n"
                "            local fsh_file=$(find $HOME/.cache/bazel -path '*/com_google_absl/absl/debugging/failure_signal_handler.cc' 2>/dev/null | head -1)\n"
                "            if [ -n \"$fsh_file\" ]; then\n"
                "                echo \"AUTO-FIX: Patching SIGSTKSZ type mismatch in $fsh_file\"\n"
                "                sed -i 's/std::max(SIGSTKSZ, 65536)/std::max<long>(SIGSTKSZ, 65536)/' \"$fsh_file\"\n"
                "                retry=true\n"
                "            fi\n"
                "        fi\n"
                "\n"
                "        if [ \"$retry\" = true ]; then\n"
                "            bazel shutdown 2>/dev/null || true\n"
                "            attempt=$((attempt + 1))\n"
                "            continue\n"
                "        fi\n"
                "        break\n"
                "    done\n"
                "    rm -f \"$outfile\"\n"
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

# Derive test targets from the test patch file (not git diff, since
# run.sh executes on the clean base commit with no local changes).
PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
TARGETS=$(derive_test_targets "$PATCH_FILES")

if [ -z "$TARGETS" ]; then
    echo "No test targets detected from test patch"
    echo "Patch files: $PATCH_FILES"
    exit 0
fi

echo "Test targets: $TARGETS"
run_bazel_test /home/{pr.repo} $TARGETS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
source /home/bazel_utils.sh

# Strip binary diff sections from a patch file (git apply cannot handle
# "Binary files ... differ" without GIT binary patch data)
strip_binary_diffs() {{
    python3 -c "
import sys, re
content = open(sys.argv[1]).read()
# Split into per-file diff sections
parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
for part in parts:
    if part and 'Binary files' not in part:
        sys.stdout.write(part)
" "$1"
}}

cd /home/{pr.repo}
strip_binary_diffs /home/test.patch > /tmp/test_text.patch
git apply --whitespace=nowarn /tmp/test_text.patch

# Derive test targets from the test patch file paths
PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
TARGETS=$(derive_test_targets "$PATCH_FILES")

if [ -z "$TARGETS" ]; then
    echo "No test targets detected from changed files"
    echo "Changed files: $CHANGED_FILES"
    exit 0
fi

echo "Test targets: $TARGETS"
run_bazel_test /home/{pr.repo} $TARGETS

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
source /home/bazel_utils.sh

# Strip binary diff sections from a patch file (git apply cannot handle
# "Binary files ... differ" without GIT binary patch data)
strip_binary_diffs() {{
    python3 -c "
import sys, re
content = open(sys.argv[1]).read()
# Split into per-file diff sections
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

# Derive test targets from the test patch file paths
PATCH_FILES=$(grep -oP '(?<=^diff --git a/)\\S+' /home/test.patch 2>/dev/null || true)
TARGETS=$(derive_test_targets "$PATCH_FILES")

if [ -z "$TARGETS" ]; then
    echo "No test targets detected from changed files"
    echo "Changed files: $CHANGED_FILES"
    exit 0
fi

echo "Test targets: $TARGETS"
run_bazel_test /home/{pr.repo} $TARGETS

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY --chown=builder:builder {file.name} /home/\n"

        clear_env_section = f'{self.clear_env}\n' if self.clear_env else ''

        return (
            '# syntax=docker/dockerfile:1.6\n'
            '\n'
            f'FROM {name}:{tag}\n'
            '\n'
            'ARG TARGETARCH\n'
            f'ARG REPO_URL="{repo_url}"\n'
            'ARG BASE_COMMIT\n'
            '\n'
            'ARG http_proxy=""\n'
            'ARG https_proxy=""\n'
            'ARG HTTP_PROXY=""\n'
            'ARG HTTPS_PROXY=""\n'
            'ARG no_proxy="localhost,127.0.0.1,::1"\n'
            'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
            'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"\n'
            '\n'
            'ENV DEBIAN_FRONTEND=noninteractive \\\n'
            '    LANG=C.UTF-8 \\\n'
            '    TZ=UTC \\\n'
            '    http_proxy=${http_proxy} \\\n'
            '    https_proxy=${https_proxy} \\\n'
            '    HTTP_PROXY=${HTTP_PROXY} \\\n'
            '    HTTPS_PROXY=${HTTPS_PROXY} \\\n'
            '    no_proxy=${no_proxy} \\\n'
            '    NO_PROXY=${NO_PROXY} \\\n'
            '    SSL_CERT_FILE=${CA_CERT_PATH} \\\n'
            '    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n'
            '    CURL_CA_BUNDLE=${CA_CERT_PATH}\n'
            '\n'
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"\n'
            '\n'
            'USER root\n'
            'RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt\n'
            'USER builder\n'
            '\n'
            f'{copy_commands}'
            '\n'
            'RUN bash /home/prepare.sh\n'
            '\n'
            f'{clear_env_section}'
            'CMD ["/bin/bash"]\n'
        )


@Instance.register("envoyproxy", "envoy")
class ENVOY(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return EnvoyImageDefault(self.pr, self._config)

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

        # Bazel target-level results (fallback if no GoogleTest output)
        bazel_passed = set()
        bazel_failed = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # GoogleTest individual test patterns
        # [       OK ] SampleTest.Addition (0 ms)
        re_pass_tests = [
            re.compile(r"^\[\s+OK\s+\]\s+(\S+)\s+\(\d+\s*ms\)"),
        ]
        # [  FAILED  ] FailingTest.ThisWillFail (0 ms)
        re_fail_tests = [
            re.compile(r"^\[\s+FAILED\s+\]\s+(\S+)\s+\(\d+\s*ms\)"),
        ]
        # Bazel --test_summary=detailed patterns
        #     PASSED  FailingTest.ThisWillPass (0.0s)
        re_detailed_pass = re.compile(
            r"^PASSED\s+(\S+)\s+\(\S+\)"
        )
        #     FAILED  FailingTest.ThisWillFail (0.0s)
        re_detailed_fail = re.compile(
            r"^FAILED\s+(\S+)\s+\(\S+\)"
        )
        # Bazel target-level patterns (used as fallback only)
        # //test/common/http:conn_manager_test  PASSED in 12.3s
        re_bazel_pass = re.compile(
            r"^(//\S+)\s+(?:\(cached\)\s+)?PASSED\s+in\s+\S+"
        )
        # //test/common/http:conn_manager_test  FAILED in 12.3s
        re_bazel_fail = re.compile(
            r"^(//\S+)\s+FAILED\s+in\s+\S+"
        )
        # //test/common/http:conn_manager_test  TIMEOUT in 300.0s
        re_bazel_timeout = re.compile(
            r"^(//\S+)\s+TIMEOUT\s+in\s+\S+"
        )

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for pattern in re_pass_tests:
                match = pattern.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for pattern in re_fail_tests:
                match = pattern.match(line)
                if match:
                    failed_tests.add(match.group(1))

            match = re_detailed_pass.match(line)
            if match:
                passed_tests.add(match.group(1))

            match = re_detailed_fail.match(line)
            if match:
                failed_tests.add(match.group(1))

            # Collect Bazel target-level results separately
            match = re_bazel_pass.match(line)
            if match:
                bazel_passed.add(match.group(1))

            match = re_bazel_fail.match(line)
            if match:
                bazel_failed.add(match.group(1))

            match = re_bazel_timeout.match(line)
            if match:
                bazel_failed.add(match.group(1))

        # Use Bazel target-level results only when no GoogleTest
        # individual results were found (fallback for targets that
        # don't produce GoogleTest output)
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
