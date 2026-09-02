import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "/home/python-ecdsa"

TEST_DEPS = '"pytest==7.4.4" "hypothesis==6.92.2" "six==1.16.0"'


TEST_CMD = (
    "python -m pytest -v -rA --color=no -p no:cacheprovider "
    "--continue-on-collection-errors src/ecdsa"
)

SCRIPT_HEADER = f"""#!/bin/bash
set -eo pipefail
export CI=true
cd {REPO_DIR}
"""

EXIT_GUARD = """STATUS=$?
set -e
if [ "$STATUS" -ge 2 ]; then
    echo "FATAL: pytest exited $STATUS - the runner never produced results" >&2
    exit 1
fi
exit 0
"""


class PythonEcdsaImageBase(Image):
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
        return "python:3.11-slim-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "openssl",
            "python3",
            "sudo",
            "wget",
        ]
        packages_str = " \\n    ".join(packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""FROM {base_img}

WORKDIR /home/

{apt_command}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

CMD ["/bin/bash"]
"""


class PythonEcdsaImageDefault(Image):
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
        return PythonEcdsaImageBase(self.pr, self._config)

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd {REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

pip install --no-cache-dir --disable-pip-version-check --no-color \
    --progress-bar off {TEST_DEPS} || true

pip install --no-cache-dir --disable-pip-version-check --no-color \
    --progress-bar off --no-build-isolation -e . || true

python -c "import pytest, hypothesis, six"
python -c "import ecdsa, sys; p = ecdsa.__file__; print(p); sys.exit(0 if p.startswith('{REPO_DIR}/src/') else 1)"
openssl version
python -m pytest --collect-only -q -p no:cacheprovider src/ecdsa > /dev/null
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SCRIPT_HEADER}
set +e
{TEST_CMD}
{EXIT_GUARD}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SCRIPT_HEADER}
if ! git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

set +e
{TEST_CMD}
{EXIT_GUARD}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SCRIPT_HEADER}
if ! git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

set +e
{TEST_CMD}
{EXIT_GUARD}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("tlsfuzzer", "python-ecdsa")
class TLSFUZZER_PYTHON_ECDSA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PythonEcdsaImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        log_no_ansi = ansi_escape.sub("", log)

        node = r"[^\s\[]+::[^\s\[]+(?:\[[^\]]*\])?"
        statuses = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        progress_re = re.compile(rf"^(?P<node>{node})\s+(?P<status>{statuses})")
        summary_re = re.compile(
            rf"^(?P<status>{statuses})\s+(?P<node>{node})(?:\s+-\s+.*)?$"
        )

        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        for line in log_no_ansi.splitlines():
            line = line.strip()
            if not line:
                continue
            match = progress_re.match(line) or summary_re.match(line)
            if not match:
                continue
            buckets[match.group("status")].add(match.group("node"))

        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
