import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class keyshadeImageBase(Image):
    """Base image: node 20 + pnpm + postgresql 15 + redis."""

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
        return "node:20"

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
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo} && "
                f"cd /home/{self.pr.repo} && "
                f"git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*/head' '+refs/pull/*/merge:refs/remotes/origin/pr/*/merge'"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git postgresql postgresql-client redis-server curl \\
    && rm -rf /var/lib/apt/lists/*

# Configure PostgreSQL for trust auth (no password needed)
RUN sed -i 's/peer/trust/g' /etc/postgresql/15/main/pg_hba.conf && \\
    sed -i 's/scram-sha-256/trust/g' /etc/postgresql/15/main/pg_hba.conf

RUN npm install -g pnpm
{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{self.clear_env}

CMD ["/bin/bash"]
"""


class keyshadeImageDefault(Image):
    """PR-specific image: checkout base, install deps, build all packages."""

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
        return keyshadeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        apply_opts = '--whitespace=nowarn --exclude="pnpm-lock.yaml"'

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

pnpm install --frozen-lockfile || pnpm install || true
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "start_services.sh",
                """\
#!/bin/bash
# Start PostgreSQL and Redis, create test database, deploy migrations.
# Sourced by run/test-run/fix-run scripts.

set -e

export DATABASE_URL="postgresql://prisma:prisma@localhost:5432/tests"
export REDIS_URL="redis://localhost:6379"
export JWT_SECRET="secret"
export NODE_ENV="e2e"
export NODE_OPTIONS="--max-old-space-size=4096"

# Start PostgreSQL
pg_ctlcluster 15 main start || true
sleep 1

# Create user and database matching docker-compose-test.yml
su - postgres -c "psql -c \\"CREATE USER prisma WITH PASSWORD 'prisma' SUPERUSER;\\"" 2>/dev/null || true
su - postgres -c "psql -c \\"CREATE DATABASE tests OWNER prisma;\\"" 2>/dev/null || true

# Start Redis
redis-server --daemonize yes || true

cd /home/{repo}

# Use project-local prisma (apps/api/node_modules/.bin/prisma) — npx/pnpm dlx download latest Prisma 7 which breaks old schemas
PRISMA_BIN="./apps/api/node_modules/.bin/prisma"
if [ ! -f "$PRISMA_BIN" ]; then
  PRISMA_BIN="$(find /home/{repo}/node_modules -name prisma -path '*/node_modules/.bin/prisma' -print -quit 2>/dev/null)"
