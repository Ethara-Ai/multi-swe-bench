"""OpenHands/OpenHands config for PRs 8310-9648 (Python ^3.12,<3.14, Node >=22, make build)."""

import json
import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenHandsImageBase_9648_to_8310(Image):
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
        return "base-9648-to-8310"

    def workdir(self) -> str:
        return "base-9648-to-8310"

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
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git curl gnupg software-properties-common build-essential && rm -rf /var/lib/apt/lists/*
RUN add-apt-repository ppa:deadsnakes/ppa -y && apt-get update && apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev && rm -rf /var/lib/apt/lists/*
RUN python3.12 -c "import urllib.request; urllib.request.urlretrieve('https://install.python-poetry.org', '/tmp/install-poetry.py')" && python3.12 /tmp/install-poetry.py --version 1.8.5 && rm /tmp/install-poetry.py
ENV PATH="/root/.local/bin:$PATH"
RUN mkdir -p /etc/apt/keyrings && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class OpenHandsImageDefault_9648_to_8310(Image):
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
        return OpenHandsImageBase_9648_to_8310(self.pr, self.config)

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

poetry env use python3.12
git submodule update --init --recursive
poetry lock --no-update || true
INSTALL_DOCKER=skip make build
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
        poetry run pytest -v --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
echo "###VITEST_JSON_START###"
cd /home/{repo}/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
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
        poetry run pytest -v --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
echo "###VITEST_JSON_START###"
cd /home/{repo}/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
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
        poetry run pytest -v --tb=short --no-header -p no:cacheprovider $EXISTING || true
    fi
fi
echo "###VITEST_JSON_START###"
cd /home/{repo}/frontend && npx vitest run --reporter=json 2>/dev/null || true
echo "###VITEST_JSON_END###"
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


@Instance.register("OpenHands", "OpenHands_9648_to_8310")
class OPENHANDS_9648_TO_8310(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpenHandsImageDefault_9648_to_8310(self.pr, self._config)

    _TARGETED_PYTEST = (
        'PYTEST_FILES=$(grep "^diff --git" /home/test.patch 2>/dev/null'
        " | sed 's|diff --git a/.* b/||'"
        " | grep '\\.py$' | sort -u)\n"
        'if [ -n "$PYTEST_FILES" ]; then\n'
        '  EXISTING=""\n'
        '  for f in $PYTEST_FILES; do [ -f "$f" ] && EXISTING="$EXISTING $f"; done\n'
        '  if [ -n "$EXISTING" ]; then\n'
        '    poetry run pytest -v --tb=short --no-header -p no:cacheprovider $EXISTING || true\n'
        '  fi\n'
        'fi\n'
    )

    _VITEST_CMD = (
        'echo "###VITEST_JSON_START###"\n'
        'cd /home/{repo}/frontend && npx vitest run --reporter=json 2>/dev/null || true\n'
        'echo "###VITEST_JSON_END###"\n'
    )

    def _make_run_cmd(self, patches: str = "") -> str:
        import base64
        script = "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
        if patches:
            script += patches + "\n"
        script += 'export PATH="/root/.local/bin:$PATH"\n'
        script += self._TARGETED_PYTEST
        script += self._VITEST_CMD.format(repo=self.pr.repo)
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

        # pytest: "test_name PASSED/FAILED/SKIPPED/ERROR [ xx%]"
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

        # vitest JSON between delimiters
        vitest_start = "###VITEST_JSON_START###"
        vitest_end = "###VITEST_JSON_END###"
        start_idx = clean_log.find(vitest_start)
        end_idx = clean_log.find(vitest_end)
        if start_idx != -1 and end_idx != -1:
            json_str = clean_log[start_idx + len(vitest_start):end_idx].strip()
            # Strip TAP protocol header (e.g. "TAP version 13") if present before JSON
            if json_str and not json_str.startswith("{"):
                newline_idx = json_str.find("\n")
                if newline_idx != -1:
                    json_str = json_str[newline_idx + 1:].strip()
            try:
                vitest_data = json.loads(json_str)
                for suite in vitest_data.get("testResults", []):
                    for assertion in suite.get("assertionResults", []):
                        vt_name = f"vitest::{assertion.get('fullName', assertion.get('title', 'unknown'))}"
                        vt_status = assertion.get("status", "")
                        if vt_status == "passed":
                            passed_tests.add(vt_name)
                        elif vt_status == "failed":
                            failed_tests.add(vt_name)
                        elif vt_status in ("pending", "skipped", "todo"):
                            skipped_tests.add(vt_name)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

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
