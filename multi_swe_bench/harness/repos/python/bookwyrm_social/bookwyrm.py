import re

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
        return "python:3.9"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        env_exports = "\n".join([
            'export SECRET_KEY="beepbeep"',
            'export POSTGRES_PASSWORD="password"',
            'export POSTGRES_USER="postgres"',
            'export POSTGRES_DB="bookwyrm"',
            'export POSTGRES_HOST="localhost"',
            'export USE_HTTPS="true"',
        ])
        test_cmd = "pytest -v"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """set -e
###ACTION_DELIMITER###
apt-get update
###ACTION_DELIMITER###
DEBIAN_FRONTEND=noninteractive apt-get install -y gettext libgettextpo-dev tidy postgresql libpq-dev
###ACTION_DELIMITER###
sed -i 's/peer/trust/g' /etc/postgresql/*/main/pg_hba.conf && sed -i 's/md5/trust/g' /etc/postgresql/*/main/pg_hba.conf && sed -i 's/scram-sha-256/trust/g' /etc/postgresql/*/main/pg_hba.conf
###ACTION_DELIMITER###
service postgresql start
###ACTION_DELIMITER###
su - postgres -c "createdb bookwyrm" || true
###ACTION_DELIMITER###
pip install "setuptools<69" && pip install "marshmallow<4" -r requirements.txt || true""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo_name}
service postgresql start 2>/dev/null || true
{env_exports}
{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo_name}
service postgresql start 2>/dev/null || true
{env_exports}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo_name}
service postgresql start 2>/dev/null || true
{env_exports}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        return f"""\
FROM python:3.9

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh
"""


@Instance.register("bookwyrm-social", "bookwyrm")
class Bookwyrm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in clean_log.splitlines():
            line = line.strip()
            # Standard pytest verbose format:
            # bookwyrm/tests/views/test_book.py::BookViews::test_book_page PASSED
            match = re.match(
                r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line
            )
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)

        # Deduplicate (worst result wins)
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
