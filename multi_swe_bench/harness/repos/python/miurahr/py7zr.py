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


def _test_files_from_patch(patch: str) -> list[str]:
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
        test_files = _test_files_from_patch(self.pr.test_patch)
        test_files_str = " ".join(test_files)

        if test_files_str:
            pytest_cmd = f"python -m pytest --no-header -rN --tb=short -v {test_files_str}"
        else:
            pytest_cmd = "python -m pytest --no-header -rN --tb=short -v"

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
                "pip install --no-cache-dir -e '.[test]'\n".format(
                    repo=self.pr.repo, sha=self.pr.base.sha
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


class ImagePR620(ImageDefault):
    """PR-620 requires pytest-httpserver which is not in [test] deps at base commit."""

    def files(self) -> list[File]:
        base_files = super().files()
        patched: list[File] = []
        for f in base_files:
            if f.name == "prepare.sh":
                patched.append(File(
                    f.dir,
                    f.name,
                    f.content + "pip install --no-cache-dir pytest-httpserver\n",
                ))
            else:
                patched.append(f)
        return patched


class ImagePR558(ImageDefault):
    """PR-558 fix patch adds abstract report_update() to Callback base class.

    The existing test_extract_callback defines ECB(ExtractCallback) without
    report_update, so instantiation fails after the fix patch is applied.
    Inject a no-op report_update into ECB in fix-run.sh.
    """

    def files(self) -> list[File]:
        test_files = _test_files_from_patch(self.pr.test_patch)
        test_files_str = " ".join(test_files)

        if test_files_str:
            pytest_cmd = f"python -m pytest --no-header -rN --tb=short -v {test_files_str}"
        else:
            pytest_cmd = "python -m pytest --no-header -rN --tb=short -v"

        inject_report_update = (
            "python -c \"\n"
            "path = 'tests/test_misc.py'\n"
            "with open(path) as f:\n"
            "    content = f.read()\n"
            "old = '        def report_warning(self, message):'\n"
            "new = '        def report_update(self, decompressed_bytes):\\n'\\\n"
            "      '            pass\\n\\n'\\\n"
            "      '        def report_warning(self, message):'\n"
            "content = content.replace(old, new, 1)\n"
            "with open(path, 'w') as f:\n"
            "    f.write(content)\n"
            "\"\n"
        )

        base_files = super().files()
        patched: list[File] = []
        for f in base_files:
            if f.name == "fix-run.sh":
                patched.append(File(
                    f.dir,
                    f.name,
                    "#!/bin/bash\n"
                    "set -e\n"
                    "cd /home/{repo}\n"
                    "git reset --hard\n"
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                    "\n"
                    "{inject}\n"
                    "{pytest_cmd}\n".format(
                        repo=self.pr.repo,
                        inject=inject_report_update,
                        pytest_cmd=pytest_cmd,
                    ),
                ))
            else:
                patched.append(f)
        return patched


@Instance.register("miurahr", "py7zr")
class PY7ZR(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number == 620:
            return ImagePR620(self.pr, self._config)
        if self.pr.number == 558:
            return ImagePR558(self.pr, self._config)
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

        # pytest -v format: tests/test_basic.py::test_name STATUS  [N%]
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
