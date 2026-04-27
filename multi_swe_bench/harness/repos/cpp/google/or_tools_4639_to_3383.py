from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.cpp.google.or_tools_base import (
    OrToolsImageBase,
)


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Binary diffs (e.g., for .cs, .java, .mps, .nuspec files) cause
    'cannot apply binary patch without full index line' errors with
    git apply. These are typically non-C++ assets not needed for
    compilation or testing.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            # Collect entire diff section and check for binary content
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            # Only include non-binary diff sections
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


class OrToolsCMakeImageDefault(Image):
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
        return OrToolsImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
mkdir -p build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
find . -name CMakeLists.txt -exec sed -i 's/-Werror=[a-zA-Z0-9_=-]*//g; s/-Werror//g' {{}} +
cd build
cmake -S.. -B. \
    -DBUILD_DEPS=ON \
    -DBUILD_SAMPLES=ON \
    -DBUILD_EXAMPLES=ON \
    -DBUILD_TESTING=ON \
    -DBUILD_CXX=ON \
    -DBUILD_PYTHON=OFF \
    -DBUILD_JAVA=OFF \
    -DBUILD_DOTNET=OFF \
    -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc)
CTEST_OUTPUT_ON_FAILURE=1 ctest --output-on-failure -C Release --timeout 300

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

# Remove PATCH_COMMAND references to deleted patch files in CMakeLists.txt
# (patches may be deleted by test/fix patches but CMakeLists still references them)
if [ -f cmake/dependencies/CMakeLists.txt ]; then
  for pfile in patches/*.patch; do
    [ -e "$pfile" ] && continue  # file exists, skip
    # Comment out PATCH_COMMAND lines referencing this missing patch file
    bname=$(basename "$pfile")
    sed -i "/$bname/s/^/#DISABLED# /" cmake/dependencies/CMakeLists.txt 2>/dev/null || true
  done
  # Also handle case where patches dir is entirely gone
  if [ ! -d patches ]; then
    sed -i '/PATCH_COMMAND.*patches\\//s/^/#DISABLED# /' cmake/dependencies/CMakeLists.txt 2>/dev/null || true
  fi
fi

# Clean build dir to force fresh cmake configure after patching
rm -rf build/_deps build/CMakeCache.txt build/CMakeFiles
mkdir -p build

find . -name CMakeLists.txt -exec sed -i 's/-Werror=[a-zA-Z0-9_=-]*//g; s/-Werror//g' {{}} +
cd build
cmake -S.. -B. \
    -DBUILD_DEPS=ON \
    -DBUILD_SAMPLES=ON \
    -DBUILD_EXAMPLES=ON \
    -DBUILD_TESTING=ON \
    -DBUILD_CXX=ON \
    -DBUILD_PYTHON=OFF \
    -DBUILD_JAVA=OFF \
    -DBUILD_DOTNET=OFF \
    -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc) 2>&1 || true
CTEST_OUTPUT_ON_FAILURE=1 ctest --output-on-failure -C Release --timeout 300 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export CI=true

cd /home/{pr.repo}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

# Remove PATCH_COMMAND references to deleted patch files in CMakeLists.txt
if [ -f cmake/dependencies/CMakeLists.txt ]; then
  for pfile in patches/*.patch; do
    [ -e "$pfile" ] && continue
    bname=$(basename "$pfile")
    sed -i "/$bname/s/^/#DISABLED# /" cmake/dependencies/CMakeLists.txt 2>/dev/null || true
  done
  if [ ! -d patches ]; then
    sed -i '/PATCH_COMMAND.*patches\\//s/^/#DISABLED# /' cmake/dependencies/CMakeLists.txt 2>/dev/null || true
  fi
fi

# Clean build dir to force fresh cmake configure after patching
rm -rf build/_deps build/CMakeCache.txt build/CMakeFiles
mkdir -p build

find . -name CMakeLists.txt -exec sed -i 's/-Werror=[a-zA-Z0-9_=-]*//g; s/-Werror//g' {{}} +
cd build
cmake -S.. -B. \
    -DBUILD_DEPS=ON \
    -DBUILD_SAMPLES=ON \
    -DBUILD_EXAMPLES=ON \
    -DBUILD_TESTING=ON \
    -DBUILD_CXX=ON \
    -DBUILD_PYTHON=OFF \
    -DBUILD_JAVA=OFF \
    -DBUILD_DOTNET=OFF \
    -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc) 2>&1 || true
CTEST_OUTPUT_ON_FAILURE=1 ctest --output-on-failure -C Release --timeout 300 2>&1 || true

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


@Instance.register("google", "or_tools_4639_to_3383")
class OR_TOOLS_4639_TO_3383(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OrToolsCMakeImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [
            re.compile(r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed"),
        ]
        re_fail_tests = [
            re.compile(r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed"),
            re.compile(
                r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Subprocess aborted\*+Exception"
            ),
        ]
        re_skip_tests = [
            re.compile(
                r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Not Run \(Disabled\)"
            ),
        ]

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

            for pattern in re_skip_tests:
                match = pattern.match(line)
                if match:
                    skipped_tests.add(match.group(1))

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
