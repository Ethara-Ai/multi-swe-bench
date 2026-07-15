import json
import re
from json import JSONDecoder
from typing import Generator, Optional, Union

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
        return "base48169to47585"

    def workdir(self) -> str:
        return "base48169to47585"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

{code}

RUN apt update && apt install -y git 
RUN npm install -g pnpm@10
RUN apt install -y jq

RUN git config --global --add safe.directory '*'

# Light hardening only: keep FULL history (gc off) so every PR's base.sha can be
# checked out; the PR layer does the strict per-sha strip.
WORKDIR /home/{self.pr.repo}
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
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

jq '.packageManager = "pnpm@^10" | del(.engines)' package.json > temp.json && mv temp.json package.json
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
TZ=UTC pnpm vitest run --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch
TZ=UTC pnpm vitest run --reporter json
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='yarn.lock' --exclude='package-lock.json' --exclude='pnpm-lock.yaml' /home/test.patch /home/fix.patch
TZ=UTC pnpm vitest run --reporter json
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

        # Strict anti-reward-hack hardening at the PR layer with this PR's LITERAL
        # base.sha (shared base keeps full history; each PR strips its own image).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}
{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("mui", "material-ui_48169_to_47585")
class MaterialUi48169to47585(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        def extract_json_objects(
            text: str, decoder=JSONDecoder()
        ) -> Generator[dict, None, None]:
            pos = 0
            while True:
                match = text.find("{", pos)
                if match == -1:
                    break
                try:
                    result, index = decoder.raw_decode(text[match:])
                    yield result
                    pos = match + index
                except ValueError:
                    pos = match + 1

        if "Building new" in test_log:
            test_log = test_log[test_log.find("Building new", 0) :]

        re_removes = [
            re.compile(r"error Command failed with exit code \d+\.", re.DOTALL),
        ]
        for re_remove in re_removes:
            test_log = re_remove.sub("", test_log)

        original_log = test_log
        test_log = test_log.replace("\r\n", "")
        test_log = test_log.replace("\n", "")

        for obj in extract_json_objects(test_log):
            if "numTotalTests" in obj:
                for suite in obj.get("testResults", []):
                    for test in suite.get("assertionResults", []):
                        name = test.get("fullName", test.get("title", ""))
                        status = test.get("status", "")
                        if not name:
                            continue
                        if status == "passed":
                            passed_tests.add(name)
                        elif status == "failed":
                            failed_tests.add(name)
                        elif status in ("skipped", "pending", "disabled"):
                            skipped_tests.add(name)

        for test in failed_tests:
            if test in passed_tests:
                passed_tests.remove(test)
            if test in skipped_tests:
                skipped_tests.remove(test)

        for test in skipped_tests:
            if test in passed_tests:
                passed_tests.remove(test)

        # Fallback: vitest terminal output
        if not passed_tests and not failed_tests and not skipped_tests:
            clean_log = re.sub(r'\x1b\[[0-9;]*m', '', original_log)

            vitest_match = re.search(
                r"Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                clean_log,
            )
            if not vitest_match:
                vitest_match = re.search(
                    r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                    clean_log,
                )
                if vitest_match:
                    vp = int(vitest_match.group(1) or 0)
                    vs = int(vitest_match.group(2) or 0)
                    for i in range(vp):
                        passed_tests.add(f"vitest_pass_{i}")
                    for i in range(vs):
                        skipped_tests.add(f"vitest_skip_{i}")
            else:
                vf = int(vitest_match.group(1) or 0)
                vp = int(vitest_match.group(2) or 0)
                vs = int(vitest_match.group(3) or 0)
                if vp > 0 or vf > 0:
                    for i in range(vp):
                        passed_tests.add(f"vitest_pass_{i}")
                    for i in range(vf):
                        failed_tests.add(f"vitest_fail_{i}")
                    for i in range(vs):
                        skipped_tests.add(f"vitest_skip_{i}")

            # Fallback: mocha dot reporter
            if not passed_tests and not failed_tests:
                dot_pass = re.search(r"(\d+)\s+passing", clean_log)
                dot_fail = re.search(r"(\d+)\s+failing", clean_log)
                dot_pend = re.search(r"(\d+)\s+pending", clean_log)
                dp = int(dot_pass.group(1)) if dot_pass else 0
                df = int(dot_fail.group(1)) if dot_fail else 0
                ds = int(dot_pend.group(1)) if dot_pend else 0
                if dp > 0 or df > 0:
                    for i in range(dp):
                        passed_tests.add(f"dot_pass_{i}")
                    for i in range(df):
                        failed_tests.add(f"dot_fail_{i}")
                    for i in range(ds):
                        skipped_tests.add(f"dot_skip_{i}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- Bundle-level number_interval routing keys (all -> MaterialUi48169to47585) ---
# Each bundle's dash-joined number_interval registered so Instance.create()
# resolves f"mui/{number_interval}" to this era class. Fixes routing: records with
# empty/era-name number_interval otherwise fall through to the mui/material-ui fallback.
_BUNDLE_NIS_MATERIALUI48169TO47585 = [
    "47789-47791-47794-47805-47810-47815-47840-47841-47848-47851-47853-47855-47862-47873-47874-47888-47893-47905-47908-47909-47910-47911",
    "47820-47913-47914-47916-47917-47921-47932-47934-47938-47942-47944-47946-47954-47955-47961-47969-47994-48022-48023-48027-48033-48109-48144-48152-48160-48161-48162-48168-48210-48217-48220-48222",
]
for _ni in _BUNDLE_NIS_MATERIALUI48169TO47585:
    Instance.register("mui", _ni)(MaterialUi48169to47585)
