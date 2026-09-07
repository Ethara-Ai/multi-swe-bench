"""keyshade-xyz/keyshade harness config — PRs #382 to #427.

Differs from ``keyshade.py`` (written for PR #370) in two ways that this PR
range forces:

1. **Both packages are graded.** #387 and #394 touch only
   ``apps/api/**.e2e.spec.ts``; grading ``packages/api-client`` alone would give
   them zero gating tests. So each stage runs ``apps/api``'s e2e suite *and*
   ``packages/api-client``, announced with the ``===== PACKAGE: ... =====``
   marker that ``parse_jest_verbose_log`` keys on.

2. **apps/api is rebuilt every stage.** keyshade.py builds it once at image
   build time, on the grounds that "the fix patch does not touch apps/api" --
   true for #370, false for four of these five (#382/#387/#394/#427 all edit
   ``apps/api/src``). Building once would leave the API server serving BASE code
   at the fix stage, so the api-client specs could never observe the fix.
   ``apps/api``'s own e2e specs are unaffected either way: ts-jest compiles from
   source, not from dist.

Artifact split (current contract):
  base Dockerfile -> toolchain + services + git clone, then CMD. Nothing after.
  PR Dockerfile   -> FROM base, 7 COPY lines, RUN prepare.sh, then the git strip.
  prepare.sh      -> fetch/checkout the base sha, pnpm install, prisma generate,
                     with check_git_changes asserts. No stripping.

keyshade squash-merges, so a base sha is reachable from no branch -- only under
``refs/pull/*/head``. Fetching it by full sha still works (verified 2026-09-05),
which is what lets prepare.sh do the fetch rather than the clone.
"""

from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.keyshade_xyz.keyshade import (
    parse_jest_verbose_log,
)

_API_DIR = "apps/api"
_CLIENT_DIR = "packages/api-client"


# Shared verbatim by run.sh / test-run.sh / fix-run.sh so the only difference
# between graded stages is which patch was applied first (QC P7).
_TEST_BODY = r"""
export CI=true
export NODE_ENV=e2e
export DATABASE_URL="postgresql://prisma:prisma@localhost:5432/tests"
export REDIS_URL="redis://localhost:6379"
export JWT_SECRET="secret"
export BACKEND_URL="http://localhost:4200"
export NODE_OPTIONS="--max-old-space-size=4096"

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

su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='prisma'\"" \
    | grep -q 1 \
    || su postgres -c "psql -c \"CREATE USER prisma WITH PASSWORD 'prisma' SUPERUSER;\""

reset_db() {
    # Rebuilt before each suite: otherwise the second suite would inherit the
    # first one's rows, and the fix stage would inherit the test stage's.
    su postgres -c "psql -c 'DROP DATABASE IF EXISTS tests;'" > /dev/null
    su postgres -c "psql -c 'CREATE DATABASE tests OWNER prisma;'" > /dev/null
    cd /home/[[REPO]]/apps/api
    # Project-local prisma, never `pnpm dlx prisma`: the repo pins 5.13.0 and
    # dlx would fetch the current major against a 5.x schema.
    ./node_modules/.bin/prisma migrate deploy --schema=src/prisma/schema.prisma > /dev/null
}

########## suite 1: apps/api e2e (in-process, ts-jest -- no build needed) ##########
reset_db
cd /home/[[REPO]]/[[API_DIR]]
echo "===== PACKAGE: [[API_DIR]] ====="
set +e
pnpm exec jest \
    --config=jest.e2e-config.ts \
    --runInBand \
    --ci \
    --verbose \
    --forceExit \
    > /tmp/jest-api.out 2>&1
API_RC=$?
set -e
cat /tmp/jest-api.out
echo "jest exit code (api): ${API_RC}"
grep -qE '^(Test Suites|Tests):' /tmp/jest-api.out

########## suite 2: packages/api-client (HTTP client -- needs a live server) ##########
reset_db

# Rebuilt HERE, not in prepare.sh: four of the five PRs in this range edit
# apps/api/src, and a build baked at image time would serve BASE code from dist
# at the fix stage.
cd /home/[[REPO]]
pnpm exec turbo run build --filter=api > /tmp/build.out 2>&1 || {
    echo "FATAL: apps/api build failed"; tail -n 100 /tmp/build.out; exit 1;
}

cd /home/[[REPO]]/apps/api
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

cd /home/[[REPO]]/[[CLIENT_DIR]]
echo "===== PACKAGE: [[CLIENT_DIR]] ====="
set +e
# Displaces tests/config/{setup,teardown}.ts, which shell out to `docker
# compose` and call process.exit(0).
pnpm exec jest \
    --runInBand \
    --ci \
    --verbose \
    --forceExit \
    --globalSetup /home/noop-global.cjs \
    --globalTeardown /home/noop-global.cjs \
    > /tmp/jest-client.out 2>&1
CLIENT_RC=$?
set -e

kill "$API_PID" 2>/dev/null || true
wait "$API_PID" 2>/dev/null || true

cat /tmp/jest-client.out
echo "jest exit code (client): ${CLIENT_RC}"

# A non-zero rc is the honest outcome of a stage with failing tests and must not
# abort it -- the harness grades from the log text. A runner that never started
# prints no summary line; failing here turns that into a loud stage failure
# instead of a silent 0/0/0.
grep -qE '^(Test Suites|Tests):' /tmp/jest-client.out
""".replace("[[API_DIR]]", _API_DIR).replace("[[CLIENT_DIR]]", _CLIENT_DIR)


