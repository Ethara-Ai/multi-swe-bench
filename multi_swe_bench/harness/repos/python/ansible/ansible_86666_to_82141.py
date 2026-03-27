import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ansible/ansible — PRs 82141–86666 (Range 3: Python 3.10)
# Python 3.10 era, uses setup.cfg/setup.py, tests via pytest (test/units/)
REPO_DIR = "ansible"


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
        return "python:3.10-slim-bookworm"

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
                """pip install --upgrade pip setuptools
###ACTION_DELIMITER###
pip install -e . || pip install . || true
###ACTION_DELIMITER###
pip install pytest mock pyyaml "jinja2>=3.0.0" paramiko cryptography packaging "resolvelib>=0.5.3,<1.1.0"
###ACTION_DELIMITER###
echo 'python -m pytest -x -v test/units/' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
python -m pytest -x -v test/units/
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -x -v test/units/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -x -v test/units/
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    build-essential \\
    openssh-client \\
    sshpass \\
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \\
        if command -v apk >/dev/null 2>&1; then \\
            apk add --no-cache bash; \\
        elif command -v apt-get >/dev/null 2>&1; then \\
            apt-get update && apt-get install -y bash; \\
        elif command -v yum >/dev/null 2>&1; then \\
            yum install -y bash; \\
        else \\
            exit 1; \\
        fi \\
    fi

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/ansible/ansible.git /home/ansible

WORKDIR /home/ansible
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install --upgrade pip setuptools
RUN pip install -e . || pip install . || true
RUN pip install pytest mock pyyaml "jinja2>=3.0.0" paramiko cryptography packaging "resolvelib>=0.5.3,<1.1.0"
"""
        dockerfile_content += f"""
{copy_commands}
"""
        dockerfile_content += """
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("ansible", "ansible_86666_to_82141")
class ANSIBLE_86666_TO_82141(Instance):
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
        """Parse pytest verbose output from ansible unit tests.

        Matches lines like:
            test/units/path/to/test.py::TestClass::test_method PASSED
            test/units/path/to/test.py::test_function FAILED
            test/units/path/to/test.py::test_function SKIPPED
            PASSED test/units/path/to/test.py::test_function
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Pattern 1: "test_name PASSED/FAILED/SKIPPED" (standard pytest -v)
        # Pattern 2: "PASSED/FAILED/SKIPPED test_name" (alternative format)
        pattern = re.compile(
            r"^\s*(?:([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)|(PASSED|FAILED|SKIPPED|ERROR)\s+([^\s]+))(?:\s+\[.*?\])?\s*$",
            re.MULTILINE,
        )
        for match in pattern.finditer(test_log):
            test_name = (match.group(1) or match.group(4)).strip()
            status = match.group(2) or match.group(3)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
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
