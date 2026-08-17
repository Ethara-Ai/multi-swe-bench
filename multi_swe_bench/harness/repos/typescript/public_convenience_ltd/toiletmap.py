"""public-convenience-ltd/toiletmap harness config — Next.js + Cypress e2e + pnpm.

Unlike a unit-test repo, the graded test here is a **Cypress end-to-end spec**
(``cypress/e2e/desktop/add.cy.ts``), so a stage does not simply invoke a test
runner: it must build the Next.js app, serve it on :3000, and drive a real
Chrome against it.

Pinned from the repo at the PR's base commit:

* node 18.12.1        (``.nvmrc``)
* pnpm 7.15.0         (``packageManager``)
* cypress 11.2.0      (``devDependencies``)

The base image is ``cypress/browsers:node18.12.0-chrome106-ff106`` — the
published tag whose node line matches ``.nvmrc`` and whose Chrome is
contemporary with Cypress 11.

TWO EXTERNAL REQUIREMENTS THIS CONFIG CANNOT SATISFY BY ITSELF
--------------------------------------------------------------
1. **Auth0 credentials.** ``add.cy.ts`` calls ``cy.login()`` in a ``before()``
   hook, which performs a real login against ``gbptm.eu.auth0.com``. Upstream
   CI supplies ``CYPRESS_auth0Username`` / ``CYPRESS_auth0Password`` /
   ``CYPRESS_auth0ClientSecret`` etc. from GitHub Secrets. Without them every
   spec in this file fails at the login hook, before the graded assertion runs.
2. **A database.** The app reads Postgres through Prisma. Upstream CI runs
   ``supabase start``, which spins up its own container stack — not available
   inside a build container. This config instead starts a local Postgres and
   loads ``supabase/seed.sql``, which is the closest single-container
   equivalent.

Both are injected through the environment (``Config.global_env`` or
``docker run -e``); the scripts read them and never hardcode a secret. Supply
the Auth0 values or the instance cannot become valid.
"""

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Markers wrapping the mocha JSON report Cypress emits. Parsing JSON keeps test
# identities stable across the run/test/fix stages, which is what fail-to-pass
# detection depends on.
JSON_START = "###CYPRESS_JSON_START###"
JSON_END = "###CYPRESS_JSON_END###"
JSON_PATH = "/home/cypress-results.json"

PNPM_VERSION = "7.15.0"

# Booted by every stage. Upstream CI uses `supabase start`, which needs a
# container runtime; a plain local Postgres plus the checked-in seed is the
# single-container equivalent.
DB_BOOT = """\
export PGDATA=/var/lib/postgresql/data
export PGUSER=postgres
export DATABASE_NAME="${DATABASE_NAME:-toiletmap}"

if [ -z "${DATABASE_URL:-}" ]; then
  service postgresql start 2>/dev/null || pg_ctlcluster "$(ls /etc/postgresql | head -1)" main start 2>/dev/null || true
  for _ in $(seq 1 30); do
    su postgres -c "psql -c 'select 1'" >/dev/null 2>&1 && break
    sleep 1
  done
  su postgres -c "psql -c \\"ALTER USER postgres PASSWORD 'postgres'\\"" >/dev/null 2>&1 || true
  su postgres -c "createdb ${DATABASE_NAME}" >/dev/null 2>&1 || true
  if [ -f supabase/seed.sql ]; then
    su postgres -c "psql -q -d ${DATABASE_NAME} -f supabase/seed.sql" >/dev/null 2>&1 || true
  fi
  export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/${DATABASE_NAME}"
fi
"""

