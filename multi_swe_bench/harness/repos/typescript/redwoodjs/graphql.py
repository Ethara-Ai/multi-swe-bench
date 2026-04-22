import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip jest file-level metadata with optional trailing bare timing:
    #   (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class RedwoodjsGraphqlImageBase(Image):

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
        return "node:18-bullseye"

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
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y python3 && \
    dpkg --add-architecture amd64 && apt-get update && \
    apt-get install -y libc6:amd64 libssl1.1:amd64 zlib1g:amd64 && \
    rm -rf /var/lib/apt/lists/*
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \
    export NVM_DIR="$HOME/.nvm" && \
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
{code}

{self.clear_env}

"""


class RedwoodjsGraphqlImageDefault(Image):

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
        return RedwoodjsGraphqlImageBase(self.pr, self.config)

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
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Fix deprecated git:// protocol (blocked by GitHub since 2021)
git config --global url."https://".insteadOf git://

# Detect yarn version and set Node version accordingly
# yarn berry 3.0.2 has ERR_STREAM_PREMATURE_CLOSE on Node 18 — use Node 16
if [ -f .yarnrc.yml ]; then
  echo ">>> Detected yarn berry (.yarnrc.yml found) — using Node 16"
  nvm install 16 || true
  nvm use 16 || true
else
  echo ">>> Detected yarn classic (v1) — using Node 18"
  nvm install 18 || true
  nvm use 18 || true
fi
corepack enable || true

# Ensure node_modules/.bin is on PATH (for lerna, ttsc, etc.)
export PATH="$(pwd)/node_modules/.bin:$PATH"

# Install dependencies
if [ -f .yarnrc.yml ]; then
  export YARN_ENABLE_IMMUTABLE_INSTALLS=false
  export YARN_HTTP_TIMEOUT=120000
  export YARN_NETWORK_CONCURRENCY=4
  # Retry yarn install up to 3 times (yarn berry fetch can fail intermittently)
  for attempt in 1 2 3; do
    echo ">>> yarn install attempt $attempt"
    if yarn install; then
      echo ">>> yarn install succeeded on attempt $attempt"
      break
    fi
    echo ">>> yarn install failed on attempt $attempt, retrying..."
    sleep 5
  done || true
else
  yarn install --ignore-engines || true
fi

# Build all workspaces (some may fail under constrained environments)
yarn build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}

# Fix deprecated git:// protocol
git config --global url."https://".insteadOf git://

if [ -f .yarnrc.yml ]; then
  nvm use 16 || true
  export YARN_ENABLE_IMMUTABLE_INSTALLS=false
else
  nvm use 18 || true
fi
export PATH="$(pwd)/node_modules/.bin:$PATH"

yarn test --verbose || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}

# Fix deprecated git:// protocol
git config --global url."https://".insteadOf git://

git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch
if git diff --name-only HEAD | grep -q 'package\\.json' || git ls-files --others --exclude-standard | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  export PATH="$(pwd)/node_modules/.bin:$PATH"
  if [ -f .yarnrc.yml ]; then
    export YARN_ENABLE_IMMUTABLE_INSTALLS=false
    export YARN_HTTP_TIMEOUT=120000
    export YARN_NETWORK_CONCURRENCY=4
    for attempt in 1 2 3; do
      echo ">>> yarn install attempt $attempt"
      if yarn install; then break; fi
      echo ">>> yarn install failed on attempt $attempt, retrying..."
      sleep 5
    done
  else
    yarn install --ignore-engines || true
  fi
  yarn build || true
fi
if [ -f .yarnrc.yml ]; then
  nvm use 16 || true
else
  nvm use 18 || true
fi
export PATH="$(pwd)/node_modules/.bin:$PATH"
yarn test --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}

# Fix deprecated git:// protocol
git config --global url."https://".insteadOf git://

git apply --whitespace=nowarn --exclude yarn.lock /home/test.patch /home/fix.patch
if git diff --name-only HEAD | grep -q 'package\\.json' || git ls-files --others --exclude-standard | grep -q 'package\\.json'; then
  echo ">>> package.json changed by patch, running yarn install && yarn build"
  export PATH="$(pwd)/node_modules/.bin:$PATH"
  if [ -f .yarnrc.yml ]; then
    export YARN_ENABLE_IMMUTABLE_INSTALLS=false
    export YARN_HTTP_TIMEOUT=120000
    export YARN_NETWORK_CONCURRENCY=4
    for attempt in 1 2 3; do
      echo ">>> yarn install attempt $attempt"
      if yarn install; then break; fi
      echo ">>> yarn install failed on attempt $attempt, retrying..."
      sleep 5
    done
  else
    yarn install --ignore-engines || true
  fi
  yarn build || true
fi
if [ -f .yarnrc.yml ]; then
  nvm use 16 || true
else
  nvm use 18 || true
fi
export PATH="$(pwd)/node_modules/.bin:$PATH"
yarn test --verbose || true

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("redwoodjs", "graphql")
class RedwoodjsGraphql(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RedwoodjsGraphqlImageDefault(self.pr, self._config)

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

    _NEEDS_AMD64 = set()

    def run_platform(self) -> str | None:
        if self._pr.number in self._NEEDS_AMD64:
            return "linux/amd64"
        return None

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape sequences
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()

            # Strip monorepo workspace prefix:
            # @redwoodjs/cli: PASS ... → PASS ...
            # @redwoodjs/internal:   ✓ test name → ✓ test name
            stripped = re.sub(r"^@[\w\-/.]+:\s*", "", stripped)

            # Jest file-level PASS/FAIL
            if stripped.startswith("PASS "):
                passed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue
            if stripped.startswith("FAIL "):
                failed_tests.add(_clean_test_name(stripped[5:].strip()))
                continue

            # Test-level pass (✓/✔)
            m = re.match(
                r"\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                passed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Test-level fail (×/✕/✗)
            m = re.match(
                r"\s*[×✕✗]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                failed_tests.add(_clean_test_name(m.group(1)))
                continue

            # Skipped (○)
            m = re.match(
                r"\s*○\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$", stripped
            )
            if m:
                skipped_tests.add(_clean_test_name(m.group(1)))
                continue

        # A file that FAILed should not also appear in passed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