class KeyshadeEraImageBase(Image):
    """node:20-bookworm + Postgres 15 + Redis + pnpm, then the clone.

    Emitting the syntax directive makes DockerfileEnhancer return this file
    verbatim (image.py:316), so the enhancer does not rewrite the clone into
    clone+checkout+hardening. The hardening belongs to the PR layer.
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

    def dependency(self) -> Union[str, "Image"]:
        # bookworm, not alpine: the Prisma engines are glibc binaries, and
        # Debian 12 is what makes the /etc/postgresql/15 paths correct.
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-pnpm"

    def workdir(self) -> str:
        return "base-pnpm"

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
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PNPM_HOME=/usr/local/bin

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    postgresql-15 \\
    postgresql-client-15 \\
    redis-server \\
    openssl \\
    procps \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare pnpm@9.2.0 --activate

RUN printf '%s\\n' 'module.exports = async () => {{}};' > /home/noop-global.cjs

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class KeyshadeEraImageDefault(Image):
    """PR layer: FROM base, exactly 7 COPYs, prepare.sh, then the git strip."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return KeyshadeEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        def body(script: str) -> str:
            return script.replace("[[REPO]]", repo)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                body(
                    """#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# keyshade squash-merges, so this sha sits under refs/pull/*/head and is an
# ancestor of no branch. Fetching it by full sha works; a plain clone cannot
# check it out.
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --no-tags --depth=1 origin [[SHA]] 2>/dev/null \\
    || git fetch --no-tags origin '+refs/pull/*/head:refs/remotes/origin/pr/*/head' 2>/dev/null \\
    || true
git checkout -f [[SHA]]
bash /home/check_git_changes.sh

# `|| true` only here: a native-module compile failure must not abort the build.
pnpm install --frozen-lockfile || pnpm install || true

cd /home/[[REPO]]/apps/api
./node_modules/.bin/prisma generate --schema=src/prisma/schema.prisma

# apps/api is deliberately NOT built here -- the run scripts rebuild it per
# stage so the fix patch's apps/api changes reach the served dist.

cd /home/[[REPO]]
git checkout -- .
bash /home/check_git_changes.sh
""".replace("[[ORG]]", org).replace("[[SHA]]", sha)
                ),
            ),
            File(".", "run.sh", body("#!/bin/bash\nset -e\n\ncd /home/[[REPO]]\n" + _TEST_BODY)),
            File(
                ".",
                "test-run.sh",
                body(
                    """#!/bin/bash
set -e

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
"""
                    + _TEST_BODY
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                body(
                    """#!/bin/bash
set -e

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
"""
                    + _TEST_BODY
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("keyshade-xyz", "keyshade_427_to_382")
class Keyshade427To382(Instance):
    """Harness instance for keyshade-xyz/keyshade — PRs #382 to #427."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KeyshadeEraImageDefault(self.pr, self._config)

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
        # Shared with keyshade.py: emits one entry per suite file (the only
        # signal when a file fails to compile) plus one per test, both prefixed
        # by the announced package so ids stay repo-root-relative.
        return parse_jest_verbose_log(log)
