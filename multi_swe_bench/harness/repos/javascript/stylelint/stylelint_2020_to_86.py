from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase_2020_to_86(Image):
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
        return "node:8-stretch"

    def image_tag(self) -> str:
        return "base-tape"

    def workdir(self) -> str:
        return "base-tape"

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

LABEL org.opencontainers.image.title="stylelint/stylelint" \\
      org.opencontainers.image.description="stylelint/stylelint Docker image (tape era)" \\
      org.opencontainers.image.source="https://github.com/stylelint/stylelint" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/stretch-updates/d' /etc/apt/sources.list

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN npm install || true

CMD ["/bin/bash"]
"""


class ImageDefault_2020_to_86(Image):
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
        return ImageBase_2020_to_86(self.pr, self._config)

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
git clean -fd || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npx babel-tape-runner "src/**/__tests__/**/*.js" 2>&1; exit 0
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
git apply --whitespace=nowarn /home/test.patch || true
npm install 2>/dev/null || true
npx babel-tape-runner "src/**/__tests__/**/*.js" 2>&1; exit 0
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
git apply --whitespace=nowarn /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || true
npm install 2>/dev/null || true
npx babel-tape-runner "src/**/__tests__/**/*.js" 2>&1; exit 0
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


@Instance.register("stylelint", "stylelint_2020_to_86")
class Stylelint_2020_to_86(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_2020_to_86(self.pr, self._config)

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
        """Parse TAP (Test Anything Protocol) output from tape test runner.

        TAP format:
            ok 1 should be accepted
            not ok 2 one should equal two
            # tests 25
            # pass  23
            # fail  2
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^ok\s+(\d+)\s+(.*)$")
        re_fail = re.compile(r"^not ok\s+(\d+)\s+(.*)$")
        re_skip = re.compile(r"^ok\s+(\d+)\s+(.*)#\s*(?:SKIP|skip|TODO|todo)\b")

        current_context = ""

        for line in test_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Track context from comment lines (# > rule: ...)
            if stripped.startswith("# ") and not stripped.startswith("# tests") and not stripped.startswith("# pass") and not stripped.startswith("# fail") and not stripped.startswith("# ok"):
                context_match = re.match(r"^#\s+(.+)$", stripped)
                if context_match:
                    current_context = context_match.group(1).strip()
                continue

            skip_match = re_skip.match(stripped)
            if skip_match:
                test_num = skip_match.group(1)
                test_name = skip_match.group(2).strip()
                if test_name.endswith("#"):
                    test_name = test_name[:-1].strip()
                full_name = f"{current_context}::#{test_num} {test_name}" if current_context else f"#{test_num} {test_name}"
                skipped_tests.add(full_name)
                continue

            pass_match = re_pass.match(stripped)
            if pass_match:
                test_num = pass_match.group(1)
                test_name = pass_match.group(2).strip()
                full_name = f"{current_context}::#{test_num} {test_name}" if current_context else f"#{test_num} {test_name}"
                passed_tests.add(full_name)
                continue

            fail_match = re_fail.match(stripped)
            if fail_match:
                test_num = fail_match.group(1)
                test_name = fail_match.group(2).strip()
                full_name = f"{current_context}::#{test_num} {test_name}" if current_context else f"#{test_num} {test_name}"
                failed_tests.add(full_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
