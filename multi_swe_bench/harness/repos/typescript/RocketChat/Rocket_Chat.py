import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RocketChatImageBase(Image):
    """Base image for Rocket.Chat Era 2 (node 20-22, yarn 4, Meteor 3)."""

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
RUN apt-get update && apt-get install -y git python3 make g++ gnupg curl

# Install MongoDB 7.0
RUN curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/mongodb-7.0.gpg && \\
    echo 'deb [ arch=amd64 ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main' > /etc/apt/sources.list.d/mongodb-org-7.0.list && \\
    echo 'deb [ arch=arm64 ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse' >> /etc/apt/sources.list.d/mongodb-org-7.0.list && \\
    apt-get update && apt-get install -y mongodb-org
RUN mkdir -p /data/db

# Install deno
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

# Install Meteor
RUN curl https://install.meteor.com | sh

# Setup corepack and turbo
RUN corepack enable
RUN npm install -g turbo

{code}

{self.clear_env}

"""


class RocketChatImageDefault(Image):
    """Per-PR image for Rocket.Chat Era 2."""

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
        return RocketChatImageBase(self.pr, self.config)

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

# Strip plugin-engines to avoid strict node version check
sed -i '/plugin-engines/d' .yarnrc.yml || true

# Install dependencies
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
corepack prepare --activate || true
yarn install || true

# Build all packages
yarn turbo run build --concurrency=4 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Start MongoDB
mongod --dbpath /data/db --replSet rs0 --bind_ip_all --fork --logpath /var/log/mongod.log
sleep 2
mongosh --eval 'rs.initiate({{_id:"rs0",members:[{{_id:0,host:"localhost:27017"}}]}})' || true
sleep 2

# Build and start Rocket.Chat server
cd apps/meteor
export MONGO_URL="mongodb://localhost:27017/rocketchat?replicaSet=rs0&directConnection=true"
export MONGO_OPLOG_URL="mongodb://localhost:27017/local?replicaSet=rs0&directConnection=true"
export ROOT_URL="http://localhost:3000"
export PORT=3000
export NODE_ENV=test

meteor build --server-only --directory /tmp/dist 2>/dev/null || true
if [ -d /tmp/dist/bundle ]; then
  cd /tmp/dist/bundle/programs/server && npm install --omit=dev 2>/dev/null && cd /tmp/dist/bundle
  node main.js &
  SERVER_PID=$!
  # Wait for server
  for i in $(seq 1 120); do
    if curl -sf http://localhost:3000/health > /dev/null 2>&1; then break; fi
    sleep 2
  done
fi

# Run unit tests (set +e to continue to API tests even if unit tests fail)
cd /home/{pr.repo}
set +e
yarn turbo run testunit --concurrency=1 2>&1
UNIT_EXIT=$?
set -e

# Run API tests if server is up
if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
  cd apps/meteor
  set +e
  NODE_ENV=test yarn testapi 2>&1
  API_EXIT=$?
  set -e
fi

# Cleanup
[ -n "$SERVER_PID" ] && kill $SERVER_PID 2>/dev/null || true

# Exit with failure if any test suite failed
exit ${{UNIT_EXIT:-0}}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Rebuild after patch
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install || true
yarn turbo run build --concurrency=4 || true

# Start MongoDB
mongod --dbpath /data/db --replSet rs0 --bind_ip_all --fork --logpath /var/log/mongod.log
sleep 2
mongosh --eval 'rs.initiate({{_id:"rs0",members:[{{_id:0,host:"localhost:27017"}}]}})' || true
sleep 2

# Build and start Rocket.Chat server
cd apps/meteor
export MONGO_URL="mongodb://localhost:27017/rocketchat?replicaSet=rs0&directConnection=true"
export MONGO_OPLOG_URL="mongodb://localhost:27017/local?replicaSet=rs0&directConnection=true"
export ROOT_URL="http://localhost:3000"
export PORT=3000
export NODE_ENV=test

meteor build --server-only --directory /tmp/dist 2>/dev/null || true
if [ -d /tmp/dist/bundle ]; then
  cd /tmp/dist/bundle/programs/server && npm install --omit=dev 2>/dev/null && cd /tmp/dist/bundle
  node main.js &
  SERVER_PID=$!
  for i in $(seq 1 120); do
    if curl -sf http://localhost:3000/health > /dev/null 2>&1; then break; fi
    sleep 2
  done
fi

cd /home/{pr.repo}
set +e
yarn turbo run testunit --concurrency=1 2>&1
UNIT_EXIT=$?
set -e

if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
  cd apps/meteor
  set +e
  NODE_ENV=test yarn testapi 2>&1
  API_EXIT=$?
  set -e
fi

[ -n "$SERVER_PID" ] && kill $SERVER_PID 2>/dev/null || true
exit ${{UNIT_EXIT:-0}}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Rebuild after patches
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install || true
yarn turbo run build --concurrency=4 || true

# Start MongoDB
mongod --dbpath /data/db --replSet rs0 --bind_ip_all --fork --logpath /var/log/mongod.log
sleep 2
mongosh --eval 'rs.initiate({{_id:"rs0",members:[{{_id:0,host:"localhost:27017"}}]}})' || true
sleep 2

# Build and start Rocket.Chat server
cd apps/meteor
export MONGO_URL="mongodb://localhost:27017/rocketchat?replicaSet=rs0&directConnection=true"
export MONGO_OPLOG_URL="mongodb://localhost:27017/local?replicaSet=rs0&directConnection=true"
export ROOT_URL="http://localhost:3000"
export PORT=3000
export NODE_ENV=test

meteor build --server-only --directory /tmp/dist 2>/dev/null || true
if [ -d /tmp/dist/bundle ]; then
  cd /tmp/dist/bundle/programs/server && npm install --omit=dev 2>/dev/null && cd /tmp/dist/bundle
  node main.js &
  SERVER_PID=$!
  for i in $(seq 1 120); do
    if curl -sf http://localhost:3000/health > /dev/null 2>&1; then break; fi
    sleep 2
  done
fi

cd /home/{pr.repo}
set +e
yarn turbo run testunit --concurrency=1 2>&1
UNIT_EXIT=$?
set -e

if curl -sf http://localhost:3000/health > /dev/null 2>&1; then
  cd apps/meteor
  set +e
  NODE_ENV=test yarn testapi 2>&1
  API_EXIT=$?
  set -e
fi

[ -n "$SERVER_PID" ] && kill $SERVER_PID 2>/dev/null || true
exit ${{UNIT_EXIT:-0}}

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


@Instance.register("RocketChat", "Rocket.Chat")
class RocketChat(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocketChatImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_re.sub("", test_log)

        re_jest_pass = re.compile(
            r"^\s*PASS\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))", re.MULTILINE
        )
        re_jest_fail = re.compile(
            r"^\s*FAIL\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))", re.MULTILINE
        )

        re_mocha_pass = re.compile(r"[✓✔]\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?\s*$")
        re_mocha_fail_numbered = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
        re_mocha_skip = re.compile(r"^\s*-\s+(.+?)\s*$")

        in_failure_section = False
        suite_stack: list[str] = []
        prev_indent = 0

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            turbo_match = re.match(r"^(@[\w/.:-]+:\w+:\s*)", line)
            if turbo_match:
                after_prefix = line[turbo_match.end() :]
            else:
                after_prefix = line

            turbo_stripped = stripped
            if turbo_match:
                turbo_stripped = re.sub(r"^@[\w/.:-]+:\w+:\s*", "", stripped)

            m = re_jest_pass.search(turbo_stripped)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_jest_fail.search(turbo_stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

            # Mocha indentation-based suite tracking
            raw_for_indent = after_prefix.rstrip()
            indent = len(raw_for_indent) - len(raw_for_indent.lstrip())

            m = re_mocha_pass.search(turbo_stripped)
            if m:
                test_name = m.group(1).strip()
                if test_name and len(test_name) > 2:
                    prefix = " > ".join(suite_stack) + " > " if suite_stack else ""
                    passed_tests.add(prefix + test_name)
                continue

            if re.match(r"^\s*\d+\s+failing", turbo_stripped):
                in_failure_section = True
                continue

            if re.match(r"^\s*\d+\s+passing", turbo_stripped):
                in_failure_section = False
                continue

            if in_failure_section:
                m = re_mocha_fail_numbered.match(turbo_stripped)
                if m:
                    test_name = m.group(1).strip()
                    if test_name:
                        failed_tests.add(test_name)
                    continue

            m = re_mocha_skip.match(turbo_stripped)
            if m:
                test_name = m.group(1).strip()
                if test_name and len(test_name) > 2:
                    prefix = " > ".join(suite_stack) + " > " if suite_stack else ""
                    skipped_tests.add(prefix + test_name)
                continue

            # Suite header detection: non-empty line, not a test result, not a summary
            if (
                turbo_stripped
                and not re.match(r"^\s*\d+\s+(passing|failing|pending)", turbo_stripped)
                and not re.match(r"^\s*$", turbo_stripped)
                and "✓" not in turbo_stripped
                and "✔" not in turbo_stripped
                and not re.match(r"^\s*\d+\)", turbo_stripped)
                and not re.match(r"^\s*-\s+", turbo_stripped)
                and not turbo_stripped.startswith("PASS ")
                and not turbo_stripped.startswith("FAIL ")
                and not turbo_stripped.startswith("Test Suites:")
                and not turbo_stripped.startswith("Tests:")
                and not turbo_stripped.startswith("Time:")
                and not turbo_stripped.startswith("Snapshots:")
                and not turbo_stripped.startswith("Ran all")
                and indent >= 0
            ):
                # Adjust suite stack based on indent
                while len(suite_stack) > 0 and indent <= prev_indent - 2:
                    suite_stack.pop()
                    prev_indent -= 2
                if indent >= prev_indent:
                    suite_stack.append(turbo_stripped)
                    prev_indent = indent + 2

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
