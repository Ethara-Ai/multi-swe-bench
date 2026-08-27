"""Repo config for keyshade-xyz/keyshade (TypeScript / pnpm + turbo monorepo).

PR #370 is contained entirely in ``packages/api-client``. The test patch moves
two spec files onto the ``@api-client/*`` import alias; the fix patch adds the
mapping for it (``jest.config.ts`` moduleNameMapper, ``tsconfig.json`` and
``packages/tsconfig/base.json`` paths) alongside the new controllers. So in the
test stage both spec files die at module resolution -- *Test suite failed to
run* -- and Jest prints no per-test markers for them. ``parse_log`` therefore
also emits one entry per suite file, which is what carries the
``!PASS -> PASS`` transition ``Report.check()`` rule 3 requires.

Names are repo-root-relative because ``Report._touched_by_test_patch`` matches
them against repo-root-relative patch paths, while Jest prints paths relative
to ``rootDir``. The run scripts announce the package and ``parse_log`` prefixes
it back on.

Only ``packages/api-client`` is run. ``apps/api``'s e2e suite cannot contribute
to the f2p signal -- the fix patch does not touch it -- but any of its hundreds
of shared-database tests flaking PASS->FAIL between the test and fix stages
would trip rule 2 and void the instance.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PACKAGE_DIR = "packages/api-client"

# Shared verbatim by run.sh / test-run.sh / fix-run.sh, so the only difference
# between the graded stages is which patch was applied before it runs.
_TEST_BODY = """\
export CI=true
export NODE_ENV=e2e
export DATABASE_URL="postgresql://prisma:prisma@localhost:5432/tests"
export REDIS_URL="redis://localhost:6379"
export JWT_SECRET="secret"
export BACKEND_URL="http://localhost:4200"
export NODE_OPTIONS="--max-old-space-size=4096"

# `|| true` on the daemon starts only; the readiness probes below are the real
# assertions and are not forgiving.
pg_ctlcluster 15 main start || true
for _ in $(seq 1 60); do
    pg_isready -h 127.0.0.1 -p 5432 > /dev/null 2>&1 && break
    sleep 1
done
pg_isready -h 127.0.0.1 -p 5432

redis-server --daemonize yes --save '' --appendonly no || true
for _ in $(seq 1 30); do
    redis-cli ping > /dev/null 2>&1 && break
    sleep 1
done
redis-cli ping

su postgres -c "psql -tAc \\"SELECT 1 FROM pg_roles WHERE rolname='prisma'\\"" \\
    | grep -q 1 \\
    || su postgres -c "psql -c \\"CREATE USER prisma WITH PASSWORD 'prisma' SUPERUSER;\\""

# Rebuilt every stage: without it the fix stage would inherit the rows the test
# stage left behind and the comparison would be between two databases rather
# than between two patch sets.
su postgres -c "psql -c 'DROP DATABASE IF EXISTS tests;'"
su postgres -c "psql -c 'CREATE DATABASE tests OWNER prisma;'"

# Project-local prisma, never `pnpm dlx prisma`: the repo pins 5.13.0 and dlx
# would fetch the current major against a 5.x schema.
cd /home/{repo}/apps/api
./node_modules/.bin/prisma migrate deploy --schema=src/prisma/schema.prisma

# packages/api-client is an HTTP client; every spec needs a live backend.
rm -f /tmp/api.log
node dist/main > /tmp/api.log 2>&1 &
API_PID=$!

API_UP=0
for _ in $(seq 1 180); do
    if curl -sf http://localhost:4200/api/health > /dev/null 2>&1; then
        API_UP=1
        break
    fi
    kill -0 "$API_PID" 2>/dev/null || break
    sleep 1
done

if [ "$API_UP" -ne 1 ]; then
    echo "FATAL: API server never answered /api/health on :4200"
    tail -n 200 /tmp/api.log || true
    kill "$API_PID" 2>/dev/null || true
    exit 1
fi

# Run from inside the package, the same code path as `pnpm run --filter=
# api-client test`. This is what makes the fix patch's edit to jest.config.ts
# the config actually in force; a hand-written config would discard the very
# change under test.
cd /home/{repo}/{package_dir}
echo "===== PACKAGE: {package_dir} ====="

# Displaces tests/config/{{setup,teardown}}.ts, which shell out to
# `docker compose` and call `process.exit(0)`.
set +e
pnpm exec jest \\
    --runInBand \\
    --ci \\
    --verbose \\
    --forceExit \\
    --globalSetup /home/noop-global.cjs \\
    --globalTeardown /home/noop-global.cjs \\
    > /tmp/jest.out 2>&1
