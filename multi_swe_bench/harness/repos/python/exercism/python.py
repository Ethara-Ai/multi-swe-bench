import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Content validation checks appended to every run script.
# These detect docstring/comment changes that fix_patches introduce,
# producing VALIDATION_PASS or VALIDATION_FAIL lines in the log.
VALIDATION_CHECKS = r"""
# Content validation checks
echo ""
echo "#  doccheck-ghost-gobble"
python -c '
try:
    c = open("exercises/concept/ghost-gobble-arcade-game/arcade_game.py").read()
    if "is the player touching a power pellet?" in c:
        print("VALIDATION_PASS: doccheck-ghost-gobble")
    else:
        print("VALIDATION_FAIL: doccheck-ghost-gobble")
except Exception:
    print("VALIDATION_FAIL: doccheck-ghost-gobble")
'

echo ""
echo "#  doccheck-ellens-alien-game"
python -c '
try:
    c = open("exercises/concept/ellens-alien-game/classes.py").read()
    if "#TODO:  create the new_aliens_collection" in c:
        print("VALIDATION_PASS: doccheck-ellens-alien-game")
    else:
        print("VALIDATION_FAIL: doccheck-ellens-alien-game")
except Exception:
    print("VALIDATION_FAIL: doccheck-ellens-alien-game")
'

echo ""
echo "#  doccheck-cater-waiter"
python -c '
try:
    c = open("exercises/concept/cater-waiter/sets.py").read()
    if "<CATEGORY>_INTERSECTIONS" in c:
        print("VALIDATION_PASS: doccheck-cater-waiter")
    else:
        print("VALIDATION_FAIL: doccheck-cater-waiter")
except Exception:
    print("VALIDATION_FAIL: doccheck-cater-waiter")
'
"""

# Project dependency installation — prepended to every run script.
# Kept out of the base Dockerfile so the image contains only system-level deps.
DEPS_INSTALL = (
    "# Install project dependencies\n"
    "pip install --no-cache-dir -r requirements.txt || true\n"
    "pip install --no-cache-dir pytest toml tomli\n"
)


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
        return "python:3.11-slim"

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
pip install -r requirements.txt
###ACTION_DELIMITER###
pytest -v
###ACTION_DELIMITER###
cat requirements.txt
###ACTION_DELIMITER###
pip install pytest
###ACTION_DELIMITER###
pytest -v
###ACTION_DELIMITER###
pytest exercises/ -v
###ACTION_DELIMITER###
ls bin
###ACTION_DELIMITER###
python bin/test_exercises.py -v
###ACTION_DELIMITER###
python bin/test_exercises.py -h
###ACTION_DELIMITER###
python bin/test_exercises.py --runner test-runner
###ACTION_DELIMITER###
apt-get update && apt-get install -y docker-compose
###ACTION_DELIMITER###
service docker start
###ACTION_DELIMITER###
python bin/test_exercises.py --runner test-runner
###ACTION_DELIMITER###
service docker status
###ACTION_DELIMITER###
dockerd --debug
###ACTION_DELIMITER###
update-alternatives --set iptables /usr/sbin/iptables-legacy && update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
###ACTION_DELIMITER###
service docker start
###ACTION_DELIMITER###
python bin/test_exercises.py --runner test-runner
###ACTION_DELIMITER###
python bin/test_exercises.py --runner pytest
###ACTION_DELIMITER###
echo 'python bin/test_exercises.py --runner pytest' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                (
                    "#!/bin/bash\n"
                    "cd /home/[[REPO_NAME]]\n"
                    + DEPS_INSTALL
                    + "python bin/test_exercises.py --runner pytest\n"
                    + VALIDATION_CHECKS
                ).replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    "#!/bin/bash\n"
                    "cd /home/[[REPO_NAME]]\n"
                    + DEPS_INSTALL
                    + "# Apply test patch — skip if empty\n"
                    "if [ -s /home/test.patch ]; then\n"
                    "    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then\n"
                    '        echo "Error: git apply failed" >&2\n'
                    "        exit 1\n"
                    "    fi\n"
                    "fi\n"
                    "python bin/test_exercises.py --runner pytest\n"
                    + VALIDATION_CHECKS
                ).replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    "#!/bin/bash\n"
                    "cd /home/[[REPO_NAME]]\n"
                    + DEPS_INSTALL
                    + "# Apply patches — handle empty test.patch gracefully\n"
                    "if [ -s /home/test.patch ] && [ -s /home/fix.patch ]; then\n"
                    "    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                    '        echo "Error: git apply failed" >&2\n'
                    "        exit 1\n"
                    "    fi\n"
                    "elif [ -s /home/fix.patch ]; then\n"
                    "    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/fix.patch; then\n"
                    '        echo "Error: git apply failed" >&2\n'
                    "        exit 1\n"
                    "    fi\n"
                    "elif [ -s /home/test.patch ]; then\n"
                    "    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then\n"
                    '        echo "Error: git apply failed" >&2\n'
                    "        exit 1\n"
                    "    fi\n"
                    "fi\n"
                    "python bin/test_exercises.py --runner pytest\n"
                    + VALIDATION_CHECKS
                ).replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo_name = self.pr.repo
        return f"""
FROM python:3.11-slim

# Install system-level dependencies only
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo_name}

WORKDIR /home/{repo_name}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
CMD ["/bin/bash"]
"""


@Instance.register("exercism", "python")
class PYTHON(Instance):
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

        # Extract failed tests from test runner summary
        failed_match = re.search(r"FAILURES:\s*(.*)", log)
        if failed_match:
            failed_items = failed_match.group(1).split(", ")
            for item in failed_items:
                test_name = re.sub(r"\s*\(TestFailed\)$", "", item).strip()
                if test_name:
                    failed_tests.add(test_name)

        # Extract content validation failures
        validation_fail_matches = re.findall(
            r"VALIDATION_FAIL:\s*([\w-]+)", log
        )
        for name in validation_fail_matches:
            failed_tests.add(name.strip())

        # Extract all test names from comment lines (e.g., "#  paasio")
        all_test_names = set()
        test_name_matches = re.findall(r"#\s+([\w-]+)\s*", log)
        for name in test_name_matches:
            all_test_names.add(name.strip())
        # Extract skipped tests (adjust regex based on log patterns; example: "skipped: test-name")
        skipped_matches = re.findall(r"skipped:\s*([\w-]+)", log)
        for name in skipped_matches:
            skipped_tests.add(name.strip())
        # Compute passed tests (all test names not failed or skipped)
        passed_tests = all_test_names - failed_tests - skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
