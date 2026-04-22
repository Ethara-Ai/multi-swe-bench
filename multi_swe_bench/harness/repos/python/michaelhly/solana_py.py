import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "solana-py"


class SolanaPyImageDefault(Image):
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
        if self._pr.number <= 232:
            return "python:3.8-slim"
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _extra_apt_packages(self) -> str:
        if self._pr.number <= 114:
            # Era 1 (setup.py): only git needed
            return ""
        elif self._pr.number <= 232:
            # Era 2 (poetry + PyNaCl): need build tools + libsodium for PyNaCl
            return "build-essential libffi-dev libsodium-dev"
        else:
            # Era 3 (poetry + solders): need build tools for Rust bindings
            return "build-essential"

    def _install_commands(self) -> str:
        if self._pr.number <= 114:
            # Era 1: setup.py based
            cmd = 'RUN pip install --upgrade pip setuptools wheel\nRUN pip install -e ".[dev]"'
            if self._pr.number == 105:
                # cachetools used in blockhash.py but not declared in setup.py at this commit
                cmd += "\nRUN pip install cachetools"
            return cmd
        else:
            # Era 2 & 3: poetry based
            return (
                "RUN pip install --upgrade pip setuptools wheel\n"
                "RUN pip install poetry\n"
                "RUN poetry config virtualenvs.create false\n"
                "RUN poetry lock\n"
                "RUN poetry install"
            )

    def _test_command(self) -> str:
        if self._pr.number <= 114:
            # Era 1: flat layout, need PYTHONPATH
            return (
                'PYTHONPATH=./solana pytest -v -m "not integration" '
                "--no-header -rA --tb=no -p no:cacheprovider"
            )
        elif self._pr.number <= 232:
            # Era 2: src layout, ignore integration
            return (
                'pytest -v -m "not integration" '
                "--no-header -rA --tb=no -p no:cacheprovider "
                "--ignore=src --ignore=tests/integration"
            )
        else:
            # Era 3: src layout, ignore integration, disable anyio
            return (
                'pytest -v -m "not integration" '
                "--no-header -rA --tb=no -p no:cacheprovider "
                "-p no:anyio --ignore=src --ignore=tests/integration"
            )

    def dockerfile(self) -> str:
        base_img = self.dependency()
        extra_apt = self._extra_apt_packages()

        default_packages = (
            "ca-certificates curl git gnupg make python3 sudo wget"
        )
        if extra_apt:
            packages_str = f"{default_packages} {extra_apt}"
        else:
            packages_str = default_packages

        if self.config.need_clone:
            clone = (
                f'RUN git clone "${{REPO_URL}}" /home/{REPO_DIR}'
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        install_cmds = self._install_commands()

        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )

        return f"""FROM {base_img}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {packages_str} \\
    && rm -rf /var/lib/apt/lists/*

{clone}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{install_cmds}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""

    def files(self) -> list[File]:
        test_cmd = self._test_command()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
{test_cmd}
echo '{test_cmd}' > test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
{test_cmd}
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
{test_cmd}
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
{test_cmd}
""",
            ),
        ]


@Instance.register("michaelhly", "solana-py")
class SolanaPy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SolanaPyImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        passed_pattern = re.compile(r"^PASSED\s+(\S+)")
        failed_pattern = re.compile(r"^FAILED\s+(\S+)")
        error_pattern = re.compile(r"^ERROR\s+(\S+)")
        skipped_pattern = re.compile(r"^SKIPPED\s+(\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            match = failed_pattern.match(line) or error_pattern.match(line)
            if match:
                test_results[match.group(1)] = "failed"
                continue

            match = skipped_pattern.match(line)
            if match:
                test_name = match.group(1)
                if test_results.get(test_name) != "failed":
                    test_results[test_name] = "skipped"
                continue

            match = passed_pattern.match(line)
            if match:
                test_name = match.group(1)
                if test_results.get(test_name) not in ("failed", "skipped"):
                    test_results[test_name] = "passed"
                continue

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
