import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PipenvBase_1426_to_1097(Image):
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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base_1426_to_1097"

    def workdir(self) -> str:
        return "base_1426_to_1097"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6
FROM python:3.9-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/pypa/pipenv.git"
ARG BASE_COMMIT="main"
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="pypa/pipenv" \
      org.opencontainers.image.description="pypa/pipenv multi-swe-bench base image" \
      org.opencontainers.image.source="https://github.com/pypa/pipenv"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then apt-get update && apt-get install -y --no-install-recommends bash && rm -rf /var/lib/apt/lists/*; fi

RUN git clone "${REPO_URL}" /home/pipenv

WORKDIR /home/pipenv
RUN git fetch origin "${BASE_COMMIT}" 2>/dev/null || true
RUN git checkout "${BASE_COMMIT}" 2>/dev/null || true

RUN pip install pipenv

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

    def dependency(self) -> Image:
        return PipenvBase_1426_to_1097(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """ls
###ACTION_DELIMITER###
cat Makefile
###ACTION_DELIMITER###
pipenv install --dev
###ACTION_DELIMITER###
python setup.py install
###ACTION_DELIMITER###
pipenv --version
###ACTION_DELIMITER###
pipenv install --dev
###ACTION_DELIMITER###
cat Pipfile
###ACTION_DELIMITER###
sed -i 's/sphinx = "<=1.5.5"/sphinx = ">=4.0"/' Pipfile
###ACTION_DELIMITER###
cat Pipfile
###ACTION_DELIMITER###
pipenv install --dev
###ACTION_DELIMITER###
apt-get update && apt-get install -y rustc cargo
###ACTION_DELIMITER###
pipenv install --dev
###ACTION_DELIMITER###
apt-get install -y build-essential python3-dev
###ACTION_DELIMITER###
pip install setuptools-rust
###ACTION_DELIMITER###
pipenv install --dev
###ACTION_DELIMITER###
pip install setuptools
###ACTION_DELIMITER###
pipenv install --dev --skip-lock
###ACTION_DELIMITER###
pipenv run pytest -v tests
###ACTION_DELIMITER###
echo 'pipenv run pytest -v tests' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
pipenv install pytest --dev --skip-lock || true
pipenv run pip install 'setuptools<81' 'werkzeug<2.3'
pipenv run pytest -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch 2>/dev/null || git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.tar.gz' --exclude='*.whl' --exclude='*.egg' /home/test.patch 2>/dev/null || echo "WARN: test.patch could not be applied" >&2
pipenv install pytest --dev --skip-lock || true
pipenv run pip install 'setuptools<81' 'werkzeug<2.3'
pipenv run pytest -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null || git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.tar.gz' --exclude='*.whl' --exclude='*.egg' /home/test.patch /home/fix.patch 2>/dev/null || echo "WARN: patches could not be applied" >&2
pipenv install pytest --dev --skip-lock || true
pipenv run pip install 'setuptools<81' 'werkzeug<2.3'
pipenv run pytest -v tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        base_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = (
            "# syntax=docker/dockerfile:1.6\n"
            f"FROM {base_name}\n\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n\n"
            "WORKDIR /home/pipenv\n"
            "RUN git fetch --all\n"
            "RUN git reset --hard\n"
            f"RUN git checkout {self.pr.base.sha}\n\n"
            "WORKDIR /home/\n\n"
            f"{copy_commands}\n"
            "CMD [\"/bin/bash\"]\n"
        )
        return dockerfile_content


@Instance.register("pypa", "pipenv_1426_to_1097")
class PIPENV_1426_TO_1097(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        import re

        pattern = re.compile(
            r"(tests/[^\s]+)[ \t]+(PASSED|FAILED|SKIPPED)"
            r"|(PASSED|FAILED|SKIPPED)[ \t]+(tests/[^\s]+)"
        )
        for match in pattern.findall(log):
            if match[0] and match[1]:
                test_name, status = match[0], match[1]
            else:
                status, test_name = match[2], match[3]
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
