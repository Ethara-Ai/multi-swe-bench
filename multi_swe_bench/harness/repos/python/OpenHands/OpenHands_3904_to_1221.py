"""OpenHands/OpenHands config for PRs 1221-3904 (Python 3.11, Node >=14.8, poetry)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenHandsImageBase_3904_to_1221(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-3904-to-1221"

    def workdir(self) -> str:
        return "base-3904-to-1221"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git curl gnupg build-essential && rm -rf /var/lib/apt/lists/*
RUN apt-key adv --keyserver keyserver.ubuntu.com --recv-keys F23C5A6CF475977595C89F51BA6932366A755776 && echo "deb http://ppa.launchpad.net/deadsnakes/ppa/ubuntu jammy main" > /etc/apt/sources.list.d/deadsnakes.list && apt-get update && apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev && rm -rf /var/lib/apt/lists/*
RUN python3.11 -c "import urllib.request; urllib.request.urlretrieve('https://install.python-poetry.org', '/tmp/install-poetry.py')" && python3.11 /tmp/install-poetry.py --version 1.8.5 && rm /tmp/install-poetry.py
ENV PATH="/root/.local/bin:$PATH"

{code}

{self.clear_env}

"""


class OpenHandsImageDefault_3904_to_1221(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return OpenHandsImageBase_3904_to_1221(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

poetry env use python3.11
poetry install
poetry add tomli || true
cp config.template.toml config.toml 2>/dev/null || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

cd /home/{repo}
export PATH="/root/.local/bin:$PATH"
PYTEST_FILES=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/.* b/||' | grep '\\.py$' | sort -u)
if [ -n "$PYTEST_FILES" ]; then
    EXISTING=""
    for f in $PYTEST_FILES; do [ -f "$f" ] && EXISTING="$EXISTING $f"; done
    if [ -n "$EXISTING" ]; then
        poetry run pytest --verbose --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
export PATH="/root/.local/bin:$PATH"
PYTEST_FILES=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/.* b/||' | grep '\\.py$' | sort -u)
if [ -n "$PYTEST_FILES" ]; then
    EXISTING=""
    for f in $PYTEST_FILES; do [ -f "$f" ] && EXISTING="$EXISTING $f"; done
    if [ -n "$EXISTING" ]; then
        poetry run pytest --verbose --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
export PATH="/root/.local/bin:$PATH"
PYTEST_FILES=$(grep '^diff --git' /home/test.patch 2>/dev/null | sed 's|diff --git a/.* b/||' | grep '\\.py$' | sort -u)
if [ -n "$PYTEST_FILES" ]; then
    EXISTING=""
    for f in $PYTEST_FILES; do [ -f "$f" ] && EXISTING="$EXISTING $f"; done
    if [ -n "$EXISTING" ]; then
        poetry run pytest --verbose --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("OpenHandsImageDefault dependency must be an Image")
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


@Instance.register("OpenHands", "OpenHands_3904_to_1221")
class OPENHANDS_3904_TO_1221(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpenHandsImageDefault_3904_to_1221(self.pr, self._config)

    _TARGETED_PYTEST = (
        'PYTEST_FILES=$(grep "^diff --git" /home/test.patch 2>/dev/null'
        " | sed 's|diff --git a/.* b/||'"
        " | grep '\\.py$' | sort -u)\n"
        'if [ -n "$PYTEST_FILES" ]; then\n'
        '  EXISTING=""\n'
        '  for f in $PYTEST_FILES; do [ -f "$f" ] && EXISTING="$EXISTING $f"; done\n'
        '  if [ -n "$EXISTING" ]; then\n'
        '    poetry run pytest --verbose --tb=short --no-header -p no:cacheprovider $EXISTING || true\n'
        '  fi\n'
        'fi\n'
    )

    def _make_run_cmd(self, patches: str = "") -> str:
        import base64
        script = "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
        if patches:
            script += patches + "\n"
        script += 'export PATH="/root/.local/bin:$PATH"\n'
        script += self._TARGETED_PYTEST
        b64 = base64.b64encode(script.encode()).decode()
        return f"bash -c 'echo {b64} | base64 -d > /home/_run.sh && bash /home/_run.sh'"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return self._make_run_cmd()

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return self._make_run_cmd("git apply --whitespace=nowarn /home/test.patch")

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return self._make_run_cmd("git apply --whitespace=nowarn /home/test.patch /home/fix.patch")

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # pytest verbose: "tests/path/test_file.py::test_name PASSED [ xx%]"
        pytest_pattern = re.compile(
            r"([^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR)\s+\["
        )

        for match in pytest_pattern.finditer(clean_log):
            test_name = match.group(1)
            status = match.group(2)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Deduplicate: worst wins
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
