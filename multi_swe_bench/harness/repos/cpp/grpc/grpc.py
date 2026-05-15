from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GrpcImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "autoconf",
            "automake",
            "ccache",
            "cmake",
            "clang",
            "libc++-dev",
            "libtool",
            "libssl-dev",
            "lsb-release",
            "patch",
            "pkg-config",
            "python3-pip",
            "software-properties-common",
            "strace",
            "unzip",
            "zip",
            "zlib1g-dev",
        ]

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)

        return (
            '# syntax=docker/dockerfile:1.6\n'
            '\n'
            'FROM ubuntu:22.04\n'
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
            'RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt\n'
            '\n'
            'RUN --mount=type=secret,id=mitm_ca,required=0 \\\n'
            '    if [ -f /run/secrets/mitm_ca ]; then \\\n'
            '        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\\n'
            '    fi\n'
            '\n'
            'WORKDIR /home/\n'
            '\n'
            f'RUN apt-get update && apt-get install -y --no-install-recommends \\\n'
            f'    {packages_str} \\\n'
            '    && rm -rf /var/lib/apt/lists/*\n'
            '\n'
            '# Install Bazelisk as Bazel (reads .bazelversion or tools/bazel wrapper\n'
            '# to auto-select the correct Bazel version per checkout)\n'
            'RUN ARCH=$(dpkg --print-architecture) && \\\n'
            '    curl -fSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${ARCH}" \\\n'
            '      -o /usr/local/bin/bazel && \\\n'
            '    chmod +x /usr/local/bin/bazel\n'
            '\n'
            f'RUN git clone "${{REPO_URL}}" /home/{repo}\n'
            '\n'
            f'WORKDIR /home/{repo}\n'
            '\n'
            'RUN git reset --hard\n'
            'RUN git checkout ${BASE_COMMIT}\n'
            '\n'
            '# Initialize submodules (required for WORKSPACE-based Bazel builds)\n'
            'RUN git submodule update --init\n'
            '\n'
            'CMD ["/bin/bash"]\n'
        )


class GrpcImageDefault(Image):
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
        return GrpcImageBase(self.pr, self._config)

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
git submodule update --init

# Remove -Werror from bazel build flags to avoid treating warnings as errors
# with newer clang versions on older gRPC checkouts.
if [ -f tools/bazel.rc ]; then
    sed -i 's/-Werror//g' tools/bazel.rc
fi
if [ -f .bazelrc ]; then
    sed -i 's/-Werror//g' .bazelrc
fi

# Create compiler wrappers that strip -Werror flags from external deps
cat > /home/clang-wrap <<'WRAP'
#!/bin/bash
args=()
for arg in "$@"; do
    case "$arg" in -Werror|-Werror=*) ;; *) args+=("$arg") ;; esac
done
exec /usr/bin/clang "${{args[@]}}"
WRAP
cat > /home/clang++-wrap <<'WRAP'
#!/bin/bash
args=()
for arg in "$@"; do
    case "$arg" in -Werror|-Werror=*) ;; *) args+=("$arg") ;; esac
done
exec /usr/bin/clang++ "${{args[@]}}"
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
                "derive_test_targets() {\n"
                "    local files=\"$1\"\n"
                "    local targets=\"\"\n"
                "    for f in $files; do\n"
                "        case \"$f\" in\n"
                "            # Only emit specific test-source targets; ignore BUILD/CMake/src files\n"
                "            # to avoid bazel //dir/... wildcards that pull in hundreds of tests.\n"
                "            test/*_test.cc)\n"
                "                local dir=$(dirname \"$f\")\n"
                "                local base=$(basename \"$f\" .cc)\n"
                "                targets=\"$targets //$dir:$base\"\n"
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
                "        bazel test --test_output=all --test_summary=detailed \\\n"
                "            --keep_going --local_test_jobs=1 \\\n"
                "            --test_timeout=43200 \\\n"
                "            --action_env=CC=/home/clang-wrap --action_env=CXX=/home/clang++-wrap \\\n"
                "            $targets > \"$outfile\" 2>&1 || true\n"
                "        cat \"$outfile\"\n"
                "\n"
                "        local retry=false\n"
                "\n"
                "        # Auto-fix checksum mismatches in external dependencies\n"
                "        if grep -q 'Checksum was .* but wanted' \"$outfile\"; then\n"
                "            echo \"AUTO-FIX: Checksum mismatch detected, cleaning and retrying\"\n"
                "            bazel clean --expunge 2>/dev/null || true\n"
                "            retry=true\n"
                "        fi\n"
                "\n"
                "        # Auto-fix fetch failures\n"
                "        if grep -q 'Error downloading\\|failed to fetch' \"$outfile\"; then\n"
                "            echo \"AUTO-FIX: Fetch failure detected, retrying\"\n"
                "            bazel shutdown 2>/dev/null || true\n"
                "            retry=true\n"
                "        fi\n"
                "\n"
                "        if [ \"$retry\" = true ]; then\n"
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
    echo "Changed files: $PATCH_FILES"
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
    echo "Changed files: $PATCH_FILES"
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
            copy_commands += f"COPY {file.name} /home/\n"

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
            'RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n'
            '    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt\n'
            '\n'
            f'{copy_commands}'
            '\n'
            'RUN bash /home/prepare.sh\n'
            '\n'
            f'{clear_env_section}'
            'CMD ["/bin/bash"]\n'
        )


@Instance.register("grpc", "grpc")
class GRPC(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GrpcImageDefault(self.pr, self._config)

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
        #     PASSED  SomeTest.SomeCase (0.0s)
        re_detailed_pass = re.compile(
            r"^PASSED\s+(\S+)\s+\(\S+\)"
        )
        #     FAILED  SomeTest.SomeCase (0.0s)
        re_detailed_fail = re.compile(
            r"^FAILED\s+(\S+)\s+\(\S+\)"
        )
        # Bazel target-level patterns (used as fallback only)
        # //test/core/end2end:end2end_test  PASSED in 12.3s
        re_bazel_pass = re.compile(
            r"^(//\S+)\s+(?:\(cached\)\s+)?PASSED\s+in\s+\S+"
        )
        # //test/core/end2end:end2end_test  FAILED in 12.3s
        re_bazel_fail = re.compile(
            r"^(//\S+)\s+FAILED\s+in\s+\S+"
        )
        # //test/core/end2end:end2end_test  TIMEOUT in 300.0s
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
