import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TaskiqImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.11"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y patch

{code}

{self.clear_env}

"""


class TaskiqImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return TaskiqImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{self.clear_env}

"""


@Instance.register("taskiq-python", "taskiq")
class Taskiq(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TaskiqImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return (
            'bash -c "'
            "cd /home/" + self.pr.repo + " ; "
            "git checkout -- . ; "
            "patch --batch --fuzz=5 -p1 -i /home/test.patch || true ; "
            "patch --batch --fuzz=5 -p1 -i /home/fix.patch || true ; "
            'pip install -e .\\"[zmq,orjson,msgpack,cbor]\\" 2>&1 || pip install -e . 2>&1 || true ; '
            "pytest --no-header -rA --tb=no -p no:cacheprovider 2>&1 || true"
            '"'
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -rA --tb=no format: PASSED/FAILED/SKIPPED lines
        re_result = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR)\s+(.+?)(?:\s+-\s+.*)?$")
        # pytest short summary: test_file.py::test_name PASSED
        re_short = re.compile(r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR)\s*$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_result.match(line)
            if m:
                status, name = m.group(1), m.group(2).strip()
                if status == "PASSED":
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                elif status == "SKIPPED":
                    skipped_tests.add(name)
                continue

            m = re_short.match(line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                if status == "PASSED":
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                elif status == "SKIPPED":
                    skipped_tests.add(name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
