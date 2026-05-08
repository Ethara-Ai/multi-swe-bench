from __future__ import annotations

"""grafana/grafana TypeScript-only registry config for multi-swe-bench.

Grafana is a polyglot monorepo: Go backend + TypeScript/React frontend.
This config handles ONLY TypeScript PRs (99 PRs, number_interval='grafana_typescript').
- Base image: golang:latest (Debian Bookworm — provides git, curl; Node via NVM)
- Node managed via NVM — reads .nvmrc at each commit
- Package manager: yarn (yarn.lock present throughout)
- Tests: CI=true yarn test:ci (Jest)
- Parse: Jest output (PASS/FAIL suite lines, individual check/cross/circle markers)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GrafanaTsImageBase(Image):
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base-ts"

    def workdir(self) -> str:
        return "base-ts"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# Install system dependencies and NVM (no version pins)
RUN apt-get update && apt-get install -y --no-install-recommends \\
        curl git jq ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash

{code}

{self.clear_env}

"""


class GrafanaTsImageDefault(Image):
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
        return GrafanaTsImageBase(self.pr, self._config)

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
# nvm.sh sourcing can return non-zero under set -e
set +e
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install Node version specified by .nvmrc (or latest LTS as fallback)
set +e
nvm install 2>/dev/null || nvm install --lts || true
nvm use 2>/dev/null || true
set -e

# Install yarn globally if not present
npm list -g yarn 2>/dev/null | grep -q yarn || npm install -g yarn || true

# Install JS/TS dependencies
yarn install --frozen-lockfile 2>/dev/null || yarn install || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
set +e; nvm use 2>/dev/null; set -eo pipefail

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
set +e; nvm use 2>/dev/null; set -eo pipefail

yarn install --frozen-lockfile 2>/dev/null || yarn install || true

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
set +e; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
set +e; nvm use 2>/dev/null; set -eo pipefail

yarn install --frozen-lockfile 2>/dev/null || yarn install || true

CI=true yarn test:ci 2>&1

""".format(repo=self.pr.repo),
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


@Instance.register("grafana", "grafana_typescript")
class GrafanaTypescript(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaTsImageDefault(self.pr, self._config)

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
        """Parse Jest test output.

        Jest output format (verbose):
            PASS packages/grafana-data/src/dataframe/ArrayDataFrame.test.ts
              Suite Name
                ✓ test description (2 ms)
                ✕ failing test (1 ms)
                ○ skipped test
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Jest suite-level patterns (PASS/FAIL <file>)
        re_jest_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        # Jest individual test patterns
        re_jest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_skip = re.compile(r"^\s*[○⊘]\s+(.+)")

        current_suite = ""
        has_individual_tests = False

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # Suite-level (PASS/FAIL <file>)
            m = re_jest_suite.match(line)
            if m:
                current_suite = m.group(2)
                if not has_individual_tests:
                    if m.group(1) == "PASS":
                        failed_tests.discard(current_suite)
                        skipped_tests.discard(current_suite)
                        passed_tests.add(current_suite)
                    else:
                        passed_tests.discard(current_suite)
                        failed_tests.add(current_suite)
                continue

            # Individual test lines
            m = re_jest_pass.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_jest_fail.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_jest_skip.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
