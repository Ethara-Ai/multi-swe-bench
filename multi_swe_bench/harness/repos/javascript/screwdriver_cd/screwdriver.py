import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NPM_BEFORE = "2020-02-14"

_TEST_BODY = """\
export NODE_OPTIONS="--max_old_space_size=4096"

./node_modules/.bin/mocha --recursive --timeout 10000 --retries 1 --exit \\
    --reporter spec
"""


class ScrewdriverImageBase(Image):
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
        return "node:12"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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


class ScrewdriverImageDefault(Image):
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
        return ScrewdriverImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export npm_config_audit=false
export npm_config_fund=false
export NODE_OPTIONS="--max_old_space_size=4096"

npm install --no-audit --no-fund --before={npm_before} || true

""".format(pr=self.pr, npm_before=_NPM_BEFORE),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PASS_LINE = re.compile(r"^(\s*)[✓✔]\s+(.+?)\s*$")
_FAIL_LINE = re.compile(r"^(\s*)\d+\)\s+(.+?)\s*$")
_SKIP_LINE = re.compile(r"^(\s*)-\s+(.+?)\s*$")
_SUMMARY_LINE = re.compile(r"^\s*\d+\s+(?:passing|failing|pending)\b")
_DURATION = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")


def parse_mocha_spec_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    stack: list[tuple[int, str]] = []
    seen_counts: dict[str, int] = {}

    def path_for(indent: int, leaf: str) -> str:
        parts = [name for width, name in stack if width < indent]
        parts.append(leaf)
        path = " > ".join(parts)
        count = seen_counts.get(path, 0) + 1
        seen_counts[path] = count
        return path if count == 1 else f"{path} #{count}"

    for raw in clean.splitlines():
        if _SUMMARY_LINE.match(raw):
            break

        line = raw.rstrip()
        if not line.strip():
            continue

        m = _PASS_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            passed_tests.add(path_for(indent, leaf))
            continue

        m = _FAIL_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            failed_tests.add(path_for(indent, leaf))
            continue

        m = _SKIP_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            skipped_tests.add(path_for(indent, leaf))
            continue

        indent = len(line) - len(line.lstrip())
        if indent >= 2:
            name = line.strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, name))

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


@Instance.register("screwdriver-cd", "screwdriver")
class Screwdriver(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ScrewdriverImageDefault(self.pr, self._config)

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
        return parse_mocha_spec_log(log)
