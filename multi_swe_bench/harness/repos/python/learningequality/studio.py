import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult
from multi_swe_bench.utils.python_test import python_test_command


class StudioImageBase(Image):
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
        return "python:3.10-bookworm"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git gcc libpq-dev libssl-dev libffi-dev make gettext libjpeg-dev ffmpeg \\
    postgresql postgresql-client redis-server curl \\
    && rm -rf /var/lib/apt/lists/*

RUN echo "local all all trust" > /etc/postgresql/15/main/pg_hba.conf && \\
    echo "host all all 127.0.0.1/32 trust" >> /etc/postgresql/15/main/pg_hba.conf && \\
    echo "host all all ::1/128 trust" >> /etc/postgresql/15/main/pg_hba.conf && \\
    service postgresql start && \\
    su - postgres -c "createuser -s learningequality" && \\
    su - postgres -c "createdb -O learningequality kolibri-studio" && \\
    service postgresql stop

{code}

WORKDIR /home/{self.pr.repo}

RUN pip install --no-cache-dir pip==25.2 pip-tools && \\
    pip-sync requirements.txt requirements-dev.txt || true

{self.clear_env}

"""


class StudioImageDefault(Image):
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
        return StudioImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = python_test_command(
            self.pr.test_patch,
            base_test_cmd="pytest --no-header -rA --tb=no -p no:cacheprovider --color=no",
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}
pip install --no-cache-dir pip==25.2 pip-tools
pip-sync requirements.txt requirements-dev.txt || true
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

service postgresql start
redis-server --daemonize yes

cd /home/{repo}
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

service postgresql start
redis-server --daemonize yes

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

service postgresql start
redis-server --daemonize yes

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    test_cmd=test_cmd,
                ),
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

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("learningequality", "studio")
class Studio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return StudioImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        test_status_map = {}
        for line in test_log.split("\n"):
            line = line.strip()
            if any(line.startswith(s.value) for s in TestStatus):
                if line.startswith(TestStatus.FAILED.value):
                    line = line.replace(" - ", " ")
                parts = line.split()
                if len(parts) <= 1:
                    continue
                test_name = parts[1]
                test_status_map[test_name] = parts[0]

        return mapping_to_testresult(test_status_map)