# Shared by run.sh / test-run.sh / fix-run.sh. The three stages MUST configure
# and invoke the runner identically -- any divergence makes their results
# incomparable and silently breaks fail-to-pass detection.
TEST_ENV = """\
export CI=true
export NODE_ENV=test
export NEXT_TELEMETRY_DISABLED=1
export NODE_OPTIONS=--max-old-space-size=4096
export VERCEL_URL="${VERCEL_URL:-http://localhost:3000}"

# Non-secret Auth0 coordinates, matching .github/workflows/cypress-e2e-chrome.yml.
export AUTH0_ISSUER_BASE_URL="${AUTH0_ISSUER_BASE_URL:-https://gbptm.eu.auth0.com/}"
export AUTH0_AUDIENCE="${AUTH0_AUDIENCE:-https://www.toiletmap.org.uk/graphql}"
export AUTH0_BASE_URL="${AUTH0_BASE_URL:-http://localhost:3000}"
export CYPRESS_auth0Domain="${CYPRESS_auth0Domain:-gbptm.eu.auth0.com}"
export CYPRESS_auth0Scope="${CYPRESS_auth0Scope:-openid profile email}"
export CYPRESS_auth0SessionCookieName="${CYPRESS_auth0SessionCookieName:-appSession}"

# e2e.sh refuses to run without this file; recreate it from the environment so
# no secret is ever baked into the image.
cat > .env.test.local <<'ENVEOF'
ENVEOF
for _var in DATABASE_URL DATABASE_NAME VERCEL_URL \\
            AUTH0_SECRET AUTH0_CLIENT_ID AUTH0_CLIENT_SECRET AUTH0_ISSUER_BASE_URL \\
            AUTH0_AUDIENCE AUTH0_BASE_URL AUTH0_PERMISSIONS_KEY AUTH0_PROFILE_KEY \\
            CYPRESS_auth0Username CYPRESS_auth0Password CYPRESS_auth0ClientId \\
            CYPRESS_auth0ClientSecret CYPRESS_auth0CookieSecret CYPRESS_auth0Domain \\
            CYPRESS_auth0Scope CYPRESS_auth0SessionCookieName; do
  eval "_val=\\${$_var:-}"
  if [ -n "$_val" ]; then echo "$_var=$_val" >> .env.test.local; fi
done
cp -f .env.test.local .env.local 2>/dev/null || true

if [ -z "${CYPRESS_auth0Username:-}" ] || [ -z "${CYPRESS_auth0Password:-}" ]; then
  echo "WARNING: CYPRESS_auth0Username/Password are unset. cy.login() will fail" >&2
  echo "WARNING: and every spec in add.cy.ts will error in its before() hook." >&2
fi
"""

# Build, serve, drive Chrome. `--reporter json` makes Cypress emit a mocha JSON
# report; Cypress prints its own banner to the same stream, so parse_log
# extracts the outermost JSON object rather than assuming a clean document.
RUN_TESTS = """\
pnpm build 2>&1 || true

pnpm start > /home/next-server.log 2>&1 &
for _ in $(seq 1 60); do
  curl -sf http://localhost:3000/ >/dev/null 2>&1 && break
  sleep 2
done

pnpm exec cypress run \\
    --headless \\
    --browser chrome \\
    --spec 'cypress/e2e/**/*.ts' \\
    --reporter json \\
    > [[JSON_PATH]] 2>/home/cypress-stderr.log || true

echo "[[JSON_START]]"
cat [[JSON_PATH]] 2>/dev/null || true
echo ""
echo "[[JSON_END]]"

echo "----- cypress stderr -----"
tail -50 /home/cypress-stderr.log 2>/dev/null || true
echo "----- next server log -----"
tail -30 /home/next-server.log 2>/dev/null || true
"""


def _script(body: str, repo: str, base_sha: str = "") -> str:
    """Fill the placeholders.

    Plain replacement rather than str.format, so the shell's own ``${...}`` and
    ``$(...)`` need no brace escaping. The block placeholders expand first
    because they carry placeholders of their own.
    """
    return (
        body.replace("[[DB_BOOT]]", DB_BOOT)
        .replace("[[TEST_ENV]]", TEST_ENV)
        .replace("[[RUN_TESTS]]", RUN_TESTS)
        .replace("[[REPO]]", repo)
        .replace("[[BASE_SHA]]", base_sha)
        .replace("[[JSON_PATH]]", JSON_PATH)
        .replace("[[JSON_START]]", JSON_START)
        .replace("[[JSON_END]]", JSON_END)
        .replace("[[PNPM_VERSION]]", PNPM_VERSION)
    )


class ToiletmapImageBase(Image):
    """Base image: Cypress + Chrome + node 18.12, Postgres, and the repo clone."""

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
        # Published tag whose node line matches .nvmrc (18.12.x) and whose
        # Chrome is contemporary with the repo's Cypress 11.2.0.
        return "cypress/browsers:node18.12.0-chrome106-ff106"

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

        # DockerfileEnhancer rewrites the clone/COPY line into a standardized
        # ${REPO_URL} clone + ${BASE_COMMIT} checkout and appends the hardening
        # block, which strips the origin remote and every ref except the
        # detached HEAD. Nothing downstream may assume a remote exists.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

