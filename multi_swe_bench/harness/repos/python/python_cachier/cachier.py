import re
from typing import Optional

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_DIR = "tests/"

_EXCLUDED_BASENAMES = frozenset({
    "conftest.py",
    "__init__.py",
})

# PR#116: test_patch touches mongo-dependent files + creates standalone_script.py
# (which doesn't exist at base). Only test_memory_core.py and test_pickle_core.py
# contain the hash_params->hash_func rename that produces a clean f2p signal.
_PR116_ALLOWED_FILES = frozenset({
    "tests/test_memory_core.py",
    "tests/test_pickle_core.py",
})


def _test_files_from_patch(patch: str, pr_number: int) -> list[str]:
    seen: set[str] = set()
    for patched_file in PatchSet(patch):
        path = patched_file.target_file
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path == "/dev/null":
            continue
        if path.endswith(".py") and path.startswith(_TEST_DIR):
            basename = path.rsplit("/", 1)[-1]
            if basename not in _EXCLUDED_BASENAMES:
                seen.add(path)

    if pr_number == 116:
        seen = seen & _PR116_ALLOWED_FILES

    return sorted(seen)


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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_files = _test_files_from_patch(self.pr.test_patch, self.pr.number)
        test_files_str = " ".join(test_files)

        if test_files_str:
            pytest_cmd = (
                "python -m pytest --noconftest --no-header -rN --tb=short -v"
                " -m 'not mongo and not sql and not redis'"
                " -p no:cacheprovider -o 'addopts='"
                f" {test_files_str}"
            )
        else:
            pytest_cmd = (
                "python -m pytest --noconftest --no-header -rN --tb=short -v"
                " -m 'not mongo and not sql and not redis'"
                " -p no:cacheprovider -o 'addopts='"
                " tests/"
            )

        # PRs #36, #116, #121, #133 have no tests/requirements.txt;
        # PR #134+ ships one.  The prepare script handles both eras.
        install_cmd = (
            "pip install --no-cache-dir setuptools wheel\n"
            "if [ -f tests/requirements.txt ]; then\n"
            "    pip install --no-cache-dir --no-build-isolation -e . -r tests/requirements.txt\n"
            "else\n"
            "    pip install --no-cache-dir --no-build-isolation -e . && "
            "pip install --no-cache-dir pytest pytest-cov pymongo pandas\n"
            "fi"
        )

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git checkout {sha}\n"
                "\n"
                "{install}\n".format(
                    repo=self.pr.repo, sha=self.pr.base.sha,
                    install=install_cmd,
                ),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fd\n"
                "git apply --whitespace=nowarn /home/test.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fd\n"
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                "{pytest_cmd}\n".format(
                    repo=self.pr.repo, pytest_cmd=pytest_cmd
                ),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(
            f"COPY {f.name} /home/\n" for f in self.files()
        )

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {self.dependency()}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

WORKDIR /home/

{code}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("python-cachier", "cachier")
class CACHIER(Instance):
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

        # pytest -v format: tests/test_core.py::test_name STATUS  [N%]
        pattern = re.compile(
            r"(tests/[^\s]+::[^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)"
        )

        for line in log.splitlines():
            m = pattern.search(line)
            if m:
                test_name = m.group(1)
                status = m.group(2)
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
