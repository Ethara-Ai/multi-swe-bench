import re
from typing import Optional

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
        return "ubuntu:latest"

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
cat requirements.txt
###ACTION_DELIMITER###
cat requirements_dev.txt
###ACTION_DELIMITER###
cat setup.py
###ACTION_DELIMITER###
apt-get update && apt-get install -y software-properties-common && add-apt-repository ppa:deadsnakes/ppa -y && apt-get update && apt-get install -y python3.7 python3.7-venv python3.7-dev
###ACTION_DELIMITER###
apt-get install -y build-essential libssl-dev libffi-dev libsqlcipher-dev libsecp256k1-dev libgmp-dev
###ACTION_DELIMITER###
python3.7 -m venv venv && source venv/bin/activate && pip install --upgrade pip
###ACTION_DELIMITER###
sed -i 's/gevent==1.5a2/gevent==21.12.0/' requirements.txt
###ACTION_DELIMITER###
sed -i 's/typing-extensions==3.7.4.2/typing-extensions>=3.10.0/' requirements.txt
###ACTION_DELIMITER###
sed -i 's/typing-extensions==3.7.4/typing-extensions>=3.10.0/' requirements.txt
###ACTION_DELIMITER###
sed -i 's/setuptools==40.6.2/setuptools>=50.3.2/' requirements.txt
###ACTION_DELIMITER###
sed -i 's/setuptools==41.0.1/setuptools>=50.3.2/' requirements.txt
###ACTION_DELIMITER###
cat requirements.txt
###ACTION_DELIMITER###
source venv/bin/activate && pip install -r requirements.txt
###ACTION_DELIMITER###
source venv/bin/activate && pip install greenlet==0.4.16
###ACTION_DELIMITER###
source venv/bin/activate && CFLAGS="-I/usr/include/sqlcipher" LDFLAGS="-L/usr/lib/x86_64-linux-gnu -lsqlcipher" pip install pysqlcipher3==1.0.3
###ACTION_DELIMITER###
source venv/bin/activate && pip install coincurve==13.0.0
###ACTION_DELIMITER###
source venv/bin/activate && pip install -r requirements_dev.txt
###ACTION_DELIMITER###
source venv/bin/activate && pip check
###ACTION_DELIMITER###
echo -e '#!/bin/bash
source venv/bin/activate
pytest -v -rA --tb=short -p no:cacheprovider' > test_commands.sh && chmod +x test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
source venv/bin/activate && pip install web3==5.11.1
###ACTION_DELIMITER###
source venv/bin/activate && pip install gql==2.0.0
###ACTION_DELIMITER###
source venv/bin/activate && pip install ipfshttpclient==0.6.0.post1
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
source venv/bin/activate && pip install pytest==5.2.4
###ACTION_DELIMITER###
bash test_commands.sh
###ACTION_DELIMITER###
ls -la rotkehlchen/tests/
###ACTION_DELIMITER###
cat rotkehlchen/tests/conftest.py | head -50
###ACTION_DELIMITER###
source venv/bin/activate && python -c 'import rotkehlchen' 2>&1
###ACTION_DELIMITER###
source venv/bin/activate && pytest --collect-only -v""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
source venv/bin/activate
pytest -v -rA --tb=short -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
source venv/bin/activate
pytest -v -rA --tb=short -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
source venv/bin/activate
pytest -v -rA --tb=short -p no:cacheprovider

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# This is a template for creating a Dockerfile to test patches
# LLM should fill in the appropriate values based on the context

# Choose an appropriate base image based on the project's requirements - replace ubuntu:latest with actual base image
# For example: FROM ubuntu:**, FROM python:**, FROM node:**, FROM centos:**, etc.
FROM ubuntu:latest

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
# For example: RUN apt-get update && apt-get install -y git
# For example: RUN yum install -y git
# For example: RUN apk add --no-cache git
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/rotki/rotki.git /home/rotki

WORKDIR /home/rotki
RUN git reset --hard
RUN git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("rotki", "rotki_1584_to_1067")
class ROTKI_1584_TO_1067(Instance):
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
        passed_tests = set[str]()  # Tests that passed successfully
        failed_tests = set[str]()  # Tests that failed
        skipped_tests = set[str]()  # Tests that were skipped

        # Regex patterns to match test lines
        pattern1 = re.compile(
            r"(?P<test_name>.*?::.*?)\s+(?P<status>PASSED|SKIPPED)\s+\[.*?\]"
        )
        pattern2 = re.compile(
            r"(?P<status>ERROR|FAILED|SKIPPED)\s+(?P<test_name>.*?::.*?)\s+-"
        )
        for line in log.splitlines():
            line = line.strip()
            match1 = pattern1.match(line)
            if match1:
                test_name = match1.group("test_name").strip()
                status = match1.group("status")
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
            else:
                match2 = pattern2.match(line)
                if match2:
                    test_name = match2.group("test_name").strip()
                    status = match2.group("status")
                    if status in ("FAILED", "ERROR"):
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