JEST_RC=$?
set -e

kill "$API_PID" 2>/dev/null || true
wait "$API_PID" 2>/dev/null || true

cat /tmp/jest.out
echo "jest exit code: ${{JEST_RC}}"

# A non-zero JEST_RC is the honest outcome of a stage with failing tests and
# must not abort it -- the harness grades from the log text. A runner that
# never started is a different thing, and prints no summary line; failing here
# turns that into a loud stage failure instead of a silent 0/0/0.
grep -qE '^(Test Suites|Tests):' /tmp/jest.out
"""


class keyshadeImageBase(Image):
    """node:20-bookworm plus Postgres 15, Redis and pnpm 9.2.0.

    The clone is a compound ``RUN`` and therefore keeps its literal URL:
    ``DockerfileEnhancer._standardize_repo_fetch`` anchors on end-of-line and
    skips it, so ``${REPO_URL}`` is not substituted here. That is deliberate.
    Base commit ``2b54421b`` is reachable from **no branch** -- keyshade
    squash-merges, so the pre-merge history line is gone from ``develop`` and a
    plain clone cannot check it out (measured 2026-08-27: ``git cat-file -t``
    fails on a fresh clone, and ``git for-each-ref --contains`` finds it only
    under ``refs/pull/*/head``). The enhancer's standardised block clones and
    immediately runs ``git checkout ${BASE_COMMIT}`` with no opportunity to
    fetch PR refs in between, so the fetch has to live in the clone itself.
    ``_inject_final_sanitize`` still appends the hardening block before ``CMD``,
    and that block deletes every ref before asserting
    ``rev-list --all == rev-list HEAD``, so the extra PR refs do not survive
    into the image.
    """

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
        # bookworm, not alpine: the Prisma engines are glibc binaries. Debian 12
        # is also what makes the /etc/postgresql/15 path below correct.
        return "node:20-bookworm"

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
            # This PR's head ref first (cheap); fall back to every PR head only
            # if the base commit is still absent.
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo} && \\\n"
                f"    cd /home/{self.pr.repo} && \\\n"
                f"    git fetch --no-tags origin "
                f"'+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr/{self.pr.number}/head' && \\\n"
                f"    (git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || \\\n"
                f"     git fetch --no-tags origin '+refs/pull/*/head:refs/remotes/origin/pr/*/head')"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git postgresql postgresql-client \\
    redis-server redis-tools \\
    && rm -rf /var/lib/apt/lists/*

# The suite reaches Postgres over 127.0.0.1 as `prisma`; peer/scram both break that.
RUN sed -i 's/peer/trust/g' /etc/postgresql/15/main/pg_hba.conf && \\
    sed -i 's/scram-sha-256/trust/g' /etc/postgresql/15/main/pg_hba.conf && \\
    sed -i 's/md5/trust/g' /etc/postgresql/15/main/pg_hba.conf

# The repo's own `packageManager` field. Unpinned, this installs the current
# major against a lockfileVersion 9.0 lockfile.
RUN npm install -g pnpm@9.2.0

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{self.clear_env}

CMD ["/bin/bash"]
"""


class keyshadeImageDefault(Image):
    """Per-PR image: checkout base, install the workspace, compile apps/api."""

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
        body = _TEST_BODY.format(repo=self.pr.repo, package_dir=_PACKAGE_DIR)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "noop-global.cjs",
                """\
// .cjs, not .js: jest.config.ts maps '^.+\\.[tj]s$' through ts-jest and the
// repo's tsconfig has no allowJs, so a .js hook would be handed to ts-jest and
// throw.
module.exports = async () => {};
""",
            ),
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
git clean -fdx
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# `|| true` only here: a native-module compile failure must not abort the
# build. Nothing that runs tests uses it.
pnpm install --frozen-lockfile || pnpm install || true

# Compiled once, at build time: the fix patch does not touch apps/api, so a
# per-stage rebuild could only introduce differences between stages. dist and
# node_modules are gitignored, so this leaves the tree clean for the git apply
# in test-run.sh / fix-run.sh. Not `|| true` -- a broken build fails the image
# here rather than three times as "API server never answered".
cd /home/{repo}/apps/api
./node_modules/.bin/prisma generate --schema=src/prisma/schema.prisma
cd /home/{repo}
pnpm exec turbo run build --filter=api

bash /home/check_git_changes.sh
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
""".format(repo=self.pr.repo)
                + body,
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".format(repo=self.pr.repo)
                + body,
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch + fix.patch failed" >&2
    exit 1
fi
""".format(repo=self.pr.repo)
                + body,
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PKG_MARKER = re.compile(r"^=+\s*PACKAGE:\s*(\S+)\s*=+$")
# `PASS api tests/project.spec.ts (7.4 s)` / `FAIL api tests/project.spec.ts`.
# The `api` between the status and the path is jest.config.ts's
# `displayName`, which the default reporter always renders; the non-greedy
# `(?:\S+\s+)*?` skips it (and any future displayName) without swallowing the
# path itself.
_SUITE_LINE = re.compile(
    r"^\s*(PASS|FAIL)\s+(?:\S+\s+)*?(\S+\.[cm]?[tj]sx?)(?:\s+\(.*\))?\s*$"
)
# Jest's verbose tree; indentation is the only thing carrying describe nesting.
_PASS_LINE = re.compile(r"^(\s+)[✓✔√]\s+(.+?)\s*$")
_FAIL_LINE = re.compile(r"^(\s+)[✕✗×]\s+(.+?)\s*$")
_SKIP_LINE = re.compile(r"^(\s+)[○◌◯✎]\s+(?:skipped|todo)?\s*(.+?)\s*$")
# The failure epilogue repeats `Suite > test` names in a different shape and
# would corrupt the indentation stack if parsed as tree lines.
_DETAIL_LINE = re.compile(r"^\s*●")
# Jest's console relay and stack frames, which would otherwise be pushed onto
# the stack as describe headers.
_NOISE_LINE = re.compile(r"^\s*(?:console\.\w+|at\s)")
_SUMMARY_LINE = re.compile(r"^\s*(?:Test Suites|Tests|Snapshots|Time|Ran all)\b")
# Durations vary run to run; an unstripped one makes the same test a different
# name in each stage, which surfaces as the anomaly rule 4 rejects.
_DURATION = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)\s*$")


def parse_jest_verbose_log(log: str) -> TestResult:
    """Parse ``jest --verbose`` output into repo-root-relative test names.

    Emits one entry per suite file, named by its path -- the only signal
    available when a file fails to compile -- and one per test, named
    ``<path> > <describe...> > <test>``.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    prefix = ""
    current_file: str | None = None
    stack: list[tuple[int, str]] = []
    in_detail = False

    def path_for(indent: int, leaf: str) -> str:
        parts = [current_file] if current_file else []
        parts.extend(name for width, name in stack if width < indent)
        parts.append(leaf)
        return " > ".join(parts)

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).rstrip()

        m = _PKG_MARKER.match(line)
        if m:
            prefix = m.group(1).rstrip("/") + "/"
            current_file, stack, in_detail = None, [], False
            continue

        m = _SUITE_LINE.match(line)
        if m:
            status, path = m.group(1), prefix + m.group(2)
            current_file, stack, in_detail = path, [], False
            if status == "PASS":
                passed_tests.add(path)
            else:
                failed_tests.add(path)
                passed_tests.discard(path)
            continue

        if _SUMMARY_LINE.match(line):
            current_file, stack, in_detail = None, [], False
            continue

        if _DETAIL_LINE.match(line):
            in_detail = True
            continue

        if _NOISE_LINE.match(line):
            current_file, stack, in_detail = None, [], False
            continue

        if in_detail or current_file is None or not line.strip():
            continue

        m = _PASS_LINE.match(line)
        if m:
            passed_tests.add(path_for(len(m.group(1)), _DURATION.sub("", m.group(2))))
            continue

        m = _FAIL_LINE.match(line)
        if m:
            failed_tests.add(path_for(len(m.group(1)), _DURATION.sub("", m.group(2))))
            continue

        m = _SKIP_LINE.match(line)
        if m:
            skipped_tests.add(path_for(len(m.group(1)), _DURATION.sub("", m.group(2))))
            continue

        # Anything else still inside a suite block is a describe header.
        indent = len(line) - len(line.lstrip())
        if indent >= 2:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, line.strip()))

    # TestResult.__post_init__ rejects overlapping sets.
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


@Instance.register("keyshade-xyz", "keyshade")
class Keyshade(Instance):
    """Registered on the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
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

    def parse_log(self, log: str) -> TestResult:
        return parse_jest_verbose_log(log)
