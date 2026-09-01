import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ExcalidrawOldImageBase(Image):
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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        return "base-old"

    def workdir(self) -> str:
        return "base-old"

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
RUN apt-get update && apt-get install -y git build-essential python3 make g++ && ln -sf /usr/bin/python3 /usr/bin/python

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

RUN npm install -g yarn --force || true

{code}

{self.clear_env}

"""


class ExcalidrawOldImageDefault(Image):
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
        return ExcalidrawOldImageBase(self.pr, self._config)

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

yarn install --frozen-lockfile --ignore-engines --ignore-scripts || yarn install --ignore-engines --ignore-scripts || true

# Hard verification, deliberately without "|| true": a hollow image must fail
# loudly here rather than yield a container where every stage reports 0 tests.
test -d node_modules
node -e "require.resolve('react-scripts/package.json')"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
CI=true yarn test:app --watchAll=false --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch
yarn install --frozen-lockfile --ignore-engines --ignore-scripts || yarn install --ignore-engines --ignore-scripts
CI=true yarn test:app --watchAll=false --verbose 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' /home/test.patch /home/fix.patch
yarn install --frozen-lockfile --ignore-engines --ignore-scripts || yarn install --ignore-engines --ignore-scripts
CI=true yarn test:app --watchAll=false --verbose 2>&1

""".format(pr=self.pr),
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


@Instance.register("excalidraw", "excalidraw_527_to_6336")
class EXCALIDRAW_527_TO_6336(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExcalidrawOldImageDefault(self.pr, self._config)

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

        # Strip ANSI escapes FIRST. Jest emits colour in code frames and diffs
        # even under CI=true; without this the patterns below silently miss.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # Jest --verbose nests results under the test file and its describe()
        # blocks, by indentation:
        #
        #   PASS src/tests/dragCreate.test.tsx (37.373 s)
        #     add element to the scene when pointer dragging long enough
        #       > rectangle (1951 ms)
        #     do not add element to the scene if size is too small
        #       > rectangle (670 ms)
        #
        # The leaf name alone is ambiguous ("rectangle" appears twice above), so
        # every test is qualified with its file and its describe() chain. That
        # keeps names unique within a file and identical across the three
        # stages, which is what Report.__post_init__ unions on.
        current_file = ""
        suites: list[tuple[int, str]] = []

        marker_pass = "\u2713\u221a\u2714"
        marker_fail = "\u2715\u2716\u00d7"
        marker_skip = "\u25cb\u2298\u25ef"
        tail = r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"

        def qualify(name: str) -> str:
            parts = [s for _, s in suites]
            parts.append(name)
            joined = " > ".join(parts)
            return f"{current_file}::{joined}" if current_file else joined

        for raw_line in log.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            indent = len(raw_line) - len(raw_line.lstrip())

            # File-level result: "PASS src/tests/foo.test.tsx (40.4 s)"
            m = re.match(r"^(PASS|FAIL)\s+(\S+\.(?:test|spec)\.\w+)", line)
            if m:
                current_file = m.group(2)
                suites = []
                if m.group(1) == "PASS":
                    passed_tests.add(current_file)
                else:
                    failed_tests.add(current_file)
                continue

            m = re.match(r"^[" + marker_pass + r"]\s+(.+?)" + tail, line)
            if m:
                passed_tests.add(qualify(m.group(1).strip()))
                continue

            m = re.match(r"^[" + marker_fail + r"]\s+(.+?)" + tail, line)
            if m:
                failed_tests.add(qualify(m.group(1).strip()))
                continue

            m = re.match(r"^[" + marker_skip + r"]\s+(?:skipped\s+)?(.+?)" + tail, line)
            if m:
                skipped_tests.add(qualify(m.group(1).strip()))
                continue

            # Any other indented, unmarked line inside a file block is a
            # describe() header. Console blocks and code frames are emitted
            # after that file's test list, so they can never mis-qualify a test.
            if current_file and indent >= 2 and not line.startswith("\u25cf"):
                while suites and suites[-1][0] >= indent:
                    suites.pop()
                suites.append((indent, line))

            # NOTE: jest's "\u25cf" blocks are deliberately NOT parsed as test
            # names. They also cover non-test output ("\u25cf Console",
            # "\u25cf Test suite failed to run"), and they spell a failure as
            # "suite > test" while the "\u2715" line spells it as "test" - the
            # same test would then appear under two different names across
            # stages and corrupt the union in Report.__post_init__.

        # TestResult.__post_init__ requires these three sets to be disjoint.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
