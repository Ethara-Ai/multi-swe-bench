from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
class MsgpackCMasterImageBase(Image):

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
        return "base-master"

    def workdir(self) -> str:
        return "base-master"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "cmake",
            "g++",
            "libboost-all-dev",
            "zlib1g-dev",
            "wget",
            "unzip",
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
            "make",
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
            f'RUN git clone "${{REPO_URL}}" /home/{repo}\n'
            '\n'
            f'WORKDIR /home/{repo}\n'
            '\n'
            'RUN git reset --hard\n'
            'RUN git checkout ${BASE_COMMIT}\n'
            '\n'
            '# Build GTest 1.7.0 from source (matches original Travis CI)\n'
            'RUN cd /tmp && \\\n'
            '    wget -q https://github.com/google/googletest/archive/release-1.7.0.zip -O gtest.zip && \\\n'
            '    unzip -q gtest.zip && \\\n'
            '    cd googletest-release-1.7.0 && \\\n'
            '    g++ -fPIC src/gtest-all.cc -I. -Iinclude -c && \\\n'
            '    g++ -fPIC src/gtest_main.cc -I. -Iinclude -c && \\\n'
            '    ar -rv /usr/lib/libgtest.a gtest-all.o && \\\n'
            '    ar -rv /usr/lib/libgtest_main.a gtest_main.o && \\\n'
            '    cp -r include/gtest /usr/include/ && \\\n'
            '    cd / && rm -rf /tmp/googletest-release-1.7.0 /tmp/gtest.zip\n'
            '\n'
            'CMD ["/bin/bash"]\n'
        )


class MsgpackCMasterImageDefault(Image):
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
        return MsgpackCMasterImageBase(self.pr, self._config)

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
find . -name CMakeLists.txt -exec sed -i 's/-Werror//g' {{}} +
cd build
cmake -DMSGPACK_BUILD_TESTS=ON -DMSGPACK_BUILD_EXAMPLES=OFF -DMSGPACK_BOOST=ON -DMSGPACK_CXX11=ON ..
cmake --build . -- -j$(nproc)
ctest --output-on-failure

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='fuzz/unpack_pack_fuzzer_regressions/*' --exclude='fuzz/unpack_pack_fuzzer_seed_corpus/*' --exclude='*.mpac' /home/test.patch
find . -name CMakeLists.txt -exec sed -i 's/-Werror//g' {{}} +
cd build
cmake -DMSGPACK_BUILD_TESTS=ON -DMSGPACK_BUILD_EXAMPLES=OFF -DMSGPACK_BOOST=ON -DMSGPACK_CXX11=ON ..
cmake --build . -- -j$(nproc)
ctest --output-on-failure

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='fuzz/unpack_pack_fuzzer_regressions/*' --exclude='fuzz/unpack_pack_fuzzer_seed_corpus/*' --exclude='*.mpac' /home/test.patch /home/fix.patch
find . -name CMakeLists.txt -exec sed -i 's/-Werror//g' {{}} +
cd build
cmake -DMSGPACK_BUILD_TESTS=ON -DMSGPACK_BUILD_EXAMPLES=OFF -DMSGPACK_BOOST=ON -DMSGPACK_CXX11=ON ..
cmake --build . -- -j$(nproc)
ctest --output-on-failure

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


@Instance.register("msgpack", "msgpack_c_875_to_556")
class MSGPACK_C_875_TO_556(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return MsgpackCMasterImageDefault(self.pr, self._config)

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
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed"),
        ]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed"),
        ]
        re_skip_tests = [
            re.compile(
                r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Not Run \(Disabled\)"
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
