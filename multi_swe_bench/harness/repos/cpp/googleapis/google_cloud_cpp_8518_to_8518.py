import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Fc34V2ImageBase(Image):
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
        return "fedora:34"

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
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN sed -i -e 's|^metalink=|#metalink=|g' \
       -e 's|^#baseurl=http://download.example/pub/fedora/linux|baseurl=https://archives.fedoraproject.org/pub/archive/fedora/linux|g' \
       /etc/yum.repos.d/fedora*.repo

RUN dnf makecache && dnf install -y \\
    cmake gcc-c++ git make ninja-build \\
    patch pkg-config tar wget curl zip unzip findutils \\
    libcurl-devel openssl-devel zlib-devel \\
    gtest-devel gmock-devel \\
    c-ares-devel re2-devel \\
    && dnf clean all

WORKDIR /var/tmp/build
RUN curl -sSL https://github.com/abseil/abseil-cpp/archive/20211102.0.tar.gz | \\
    tar -xzf - --strip-components=1 && \\
    sed -i 's/^#define ABSL_OPTION_USE_\\(.*\\) 2/#define ABSL_OPTION_USE_\\1 0/' "absl/base/options.h" && \\
    cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=yes \\
      -DBUILD_TESTING=OFF -DABSL_BUILD_TESTING=OFF \\
      -GNinja -S . -B cmake-out && \\
    cmake --build cmake-out --target install && \\
    ldconfig && cd /var/tmp && rm -fr build

WORKDIR /var/tmp/build
RUN curl -sSL https://github.com/protocolbuffers/protobuf/archive/v3.19.4.tar.gz | \\
    tar -xzf - --strip-components=1 && \\
    cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=yes \\
      -Dprotobuf_BUILD_TESTS=OFF -Dprotobuf_ABSL_PROVIDER=package \\
      -GNinja -S cmake -B cmake-out && \\
    cmake --build cmake-out --target install && \\
    ldconfig && cd /var/tmp && rm -fr build

WORKDIR /var/tmp/build
RUN curl -sSL https://github.com/grpc/grpc/archive/v1.44.0.tar.gz | \\
    tar -xzf - --strip-components=1 && \\
    cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \\
      -DgRPC_INSTALL=ON -DgRPC_BUILD_TESTS=OFF \\
      -DgRPC_ABSL_PROVIDER=package -DgRPC_CARES_PROVIDER=package \\
      -DgRPC_PROTOBUF_PROVIDER=package -DgRPC_RE2_PROVIDER=package \\
      -DgRPC_SSL_PROVIDER=package -DgRPC_ZLIB_PROVIDER=package \\
      -GNinja -S . -B cmake-out && \\
    cmake --build cmake-out --target install && \\
    ldconfig && cd /var/tmp && rm -fr build

WORKDIR /var/tmp/build
RUN curl -sSL https://github.com/nlohmann/json/archive/v3.10.5.tar.gz | \\
    tar -xzf - --strip-components=1 && \\
    cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=yes \\
      -DBUILD_TESTING=OFF -DJSON_BuildTests=OFF \\
      -GNinja -S . -B cmake-out && \\
    cmake --build cmake-out --target install && \\
    ldconfig && cd /var/tmp && rm -fr build

WORKDIR /var/tmp/build
RUN curl -sSL https://github.com/google/crc32c/archive/1.1.2.tar.gz | \\
    tar -xzf - --strip-components=1 && \\
    cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=yes \\
      -DCRC32C_BUILD_TESTS=OFF -DCRC32C_BUILD_BENCHMARKS=OFF -DCRC32C_USE_GLOG=OFF \\
      -GNinja -S . -B cmake-out && \\
    cmake --build cmake-out --target install && \\
    ldconfig && cd /var/tmp && rm -fr build

RUN ldconfig /usr/local/lib*

WORKDIR /home/

{code}

{self.clear_env}

"""


class Fc34V2ImageDefault(Image):
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
        return Fc34V2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

mkdir -p build && cd build
cmake -S /home/{pr.repo} -B /home/{pr.repo}/build \\
    -DBUILD_TESTING=ON \\
    -DGOOGLE_CLOUD_CPP_ENABLE=storage,bigtable,spanner,pubsub,bigquery,iam,logging,kms,secretmanager,compute \\
    -DGOOGLE_CLOUD_CPP_ENABLE_EXAMPLES=OFF \\
    -DCMAKE_BUILD_TYPE=Debug \\
    -GNinja
cmake --build /home/{pr.repo}/build -j $(nproc)

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}/build
ctest --output-on-failure

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd build
cmake --build . -j $(nproc)
ctest --output-on-failure

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd build
cmake --build . -j $(nproc)
ctest --output-on-failure

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("googleapis", "google-cloud-cpp_8518_to_8518")
class GoogleCloudCpp8518To8518(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Fc34V2ImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed")]
        re_fail_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Timeout"),
        ]
        re_skip_tests = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Not Run")
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue
            for rp in re_pass_tests:
                m = rp.match(line)
                if m:
                    passed_tests.add(m.group(1))
            for rf in re_fail_tests:
                m = rf.match(line)
                if m:
                    failed_tests.add(m.group(1))
            for rs in re_skip_tests:
                m = rs.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
