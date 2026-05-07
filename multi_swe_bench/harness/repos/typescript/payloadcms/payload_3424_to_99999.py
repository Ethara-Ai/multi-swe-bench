import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .payload import (
    payload_parse_log,
    _CHECK_GIT_CHANGES_SH,
    _STRIP_BINARY_DIFFS_PY,
    _START_MONGO_SH,
)

_INTERVAL_NAME = "payload_3424_to_99999"


class PayloadV2ImageBase(Image):

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
        return "node:18"

    def image_tag(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def workdir(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN npm install -g pnpm
RUN apt-get update && \\
    apt-get install -y gnupg curl wget ca-certificates lsb-release && \\
    wget -qO - https://pgp.mongodb.com/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg && \\
    echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \\
    > /etc/apt/sources.list.d/mongodb-org-7.0.list && \\
    apt-get update && \\
    apt-get install -y mongodb-org && \\
    mkdir -p /data/db && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \\
    export NVM_DIR="$HOME/.nvm" && \\
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
RUN apt-get update && apt-get install -y libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \\
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \\
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libx11-xcb1 && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class PayloadV2ImageDefault(Image):

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
        return PayloadV2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "strip_binary_diffs.py", _STRIP_BINARY_DIFFS_PY),
            File(".", "start-mongo.sh", _START_MONGO_SH),
            File(
                ".",
                "run-e2e.sh",
                """#!/bin/bash
# Run only e2e specs that the test patch touches, each in isolation
# Mimics runE2E.ts: starts dev server per suite, then runs Playwright

cd /home/{repo}

# Extract e2e spec files from the test patch
E2E_SPECS=$(grep -E '^diff --git a/(.*e2e\\.spec\\.ts)' /home/test.patch | sed 's|diff --git a/||;s| b/.*||' || true)

if [ -z "$E2E_SPECS" ]; then
  echo "No e2e specs found in test patch, skipping e2e tests"
  exit 0
fi

export CI=true
export START_MEMORY_DB=true
export DISABLE_LOGGING=true
export PAYLOAD_DO_NOT_SANITIZE_LOCALIZED_PROPERTY=true

echo "=== Running targeted e2e tests ==="
for spec in $E2E_SPECS; do
  if [ ! -f "$spec" ]; then
    echo "--- Spec not found (not yet created): $spec ---"
    continue
  fi
  echo "--- Running e2e spec: $spec ---"

  # Extract suite name from path: test/bulk-edit/e2e.spec.ts -> bulk-edit
  SUITE_NAME=$(echo "$spec" | sed 's|^test/||' | cut -d'/' -f1)
  echo "Suite: $SUITE_NAME"

  # Kill any leftover dev servers
  pkill -f "next dev" 2>/dev/null || true
  pkill -f "test/dev.ts" 2>/dev/null || true
  sleep 2

  # Clear webpack cache
  rm -rf node_modules/.cache/webpack

  # Start the dev server in background (same as runE2E.ts)
  pnpm dev "$SUITE_NAME" --start-memory-db &
  DEV_PID=$!

  # Wait for dev server to be ready (poll localhost:3000)
  echo "Waiting for dev server on port 3000..."
  for i in $(seq 1 120); do
    if curl -s -o /dev/null -w '' http://localhost:3000/admin 2>/dev/null; then
      echo "Dev server ready after ${{i}}s"
      break
    fi
    if ! kill -0 $DEV_PID 2>/dev/null; then
      echo "Dev server process died"
      break
    fi
    sleep 1
  done

  # Run Playwright against the specific spec
  pnpm exec playwright test "$spec" -c test/playwright.config.ts 2>&1 || true
  echo "--- Done: $spec ---"

  # Kill dev server
  kill $DEV_PID 2>/dev/null || true
  pkill -f "next dev" 2>/dev/null || true
  sleep 2
done

echo "=== E2E test run complete ==="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

nvm install || true
nvm use || true

# Use corepack to match the repo's expected pnpm version
corepack enable 2>/dev/null || true
if grep -q '"packageManager"' package.json 2>/dev/null; then
  corepack prepare --activate 2>/dev/null || true
fi

pnpm install || true

# Rebuild sharp for current architecture (ARM64 support)
pnpm rebuild sharp 2>/dev/null || npm rebuild sharp 2>/dev/null || true

pnpm exec playwright install chromium 2>/dev/null || true

pnpm build || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}
nvm use || true
corepack enable 2>/dev/null || true

export DISABLE_LOGGING=true
NODE_MAJOR=$(node -v | cut -d. -f1 | tr -d 'v')
if [ "$NODE_MAJOR" -ge 22 ]; then
  export NODE_OPTIONS="--no-deprecation --no-experimental-strip-types"
else
  export NODE_OPTIONS="--no-deprecation"
fi
export NODE_NO_WARNINGS=1

pnpm test:unit || true
pnpm test:int || true
bash /home/run-e2e.sh || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}

python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

nvm use || true
corepack enable 2>/dev/null || true

# Only rebuild if patch touches source files (not just test/ configs)
if git diff --name-only HEAD | grep -qE '^packages/.*/(src|dist)/|^src/|package\.json|tsconfig'; then
  pnpm install || true
  pnpm build || true
fi

export DISABLE_LOGGING=true
NODE_MAJOR=$(node -v | cut -d. -f1 | tr -d 'v')
if [ "$NODE_MAJOR" -ge 22 ]; then
  export NODE_OPTIONS="--no-deprecation --no-experimental-strip-types"
else
  export NODE_OPTIONS="--no-deprecation"
fi
export NODE_NO_WARNINGS=1

pnpm test:unit || true
pnpm test:int || true
bash /home/run-e2e.sh || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

bash /home/start-mongo.sh || true

cd /home/{repo}

python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}

nvm use || true
corepack enable 2>/dev/null || true

# Only rebuild if patches touch source files (not just test/ configs)
if git diff --name-only HEAD | grep -qE '^packages/.*/(src|dist)/|^src/|package\.json|tsconfig'; then
  pnpm install || true
  pnpm build || true
fi

export DISABLE_LOGGING=true
NODE_MAJOR=$(node -v | cut -d. -f1 | tr -d 'v')
if [ "$NODE_MAJOR" -ge 22 ]; then
  export NODE_OPTIONS="--no-deprecation --no-experimental-strip-types"
else
  export NODE_OPTIONS="--no-deprecation"
fi
export NODE_NO_WARNINGS=1

pnpm test:unit || true
pnpm test:int || true
bash /home/run-e2e.sh || true

echo "=== Test run complete ==="
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_3424_TO_99999(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PayloadV2ImageDefault(self.pr, self._config)

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
        return payload_parse_log(test_log)
