import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "pre-commit"

# PRs: [916, 956]
# Python 3.7 | pip + setup.py | ubuntu-16.04


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
        return "python:3.7-slim-buster"

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
            File(".", "check_git_changes.sh", """#!/bin/bash
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
"""),
            File(".", "prepare.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
echo "deb http://archive.debian.org/debian buster main" > /etc/apt/sources.list
echo "deb http://archive.debian.org/debian-security buster/updates main" >> /etc/apt/sources.list
apt-get update && apt-get install -y --no-install-recommends \
    golang ruby rustc npm nodejs make \
    && rm -rf /var/lib/apt/lists/*
gem install bundler || true
git config --global user.email "you@example.com"
git config --global user.name "Your Name"
pip install -e . || pip install . || true
pip install -r requirements-dev.txt || true
"""),
            File(".", "run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
pytest --no-header -rA --tb=no -p no:cacheprovider
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest --no-header -rA --tb=no -p no:cacheprovider -o continue_on_collection_errors=true || true
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "git apply test.patch failed, trying patch command..." >&2
    patch --batch --fuzz=5 -p1 -i /home/test.patch || {{ echo "Error: test patch apply failed" >&2; exit 1; }}
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "git apply fix.patch failed, trying patch command..." >&2
    patch --batch --fuzz=5 -p1 -i /home/fix.patch || {{ echo "Error: fix patch apply failed" >&2; exit 1; }}
fi
pytest --no-header -rA --tb=no -p no:cacheprovider
"""),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM python:3.7-slim-buster

{self.global_env}

WORKDIR /home/
RUN echo "deb http://archive.debian.org/debian buster main" > /etc/apt/sources.list && \
    echo "deb http://archive.debian.org/debian-security buster/updates main" >> /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    git build-essential ca-certificates curl patch \
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("pre-commit", "pre-commit_956_to_916")
class PreCommit956To916(Instance):
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
        return f'bash -c "cd /home/{REPO_DIR} && (git apply --whitespace=nowarn /home/test.patch 2>/dev/null || patch --batch --fuzz=5 -p1 -i /home/test.patch || exit 1) && pytest --no-header -rA --tb=no -p no:cacheprovider -o continue_on_collection_errors=true; true"'

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Verbose pytest output: tests/foo/bar.py::test_name PASSED/FAILED/SKIPPED
        verbose_re = re.compile(
            r"^(tests/.*?::\S+)\s+(PASSED|FAILED|SKIPPED)", re.MULTILINE
        )
        for match in verbose_re.finditer(log):
            test_name, status = match.groups()
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Short test summary (-rA shows PASSED/FAILED/SKIPPED/ERROR)
        summary_re = re.compile(
            r"^=+\s+short test summary info\s+=+((?:.|\n)*?)^=+.+=$", re.MULTILINE
        )
        summary_match = summary_re.search(log)
        if summary_match:
            summary_content = summary_match.group(1)
            failed_re = re.compile(r"^(?:FAILED|ERROR) (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in failed_re.finditer(summary_content):
                failed_tests.add(match.group(1).strip())
            passed_re = re.compile(r"^PASSED (.*?)$", re.MULTILINE)
            for match in passed_re.finditer(summary_content):
                passed_tests.add(match.group(1).strip())
            skipped_re = re.compile(r"^SKIPPED (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in skipped_re.finditer(summary_content):
                skipped_tests.add(match.group(1).strip())

        # Cleanup: failed tests should not be in passed
        passed_tests.difference_update(failed_tests)
        skipped_tests.difference_update(failed_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
