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
                """apt-get update && apt-get install -y python3 python3-pip python3-dev libgirepository1.0-dev libgtk-3-dev libcairo2-dev libpq-dev gir1.2-gtk-3.0
###ACTION_DELIMITER###
pip3 install poetry
###ACTION_DELIMITER###
poetry install
###ACTION_DELIMITER###
echo 'poetry run pytest -v -rA --cache-clear ./tests' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
poetry add setuptools
###ACTION_DELIMITER###
poetry add setuptools@^65.5.1
###ACTION_DELIMITER###
apt-get install -y python3-setuptools
###ACTION_DELIMITER###
poetry run pip install setuptools
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
apt-get install -y libopenblas-dev liblapack-dev libatlas-base-dev
###ACTION_DELIMITER###
echo 'poetry run pytest -v -rA --cache-clear tests/test_configuration.py' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'poetry run pytest -v -rA --cache-clear --continue-on-collection-errors ./tests' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
echo 'poetry run pytest -v --collect-only ./tests' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
apt-get install -y xvfb
###ACTION_DELIMITER###
echo 'xvfb-run poetry run pytest -v -rA --cache-clear ./tests' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
apt-get install -y postgresql postgresql-contrib && service postgresql start && su - postgres -c 'createdb ramstk_test' && su - postgres -c 'psql -c "CREATE USER ramstk WITH PASSWORD \'ramstk\'; GRANT ALL PRIVILEGES ON DATABASE ramstk_test TO ramstk;"'
###ACTION_DELIMITER###
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
service postgresql start
su - postgres -c "createdb ramstk_test" 2>/dev/null || true
su - postgres -c "psql -c \\"CREATE USER ramstk WITH PASSWORD 'ramstk'; GRANT ALL PRIVILEGES ON DATABASE ramstk_test TO ramstk;\\"" 2>/dev/null || true
cd /home/[[REPO_NAME]]
xvfb-run poetry run pytest -v -rA --cache-clear ./tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
service postgresql start
su - postgres -c "createdb ramstk_test" 2>/dev/null || true
su - postgres -c "psql -c \\"CREATE USER ramstk WITH PASSWORD 'ramstk'; GRANT ALL PRIVILEGES ON DATABASE ramstk_test TO ramstk;\\"" 2>/dev/null || true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
xvfb-run poetry run pytest -v -rA --cache-clear ./tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
service postgresql start
su - postgres -c "createdb ramstk_test" 2>/dev/null || true
su - postgres -c "psql -c \\"CREATE USER ramstk WITH PASSWORD 'ramstk'; GRANT ALL PRIVILEGES ON DATABASE ramstk_test TO ramstk;\\"" 2>/dev/null || true
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
xvfb-run poetry run pytest -v -rA --cache-clear ./tests

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN echo 'deb http://archive.ubuntu.com/ubuntu jammy main universe multiverse' > /etc/apt/sources.list && \
    echo 'deb http://archive.ubuntu.com/ubuntu jammy-updates main universe multiverse' >> /etc/apt/sources.list && \
    echo 'deb http://archive.ubuntu.com/ubuntu jammy-security main universe multiverse' >> /etc/apt/sources.list

RUN set -e; success=0; for attempt in 1 2 3 4 5 6 7 8 9 10; do \
      echo "=== apt-get attempt $attempt ===" && \
      rm -rf /var/lib/apt/lists/* && \
      apt-get clean && \
      if apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=120 update && \
         apt-get install -y --fix-missing \
        git python3 python3-pip python3-dev python3-setuptools \
        build-essential gfortran \
        libgirepository1.0-dev libgtk-3-dev libcairo2-dev libpq-dev gir1.2-gtk-3.0 \
        xvfb libopenblas-dev liblapack-dev libatlas-base-dev \
        postgresql postgresql-contrib pkg-config bash; then \
        success=1; break; \
      fi; \
      echo "Attempt $attempt failed, retrying in 120s..."; \
      sleep 120; \
    done; \
    if [ "$success" != "1" ]; then echo "FATAL: apt-get failed after 10 attempts"; exit 1; fi; \
    rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip3 install poetry

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ReliaQualAssociates/ramstk.git /home/ramstk

WORKDIR /home/ramstk
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install deps via poetry (may partially fail due to pandas==1.1.5 or scipy build issues)
RUN poetry install --no-interaction 2>&1 || true

# Install the project package itself (poetry may have aborted before editable install)
RUN poetry run pip install -e . --no-deps 2>&1 || true

# Install ALL project deps with compatible version pins via pip
# poetry install may fail completely on some PRs (e.g. pandas==1.1.5 on Python 3.10)
RUN poetry run pip install 'numpy>=1.21,<1.24' 'setuptools>=65,<70' 2>&1 || true
RUN poetry run pip install --no-deps 'scipy>=1.6,<1.8' 2>&1 || true
RUN poetry run pip install 'pandas>=1.3,<2' 'statsmodels>=0.12,<1' 2>&1 || true
RUN poetry run pip install 'numpy>=1.21,<1.24' 'setuptools>=65,<70' 'pytest>=6,<8' \
    'pypubsub>=4.0.3' 'lifelines>=0.26,<0.28' 'matplotlib>=3.3,<4' 'openpyxl>=3.0' \
    'psycopg2>=2.8' 'sortedcontainers>=2.3' 'sqlalchemy>=1.3,<2' 'sqlalchemy-utils>=0.38' \
    'sympy>=1.8' 'toml>=0.10' 'treelib>=1.5,<2' 'xlrd>=2.0' 'xlwt>=1.3' 'XlsxWriter>=3.0' 2>&1 || true

RUN poetry run python -c 'import pytest; import ramstk; import pandas; import scipy; import pkg_resources' || (echo "FATAL: critical packages missing" && exit 1)

# Setup PostgreSQL database for tests (|| true for multi-arch buildx where networking may not work under QEMU)
RUN (service postgresql start && \\
    su - postgres -c "createdb ramstk_test" && \\
    su - postgres -c "psql -c \\"CREATE USER ramstk WITH PASSWORD 'ramstk'; GRANT ALL PRIVILEGES ON DATABASE ramstk_test TO ramstk;\\"" && \\
    service postgresql stop) || true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ReliaQualAssociates", "ramstk_1068_to_1043")
class RAMSTK_1068_TO_1043(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        import re
        import json

        # TODO: Implement the parse_log function
        # Implement the log parsing logic here
        # Regex pattern to match test names and statuses
        pattern = r".*?(tests/[^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\b.*|.*?(PASSED|FAILED|SKIPPED|ERROR)\b.*?\s+(tests/[^\s]+)"
        for line in log.split("\n"):
            line = line.strip()
            match = re.search(pattern, line)
            if match:
                # Check if the first alternative matched (test followed by status)
                if match.group(1) and match.group(2):
                    test_name = match.group(1)
                    status = match.group(2)
                # Check if the second alternative matched (status followed by test)
                elif match.group(3) and match.group(4):
                    test_name = match.group(4)
                    status = match.group(3)
                else:
                    continue
                # Clean the test name (remove any trailing whitespace or characters)
                test_name = test_name.strip()
                # pytest emits duplicate entries for teardown ERRORs after PASSED; last status wins
                passed_tests.discard(test_name)
                failed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                # Determine the status and add to the appropriate set
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ["FAILED", "ERROR"]:
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
