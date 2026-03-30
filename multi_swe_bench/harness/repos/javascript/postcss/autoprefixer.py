import re
from typing import Optional, Union

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

    def dependency(self) -> Union[str, "Image"]:
        return "ubuntu:latest"

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
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

LABEL org.opencontainers.image.title="postcss/autoprefixer" \\
      org.opencontainers.image.description="postcss/autoprefixer Docker image" \\
      org.opencontainers.image.source="https://github.com/postcss/autoprefixer" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm && \\
    rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN npm install --legacy-peer-deps || npm install --force || true

CMD ["/bin/bash"]
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

    def dependency(self) -> Image | None:
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fd || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || npm install --force || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npx jest --verbose 2>&1; exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- . || true
git clean -fd || true
git apply --exclude='yarn.lock' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch || true
npm install --legacy-peer-deps 2>/dev/null || npm install --force 2>/dev/null || true
npx jest --verbose 2>&1; exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- . || true
git clean -fd || true
git apply --exclude='yarn.lock' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch || true
git apply --exclude='yarn.lock' --exclude='package-lock.json' --whitespace=nowarn /home/fix.patch || true
npm install --legacy-peer-deps 2>/dev/null || npm install --force 2>/dev/null || true
npx jest --verbose 2>&1; exit 0
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

        prepare_commands = "RUN bash /home/prepare.sh; exit 0"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("postcss", "autoprefixer")
class Autoprefixer(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_suite = None

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_pass_test = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_fail_test = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        re_skip_test = re.compile(r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_suite = re_pass_suite.match(line)
            if pass_suite:
                current_suite = pass_suite.group(1)
                continue

            fail_suite = re_fail_suite.match(line)
            if fail_suite:
                current_suite = fail_suite.group(1)
                continue

            pass_test = re_pass_test.match(line)
            if pass_test and current_suite:
                test = f"{current_suite}::{pass_test.group(1)}"
                passed_tests.add(test)
                continue

            fail_test = re_fail_test.match(line)
            if fail_test and current_suite:
                test = f"{current_suite}::{fail_test.group(1)}"
                failed_tests.add(test)
                continue

            skip_test = re_skip_test.match(line)
            if skip_test and current_suite:
                test = f"{current_suite}::{skip_test.group(1)}"
                skipped_tests.add(test)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
