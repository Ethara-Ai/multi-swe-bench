import json
import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "node:22"

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

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        mocha_cmd = """\
HAS_CORE=$(node -p "try{!!require('./package.json').scripts['test:core']}catch(e){false}")
HAS_SERVER=$(node -p "try{!!require('./package.json').scripts['test:server']}catch(e){false}")
HAS_JS_SERVER=$(node -p "try{!!require('./package.json').scripts['test:js:server']}catch(e){false}")

if [ "$HAS_CORE" = "true" ]; then
    npx mocha --reporter json "core/**/*.spec.js" "lib/**/*.spec.js" "services/**/*.spec.js"
elif [ "$HAS_SERVER" = "true" ]; then
    npx mocha --reporter json "lib/**/*.spec.js" "services/**/*.spec.js"
elif [ "$HAS_JS_SERVER" = "true" ]; then
    npx mocha --reporter json "*.spec.js" "lib/**/*.spec.js" "services/**/*.spec.js"
else
    npx mocha --reporter json "*.spec.js" "lib/**/*.spec.js"
fi
"""

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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --force || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_CONFIG_ENV=test
export TZ=UTC
export HANDLE_INTERNAL_ERRORS=false

cd /home/{pr.repo}
{mocha_cmd}
""".format(pr=self.pr, mocha_cmd=mocha_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_CONFIG_ENV=test
export TZ=UTC
export HANDLE_INTERNAL_ERRORS=false

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{mocha_cmd}
""".format(pr=self.pr, mocha_cmd=mocha_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_CONFIG_ENV=test
export TZ=UTC
export HANDLE_INTERNAL_ERRORS=false

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{mocha_cmd}
""".format(pr=self.pr, mocha_cmd=mocha_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("badges", "shields")
class BadgesShields(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        data = self._extract_mocha_json(test_log)
        if data:
            for t in data.get("passes", []):
                title = t.get("fullTitle", "")
                if title:
                    passed_tests.add(title)
            for t in data.get("failures", []):
                title = t.get("fullTitle", "")
                if title:
                    failed_tests.add(title)
            for t in data.get("pending", []):
                title = t.get("fullTitle", "")
                if title:
                    skipped_tests.add(title)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    @staticmethod
    def _extract_mocha_json(text: str) -> dict | None:
        text_stripped = text.strip()
        if text_stripped.startswith("{"):
            try:
                return json.loads(text_stripped)
            except json.JSONDecodeError:
                pass

        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None

        candidate = text[first : last + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and ("stats" in data or "passes" in data):
                return data
        except json.JSONDecodeError:
            pass

        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("{") and '"stats"' in text[text.find(line) :]:
                blob = "\n".join(lines[i:])
                depth = 0
                for j, ch in enumerate(blob):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(blob[: j + 1])
                            except json.JSONDecodeError:
                                break
                break

        return None
