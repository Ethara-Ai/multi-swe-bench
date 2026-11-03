import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

PREPARE = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh

python3 -m venv tools/venv
tools/venv/bin/pip install --disable-pip-version-check --no-input -q -r tools/requirements.txt

test -d tests
test -x tools/venv/bin/python
tools/venv/bin/python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)"
python3 -c "import unittest, pathlib, re, subprocess, tempfile, shutil, os, json"
python3 -c "import unittest; c = unittest.defaultTestLoader.discover('tests').countTestCases(); assert c > 0, c; print('DEPS_OK tests=%d' % c)"
"""

TEST_COMMAND = """python3 -m unittest discover -s tests -v 2>&1
"""

RUN_PREFIX = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
"""

APPLY_TEST_PATCH = "git apply --whitespace=nowarn /home/test.patch\n"

APPLY_BOTH_PATCHES = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


class CleanyourtvImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.12-slim"

    def image_tag(self) -> str:
        return "base-1332_to_201"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._infrastructure_block(self, image_name)}
{self.global_env}

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates bash coreutils \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class CleanyourtvImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return CleanyourtvImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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
                CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                PREPARE.replace("__REPO__", repo).replace("__SHA__", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                RUN_PREFIX.replace("__REPO__", repo) + TEST_COMMAND,
            ),
            File(
                ".",
                "test-run.sh",
                RUN_PREFIX.replace("__REPO__", repo) + APPLY_TEST_PATCH + TEST_COMMAND,
            ),
            File(
                ".",
                "fix-run.sh",
                RUN_PREFIX.replace("__REPO__", repo)
                + APPLY_BOTH_PATCHES
                + TEST_COMMAND,
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

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

{Image._HARDENING_BLOCK}"""


@Instance.register("Gathin23", "cleanyourtv")
class CLEANYOURTV_1332_TO_201(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CleanyourtvImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        name_re = re.compile(r"^(?P<method>test_\S*) \((?P<ident>[^)\s]+)\)(?P<rest>.*)$")
        inline_re = re.compile(
            r"^ \.\.\. (?P<verdict>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
        )
        trailing_re = re.compile(
            r"\.\.\. (?P<verdict>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
        )
        bare_re = re.compile(
            r"^(?P<verdict>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
        )

        def record(ident: str, verdict: str) -> None:
            if verdict == "ok":
                passed_tests.add(ident)
            elif verdict in ("FAIL", "ERROR"):
                failed_tests.add(ident)
            elif verdict in ("skipped", "expected failure"):
                skipped_tests.add(ident)
            elif verdict == "unexpected success":
                failed_tests.add(ident)

        pending: Optional[str] = None
        for line in clean.splitlines():
            match = name_re.match(line)
            if match:
                ident = match.group("ident")
                rest = match.group("rest")
                inline = inline_re.match(rest)
                if inline:
                    record(ident, inline.group("verdict"))
                    pending = None
                else:
                    trailing = trailing_re.search(rest)
                    if trailing:
                        record(ident, trailing.group("verdict"))
                        pending = None
                    else:
                        pending = ident
                continue

            if pending is None:
                continue

            bare = bare_re.match(line)
            if bare:
                record(pending, bare.group("verdict"))
                pending = None
                continue

            trailing = trailing_re.search(line)
            if trailing:
                record(pending, trailing.group("verdict"))
                pending = None

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
