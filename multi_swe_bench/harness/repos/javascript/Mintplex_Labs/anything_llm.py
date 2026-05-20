from __future__ import annotations

import re
from typing import Optional, Union

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
        return "node:20-bookworm"

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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
ENV LC_ALL=C.UTF-8
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV SHARP_IGNORE_GLOBAL_LIBVIPS=true
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates openssl python3 make g++ && rm -rf /var/lib/apt/lists/*

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

yarn install --ignore-scripts || true

if [ -f server/package.json ]; then
  (cd server && yarn install --frozen-lockfile --ignore-scripts) || (cd server && yarn install --ignore-scripts) || true
fi

if [ -f collector/package.json ]; then
  (cd collector && yarn install --frozen-lockfile --ignore-scripts) || (cd collector && yarn install --ignore-scripts) || true
fi

if [ -f server/prisma/schema.prisma ]; then
  (cd server && npx --yes prisma generate) || true
fi

if [ -f server/.env.example ] && [ ! -f server/.env.development ]; then
  cp server/.env.example server/.env.development || true
fi
if [ -f collector/.env.example ] && [ ! -f collector/.env ]; then
  cp collector/.env.example collector/.env || true
fi
if [ -f frontend/.env.example ] && [ ! -f frontend/.env ]; then
  cp frontend/.env.example frontend/.env || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  echo "No tests configured at this commit."
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

yarn install --ignore-scripts || true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  echo "No tests configured at this commit."
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || \\
  (git apply --whitespace=nowarn --reject /home/test.patch; git apply --whitespace=nowarn --reject /home/fix.patch) || true

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

yarn install --ignore-scripts || true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  echo "No tests configured at this commit."
fi
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


@Instance.register("Mintplex-Labs", "anything-llm")
class AnythingLLM(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_file = re.compile(
            r"^(?:PASS|FAIL)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\b"
        )
        re_test = re.compile(
            r"^(\s+)([✓√✕✗×○⊘↓])\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        re_describe = re.compile(r"^(\s+)([A-Za-z0-9_$#].*?)\s*$")

        ignore_describe_prefixes = (
            "at ",
            "console.",
            "expect(",
            "Expected",
            "Received",
            "Difference:",
        )

        current_file = ""
        describes: list[tuple[int, str]] = []
        name_counts: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line)
            if not line.strip():
                continue

            file_match = re_file.match(line)
            if file_match:
                current_file = file_match.group(1)
                describes = []
                continue

            test_match = re_test.match(line)
            if test_match:
                indent = len(test_match.group(1))
                glyph = test_match.group(2)
                leaf = test_match.group(3).strip()
                path_parts = [d[1] for d in describes if d[0] < indent]
                path_parts.append(leaf)
                full = " > ".join(path_parts)
                base_name = f"{current_file}::{full}" if current_file else full
                count = name_counts.get(base_name, 0) + 1
                name_counts[base_name] = count
                name = base_name if count == 1 else f"{base_name}#{count}"
                if glyph in "✓√":
                    passed_tests.add(name)
                elif glyph in "✕✗×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            desc_match = re_describe.match(line)
            if desc_match:
                indent = len(desc_match.group(1))
                text = desc_match.group(2).strip()
                if (
                    indent < 30
                    and not text.startswith(ignore_describe_prefixes)
                    and not text.startswith(("●", "▼", "▶", "›", ">", "|"))
                ):
                    describes = [d for d in describes if d[0] < indent]
                    describes.append((indent, text))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
