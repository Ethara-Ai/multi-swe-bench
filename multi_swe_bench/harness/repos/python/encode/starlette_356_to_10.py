import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Standalone filter script for binary patches
FILTER_SCRIPT = """\
import sys
import re

patch_file = sys.argv[1]
output_file = sys.argv[2]

with open(patch_file, 'r', errors='replace') as f:
    content = f.read()

starts = [m.start() for m in re.finditer(r'^diff --git ', content, re.MULTILINE)]
parts = []
for i, s in enumerate(starts):
    end = starts[i + 1] if i + 1 < len(starts) else len(content)
    parts.append(content[s:end])

filtered = []
for part in parts:
    if not part.strip():
        continue
    if 'GIT binary patch' in part or 'Binary files' in part:
        continue
    first_line = part.split('\\n')[0]
    if re.search(
        r'\\.(png|jpg|jpeg|gif|ico|bin|woff|woff2|eot|ttf|otf|zip|gz|tar|svg)$',
        first_line, re.IGNORECASE):
        continue
    filtered.append(part)

with open(output_file, 'w') as f:
    f.write(''.join(filtered))
"""

# PRs with test_graphql.py at their base commit need --ignore
# Era 1 PRs 129-356 have test_graphql.py; PRs 10-119 do not
GRAPHQL_PRS = {129, 142, 156, 161, 162, 167, 186, 204, 210, 219, 225, 235, 243, 249, 253, 257, 276, 323, 335, 356}

# PRs that need --ignore=tests/test_database.py (STARLETTE_TEST_DATABASE env var missing)
DATABASE_PRS = {249, 253}


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

    def dependency(self) -> str:
        return "python:3.6-slim-buster"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _pytest_cmd(self) -> str:
        ignores = []
        if self.pr.number in DATABASE_PRS:
            ignores.append("--ignore=tests/test_database.py")
        ignore_str = " ".join(ignores)
        if ignore_str:
            return f"python -m pytest -v --tb=short --timeout=120 {ignore_str} tests/"
        return "python -m pytest -v --tb=short --timeout=120 tests/"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        pytest_cmd = self._pytest_cmd()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_binary_patch.py", FILTER_SCRIPT),
            File(
                ".",
                "prepare.sh",
                """pip install --upgrade "pip<22" "setuptools<58"
###ACTION_DELIMITER###
pip install -r requirements.txt || true
###ACTION_DELIMITER###
pip install -e . || pip install -e ".[full]" || true
###ACTION_DELIMITER###
pip install pytest pytest-cov pytest-timeout || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/[[REPO_NAME]]
{pytest_cmd}
""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/[[REPO_NAME]]

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi
{pytest_cmd}
""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/[[REPO_NAME]]

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch
python3 /home/filter_binary_patch.py /home/fix.patch /tmp/fix_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

if ! git apply --whitespace=nowarn /tmp/fix_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/fix_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/fix_filtered.patch 2>/dev/null || true
    fi
fi

{pytest_cmd}
""".replace(
                    "[[REPO_NAME]]", repo_name
                ),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """\
# syntax=docker/dockerfile:1.6
FROM python:3.6-slim-buster

ARG TARGETARCH
ARG REPO_URL="https://github.com/encode/starlette.git"
ARG BASE_COMMIT
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
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="encode/starlette" \
      org.opencontainers.image.description="encode/starlette Docker image" \
      org.opencontainers.image.source="https://github.com/encode/starlette" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# Fix archived Debian Buster repos (EOL)
RUN sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g; /buster-updates/d' /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends git libssl-dev libffi-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone "${{REPO_URL}}" /home/starlette

WORKDIR /home/starlette
RUN git reset --hard
RUN git fetch origin ${{BASE_COMMIT}} 2>/dev/null || git fetch --unshallow 2>/dev/null || true
RUN git checkout ${{BASE_COMMIT}}

RUN pip install --upgrade "pip<22" "setuptools<58"
RUN pip install -r requirements.txt || true
RUN pip install -e . || pip install -e ".[full]" || true
RUN pip install pytest pytest-cov pytest-timeout || true
RUN pip install "graphql-core<3" "graphene<3" itsdangerous sqlalchemy || true

{copy_commands}
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(copy_commands=copy_commands)


@Instance.register("encode", "starlette_356_to_10")
class STARLETTE_356_TO_10(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_escape.sub("", log)

        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)|(PASSED|FAILED|SKIPPED|ERROR)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Capture file-level collection ERRORs
        file_error_pattern = re.compile(
            r"^ERROR\s+(tests/\S+\.py)\s*$", re.MULTILINE
        )
        for match in file_error_pattern.finditer(log):
            failed_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
