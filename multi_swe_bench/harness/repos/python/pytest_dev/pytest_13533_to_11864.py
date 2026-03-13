import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "ubuntu:22.04"

    def image_prefix(self) -> str:
        return "envagent"

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
                """python -m pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
python -m pip install "setuptools-scm[toml]>=6.2.3"
###ACTION_DELIMITER###
python -m pip install tox virtualenv
###ACTION_DELIMITER###
python -m pip install -e ".[testing]" || python -m pip install -e .
###ACTION_DELIMITER###
python -m pip install --no-cache-dir pytest-json-report
###ACTION_DELIMITER###
echo 'python -m pytest -x -v testing/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/pytest
python -m pytest -x -v testing/

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/pytest
if ! git -C /home/pytest apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -x -v testing/

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/pytest
if ! git -C /home/pytest apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -x -v testing/

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

# System packages + Python 3.8 via deadsnakes PPA
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    build-essential \\
    curl \\
    software-properties-common \\
    gnupg \\
    && apt-key adv --keyserver keyserver.ubuntu.com --recv-keys F23C5A6CF475977595C89F51BA6932366A755776 \\
    && add-apt-repository ppa:deadsnakes/ppa \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends \\
    python3.8 \\
    python3.8-dev \\
    python3.8-distutils \\
    && rm -rf /var/lib/apt/lists/*

# Set python3.8 as default python/python3
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.8 1 && \\
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.8 1

# Install pip into python3.8 system-wide
RUN curl -sS https://bootstrap.pypa.io/pip/3.8/get-pip.py | python3.8

# Verify python and pip both point to 3.8
RUN python --version && python -m pip --version

# CA cert symlinks for broad SSL compatibility
RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/pytest-dev/pytest.git /home/pytest

WORKDIR /home/pytest
RUN git reset --hard
RUN git fetch origin {pr.base.sha} 2>/dev/null || git fetch --unshallow 2>/dev/null || true
RUN git checkout {pr.base.sha}

# Baked-in prepare.sh steps
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install "setuptools-scm[toml]>=6.2.3"
RUN python -m pip install tox virtualenv

ENV VIRTUALENV_CREATE=false

RUN python -m pip install -e ".[testing]" || python -m pip install -e .

RUN python -m pip install --no-cache-dir pytest-json-report \\
    && printf '#!/bin/bash\\nexec python -m pytest "$@"\\n' > /usr/local/bin/pytest \\
    && chmod +x /usr/local/bin/pytest

RUN python -c "import pytest; print('pytest', pytest.__version__)"
"""
        dockerfile_content += f"""
{copy_commands}
"""
        dockerfile_content += """
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("pytest-dev", "pytest_13533_to_11864")
class PYTEST_13533_TO_11864(Instance):
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

        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED)|(PASSED|FAILED|SKIPPED)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
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
