"""grafana/grafana registry config for multi-swe-bench.

Grafana is a polyglot monorepo: Go backend + TypeScript/React frontend.
- 83 PRs, #19416 (2019) to #117038 (2026)
- 35 PRs have Go tests, 46 have TS tests, 2 have both
- Base image: golang:latest (includes Go toolchain on Debian Bookworm)
- Node managed via NVM — reads .nvmrc at each commit automatically
- Package manager: yarn (yarn.lock present throughout all eras)
- Single config covers all eras (NVM + golang:latest handle version drift)
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GrafanaImageBase(Image):
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
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash

{code}

{self.clear_env}

"""


class GrafanaImageDefault(Image):
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
        return GrafanaImageBase(self.pr, self._config)

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
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install Node version specified by .nvmrc (or latest LTS as fallback)
nvm install 2>/dev/null || nvm install --lts || true
nvm use 2>/dev/null || true

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
set -e

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
nvm use 2>/dev/null || true

# Run Go tests if Go source files are present in the repo
if ls pkg/ > /dev/null 2>&1; then
    go test -v -count=1 ./pkg/... 2>&1 || true
fi

# Run TypeScript/Jest tests
CI=true yarn test:ci 2>&1 || CI=true yarn test --watchAll=false 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
nvm use 2>/dev/null || true

# Reinstall deps in case test patch added new packages
yarn install --frozen-lockfile 2>/dev/null || yarn install || true

# Run Go tests if Go source files are present
if ls pkg/ > /dev/null 2>&1; then
    go test -v -count=1 ./pkg/... 2>&1 || true
fi

# Run TypeScript/Jest tests
CI=true yarn test:ci 2>&1 || CI=true yarn test --watchAll=false 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
nvm use 2>/dev/null || true

# Reinstall deps in case patches added new packages
yarn install --frozen-lockfile 2>/dev/null || yarn install || true

# Run Go tests if Go source files are present
if ls pkg/ > /dev/null 2>&1; then
    go test -v -count=1 ./pkg/... 2>&1 || true
fi

# Run TypeScript/Jest tests
CI=true yarn test:ci 2>&1 || CI=true yarn test --watchAll=false 2>&1 || true

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


@Instance.register("grafana", "grafana")
class Grafana(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaImageDefault(self.pr, self._config)

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
        """Parse test output from both Go tests and Jest (TypeScript) tests.

        Go test output format:
            --- PASS: TestName (0.00s)
            --- FAIL: TestName (0.01s)
            --- SKIP: TestName (0.00s)

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

        # Go test patterns
        re_go_pass = re.compile(r"^--- PASS: (\S+)")
        re_go_fail = re.compile(r"^--- FAIL: (\S+)")
        re_go_skip = re.compile(r"^--- SKIP: (\S+)")

        # Jest suite-level patterns (PASS/FAIL <file>)
        re_jest_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        # Jest individual test patterns (✓ / ✕ / ○)
        re_jest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_skip = re.compile(r"^\s*[○⊘]\s+(.+)")

        current_suite = ""
        has_individual_tests = False

        def get_base_name(name: str) -> str:
            """Strip subtest suffix for Go tests (TestFoo/SubTest -> TestFoo)."""
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # --- Go test output ---
            m = re_go_pass.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_go_fail.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_go_skip.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

            # --- Jest suite-level (PASS/FAIL <file>) ---
            m = re_jest_suite.match(line)
            if m:
                current_suite = m.group(2)
                # Only add suite-level if no individual tests found yet
                if not has_individual_tests:
                    if m.group(1) == "PASS":
                        failed_tests.discard(current_suite)
                        skipped_tests.discard(current_suite)
                        passed_tests.add(current_suite)
                    else:
                        failed_tests.discard(current_suite)
                        passed_tests.discard(current_suite)
                        failed_tests.add(current_suite)
                continue

            # --- Jest individual test lines ---
            m = re_jest_pass.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                # Remove suite-level entry now that we have individual tests
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