# The cypress/browsers base ships a Google Chrome apt source whose signing key
# has since rotated out of the image keyring, so apt-get update exits 100 on an
# unsigned-repo error. Chrome is already installed; drop the update channel.
RUN rm -f /etc/apt/sources.list.d/google-chrome*.list \\
    && apt-get update && apt-get install -y --no-install-recommends \\
    postgresql postgresql-client curl build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare pnpm@{PNPM_VERSION} --activate

{code}

{self.clear_env}

"""


class ToiletmapImageDefault(Image):
    """PR layer: patches, dependency install, and the three stage scripts."""

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
        return ToiletmapImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha

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
                _script(
                    """\
#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh

# Do NOT add `git fetch origin` here. The base image is hardened by
# DockerfileEnhancer, which removes the origin remote entirely, so a fetch
# would abort this script under `set -e`. The base image is already at
# BASE_COMMIT, so the checkout above is sufficient.

export CI=true
export NEXT_TELEMETRY_DISABLED=1
export HUSKY=0
export CYPRESS_INSTALL_BINARY=0

# postinstall runs `husky install && pnpm codegen` (graphql-codegen + prisma
# generate). prisma generate reads schema.prisma and needs no live database.
pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile
""",
                    repo,
                    base_sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

[[DB_BOOT]]
[[TEST_ENV]]
[[RUN_TESTS]]""",
                    repo,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch

# The test patch may touch dependencies.
export CI=true
export HUSKY=0
pnpm install --no-frozen-lockfile || true

[[DB_BOOT]]
[[TEST_ENV]]
[[RUN_TESTS]]""",
                    repo,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# The fix patch may touch dependencies.
export CI=true
export HUSKY=0
pnpm install --no-frozen-lockfile || true

[[DB_BOOT]]
[[TEST_ENV]]
[[RUN_TESTS]]""",
                    repo,
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("public-convenience-ltd", "toiletmap")
class PublicConvenienceLtdToiletmap(Instance):
    """Harness instance for public-convenience-ltd/toiletmap — Next.js + Cypress."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ToiletmapImageDefault(self.pr, self._config)

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
        """Prefer the mocha JSON report; fall back to Cypress console output."""
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        if not self._parse_json(log, passed, failed, skipped):
            self._parse_console(log, passed, failed, skipped)

        # A title may appear more than once (Cypress retries). Failure wins over
        # pass, and both win over skip, so the three sets stay disjoint --
        # TestResult raises if they overlap.
        passed -= failed
        skipped -= passed
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

    @staticmethod
    def _extract_json(log: str) -> Optional[dict]:
        start = log.find(JSON_START)
        end = log.find(JSON_END)
        if start == -1 or end == -1 or end <= start:
            return None

        blob = log[start + len(JSON_START) : end].strip()
        if not blob:
            return None

        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass

        # Cypress writes its run banner to the same stream as the reporter, so
        # fall back to the outermost {...} span.
        first, last = blob.find("{"), blob.rfind("}")
        if first == -1 or last <= first:
            return None
        try:
            return json.loads(blob[first : last + 1])
        except json.JSONDecodeError:
            return None

    @classmethod
    def _parse_json(
        cls, log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> bool:
        data = cls._extract_json(log)
        if not isinstance(data, dict):
            return False

        def _titles(key: str) -> list[str]:
            out = []
            for entry in data.get(key, []) or []:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("fullTitle") or entry.get("title") or "").strip()
                if name:
                    out.append(name)
            return out

        found = False
        for name in _titles("passes"):
            passed.add(name)
            found = True
        for name in _titles("failures"):
            failed.add(name)
            found = True
        for name in _titles("pending"):
            skipped.add(name)
            found = True

        return found

    @staticmethod
    def _parse_console(
        log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> None:
        # Cypress' spec reporter: "✓ title (123ms)", "1) title", "- title".
        pass_re = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
        fail_re = re.compile(r"^\s*\d+\)\s+(.+?)$")
        skip_re = re.compile(r"^\s*[-–]\s+(.+?)$")

        for line in log.splitlines():
            line = line.rstrip()
            m = fail_re.match(line)
            if m:
                failed.add(m.group(1).strip())
                continue
            m = pass_re.match(line)
            if m:
                passed.add(m.group(1).strip())
                continue
            m = skip_re.match(line)
            if m:
                skipped.add(m.group(1).strip())