fi
export PRISMA_BIN
$PRISMA_BIN generate --schema=apps/api/src/prisma/schema.prisma || true
DATABASE_URL="$DATABASE_URL" $PRISMA_BIN migrate deploy --schema=apps/api/src/prisma/schema.prisma || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run_tests.sh",
                """\
#!/bin/bash
# Run targeted tests for all testable packages.
# PostgreSQL, Redis, and migrations must already be running (via start_services.sh).

set -e
cd /home/{repo}

export DATABASE_URL="postgresql://prisma:prisma@localhost:5432/tests"
export REDIS_URL="redis://localhost:6379"
export JWT_SECRET="secret"
export NODE_ENV="e2e"
export BACKEND_URL="http://localhost:4200"

echo "===== Building all packages ====="
pnpm run build || true

echo "===== Running secret-scan tests ====="
pnpm run --filter @keyshade/secret-scan test || true

echo "===== Running schema tests ====="
pnpm run --filter @keyshade/schema test || true

echo "===== Running API e2e tests ====="
# Run e2e tests directly with jest (skip e2e:prepare which uses docker compose)
cd apps/api
npx jest --runInBand --config=jest.e2e-config.ts --forceExit || true
cd /home/{repo}

echo "===== Resetting database for api-client tests ====="
su - postgres -c "psql -c 'DROP DATABASE IF EXISTS tests;'" 2>/dev/null || true
su - postgres -c "psql -c 'CREATE DATABASE tests OWNER prisma;'" 2>/dev/null || true
DATABASE_URL="$DATABASE_URL" $PRISMA_BIN migrate deploy --schema=apps/api/src/prisma/schema.prisma || true

echo "===== Starting API server for api-client tests ====="
cd apps/api
node dist/main &
API_PID=$!
cd /home/{repo}

for i in $(seq 1 30); do
  if curl -s http://localhost:4200/api/health > /dev/null 2>&1; then
    echo "API server ready after ${{i}}s"
    break
  fi
  sleep 1
done

echo "===== Running api-client tests ====="
cd packages/api-client
BACKEND_URL="http://localhost:4200" npx jest --runInBand --globalSetup= --globalTeardown= || true
cd /home/{repo}

kill $API_PID 2>/dev/null || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

source /home/start_services.sh
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply {apply_opts} /home/test.patch || \
  (git reset --hard && git apply {apply_opts} --3way /home/test.patch) || true

pnpm install --frozen-lockfile || pnpm install || true

source /home/start_services.sh
bash /home/run_tests.sh
""".format(
                    repo=self.pr.repo,
                    apply_opts=apply_opts,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply {apply_opts} /home/test.patch /home/fix.patch || \
  (git reset --hard && git apply {apply_opts} --3way /home/test.patch /home/fix.patch) || true

pnpm install --frozen-lockfile || pnpm install || true

source /home/start_services.sh
bash /home/run_tests.sh
""".format(
                    repo=self.pr.repo,
                    apply_opts=apply_opts,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

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

RUN bash /home/prepare.sh

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("keyshade-xyz", "keyshade")
class Keyshade(Instance):
    """keyshade-xyz/keyshade: pnpm + turbo monorepo, Jest tests (e2e + unit)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return keyshadeImageDefault(self.pr, self._config)

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

        re_suite_pass = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*m?s\))?\s*$")
        re_suite_fail = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*m?s\))?\s*$")

        re_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_skip = re.compile(
            r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )

        re_summary = re.compile(
            r"Tests:\s+"
            r"(?:(\d+)\s+passed)?"
            r"(?:,?\s*(\d+)\s+failed)?"
            r"(?:,?\s*(\d+)\s+skipped)?"
            r"(?:,?\s*(\d+)\s+todo)?"
            r"(?:,?\s*(\d+)\s+total)?"
        )

        current_suite = ""
        total_summary_passed = 0
        total_summary_failed = 0
        total_summary_skipped = 0

        for line in test_log.splitlines():
            clean = ansi_escape.sub("", line)

            m = re_suite_pass.match(clean)
            if m:
                suite_path = m.group(1).strip()
                if suite_path.endswith((".ts", ".js", ".tsx", ".jsx")):
                    current_suite = suite_path
                    passed_tests.add(f"SUITE:{suite_path}")
                continue

            m = re_suite_fail.match(clean)
            if m:
                suite_path = m.group(1).strip()
                if suite_path.endswith((".ts", ".js", ".tsx", ".jsx")):
                    current_suite = suite_path
                    failed_tests.add(f"SUITE:{suite_path}")
                    passed_tests.discard(f"SUITE:{suite_path}")
                continue

            m = re_summary.search(clean)
            if m and m.group(5):
                total_summary_passed += int(m.group(1) or 0)
                total_summary_failed += int(m.group(2) or 0)
                total_summary_skipped += int(m.group(3) or 0)
                continue

            m = re_pass.match(clean)
            if m:
                test_name = m.group(1).strip()
                if "Prisma Client" in test_name or "Generated" in test_name:
                    continue
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                passed_tests.add(test_name)
                continue

            m = re_fail.match(clean)
            if m:
                test_name = m.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue

            m = re_skip.match(clean)
            if m:
                test_name = m.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                skipped_tests.add(test_name)
                continue

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
