from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.cpp.ros2.geometry2_base import (
    Geometry2ImageBase,
    Geometry2IronImageBase,
)


def _make_files(pr: PullRequest, distro: str) -> list[File]:
    return [
        File(".", "fix.patch", f"{pr.fix_patch}"),
        File(".", "test.patch", f"{pr.test_patch}"),
        File(
            ".",
            "prepare.sh",
            """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

""".format(pr=pr),
        ),
        File(
            ".",
            "run.sh",
            """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /opt/ros/{distro}/setup.bash
colcon build --event-handlers console_direct+ 2>&1
colcon test --event-handlers console_direct+ 2>&1
colcon test-result --verbose 2>&1

""".format(pr=pr, distro=distro),
        ),
        File(
            ".",
            "test-run.sh",
            """#!/bin/bash

cd /home/{pr.repo}
source /opt/ros/{distro}/setup.bash

if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi

colcon build --event-handlers console_direct+ 2>&1 || true
colcon test --event-handlers console_direct+ 2>&1 || true
colcon test-result --verbose 2>&1 || true

""".format(pr=pr, distro=distro),
        ),
        File(
            ".",
            "fix-run.sh",
            """#!/bin/bash

cd /home/{pr.repo}
source /opt/ros/{distro}/setup.bash

if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi

colcon build --event-handlers console_direct+ 2>&1 || true
colcon test --event-handlers console_direct+ 2>&1 || true
colcon test-result --verbose 2>&1 || true

""".format(pr=pr, distro=distro),
        ),
    ]


def _default_dockerfile(image: Image) -> str:
    dep = image.dependency()
    name = dep.image_name()
    tag = dep.image_tag()
    org, repo = image.pr.org, image.pr.repo
    repo_url = f"https://github.com/{org}/{repo}.git"

    copy_commands = ""
    for file in image.files():
        copy_commands += f"COPY {file.name} /home/\n"

    clear_env_section = f'{image.clear_env}\n' if image.clear_env else ''

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


def _parse_ctest_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    re_pass = re.compile(r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed")
    re_fail = re.compile(r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed")
    re_abort = re.compile(
        r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Subprocess aborted\*+Exception"
    )
    re_skip = re.compile(
        r"^\s*\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Not Run \(Disabled\)"
    )

    for line in clean_log.splitlines():
        line = line.strip()
        if not line:
            continue

        m = re_pass.match(line)
        if m:
            passed_tests.add(m.group(1))
            continue

        m = re_fail.match(line)
        if m:
            failed_tests.add(m.group(1))
            continue

        m = re_abort.match(line)
        if m:
            failed_tests.add(m.group(1))
            continue

        m = re_skip.match(line)
        if m:
            skipped_tests.add(m.group(1))

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


class Geometry2ImageDefault(Image):
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
        return Geometry2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return _make_files(self.pr, "humble")

    def dockerfile(self) -> str:
        return _default_dockerfile(self)


@Instance.register("ros2", "geometry2")
class GEOMETRY2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Geometry2ImageDefault(self.pr, self._config)

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
        return _parse_ctest_log(test_log)


class Geometry2IronImageDefault(Image):
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
        return Geometry2IronImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return _make_files(self.pr, "jazzy")

    def dockerfile(self) -> str:
        return _default_dockerfile(self)


@Instance.register("ros2", "geometry2_897_to_673")
class GEOMETRY2_IRON(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Geometry2IronImageDefault(self.pr, self._config)

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
        return _parse_ctest_log(test_log)
