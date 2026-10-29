from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: goharbor/harbor PRs 23078..23865 (Apr-Sep 2026, release line 2.15+).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-23865_to_23078"

# src/go.mod declares `go 1.26.3` .. `go 1.26.7` across these six commits, and
# GOTOOLCHAIN=local forbids fetching a newer toolchain, so the image must already
# carry >= 1.26.7. golang:1.24-bookworm (the legacy harbor.py base) refuses to
# build these trees outright.
GO_IMAGE = "golang:1.26-bookworm"

MODULE_PREFIX = "github.com/goharbor/harbor/src"
REPO_DIR = "/home/harbor"
SRC_DIR = f"{REPO_DIR}/src"

# The slowest graded package finishes in ~10s; the timeout only bounds a hang.
GO_TEST_FLAGS = "-v -count=1 -timeout 10m"

# Upstream tests that hang at these commits independently of every PR here. At
# #23647's base, TestCopy's accessory mock feeds copyDeeply its own artifact back
# and it recurses until the timeout kills the whole package binary -- in all
# three stages alike, since neither patch touches TestCopy or copyDeeply.
# Skipped identically per stage, so no transition is created or erased.
HANGING_TESTS = {"./controller/artifact": "TestControllerTestSuite/TestCopy"}

# src/server/v2.0/{models,restapi} -- the whole REST API layer every handler
# imports -- is GENERATED from api/v2.0/swagger.yaml and gitignored, so a bare
# checkout does not compile. All six commits pin SWAGGER_VERSION=v0.33.1 in the
# Makefile, which makes the generator era-invariant tooling for the shared base.
SWAGGER_VERSION = "v0.33.1"

# Package-level key emitted from go's own verdict line. Go gates tests at COMPILE
# time: when a test patch references a symbol only the fix adds, the package fails
# to build, `go test` prints no `--- PASS/FAIL` lines at all, and the graded test
# can never pair FAILED(test) with PASSED(fix) -- f2p comes out 0. This key is
# emitted only when go actually printed a verdict for the package, so a stage that
# never ran cannot manufacture a transition.
BUILD_TEST_NAME = "<build>"

# Both sides of the diff header: a renamed test file is named only on the b/ side.
_DIFF_PATHS_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)


def target_test_files(pr: PullRequest) -> list[str]:
    """Graded test files, derived from this PR's own test patch."""
    pairs = _DIFF_PATHS_RE.findall(pr.test_patch or "")
    paths = {p for pair in pairs for p in pair if p.endswith("_test.go")}
    return sorted(p for p in paths if p.startswith("src/"))


def target_packages(pr: PullRequest) -> list[str]:
    """Go packages owning those files, as ./-relative paths from src/.

    Scoped rather than `./...`: harbor has ~58 test packages, most of which need
    the database, and running all of them per stage costs ~40min and fails for
    reasons unrelated to the PR.

    May be EMPTY. PR 23078 is a frontend-only Angular upgrade whose test patch
    touches a single shell script (tests/ci/ui_ut_run.sh) and no Go file at all;
    there is nothing here a Go test command can grade. test.sh reports that
    plainly rather than falling back to ./... and inventing a signal.
    """
    owners = {path.rsplit("/", 1)[0][len("src/"):] for path in target_test_files(pr)}
    return sorted(f"./{owner}" for owner in owners if owner)


class HarborImageBase(Image):
    """Shared base: toolchain + full-history clone. No pin, no scrub."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return GO_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming
        # it is what would pin this shared image to one PR's commit.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV GOFLAGS=-buildvcs=false \\
    GOTOOLCHAIN=local \\
    GO111MODULE=on \\
    CGO_ENABLED=1 \\
    GOPATH=/go \\
    GOCACHE=/go/cache

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    pkg-config \\
    libldap2-dev \\
    postgresql \\
    postgresql-contrib \\
    redis-server \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {REPO_DIR} && \\
    cd {REPO_DIR} && git rev-parse HEAD >/dev/null

# Same install as harbor's own tools/swagger/Dockerfile. Built from source, so
# it is native on every TARGETARCH.
RUN GOBIN=/go/bin go install github.com/go-swagger/go-swagger/cmd/swagger@{SWAGGER_VERSION} && \\
    swagger version && \\
    go clean -cache -modcache

{self.clear_env}

CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# harbor's `make gen_apis` (Makefile: swagger_generate_server) minus the docker
# wrapper: same flags, same templates, same target. Run from the repo root so
# tools/swagger/templates resolves; output lands in gitignored paths, so the
# tree stays clean as far as git is concerned.
GEN_APIS_SH = r"""#!/bin/bash
set -euo pipefail

cd [[REPO_DIR]]

rm -rf src/server/v2.0/models src/server/v2.0/restapi
mkdir -p src/server/v2.0

if ! swagger generate server \
        --template-dir=tools/swagger/templates \
        --exclude-main \
        --additional-initialism=CVE \
        --additional-initialism=GC \
        --additional-initialism=OIDC \
        -f api/v2.0/swagger.yaml -A harbor --target src/server/v2.0 \
        >/tmp/gen_apis.log 2>&1; then
    tail -50 /tmp/gen_apis.log >&2
    echo "gen_apis: swagger generate server failed" >&2
    exit 1
fi

test -d src/server/v2.0/models
test -d src/server/v2.0/restapi
rm -f /tmp/gen_apis.log
echo "gen_apis: src/server/v2.0/{models,restapi} generated"
"""


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

# ========== Section 1: PIN ==========
cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
# From here the tree may be modified on purpose; no clean-tree assertion below.
cd [[SRC_DIR]]

export GOFLAGS=-buildvcs=false
export GOTOOLCHAIN=local
export GO111MODULE=on
export GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"
export GOSUMDB="${GOSUMDB:-sum.golang.org}"

for attempt in 1 2 3 4 5; do
    if go mod download; then
        break
    fi
    if [ "$attempt" = 5 ]; then
        echo "prepare: go mod download failed after 5 attempts" >&2
        exit 1
    fi
    sleep 15
done

bash /home/gen_apis.sh

# Warm the build cache so the graded stages start compiled. Tolerated because
# the gate below re-runs the same build non-tolerantly.
go build ./... >/dev/null 2>&1 || true

# ========== Section 3: GATE (non-tolerant, last) ==========
go version
# -n prints every command it would run to stderr; keep that out of the build log
# unless the load actually fails.
go vet -n ./... >/dev/null 2>/tmp/go_vet_n.log || { tail -50 /tmp/go_vet_n.log >&2; exit 1; }
rm -f /tmp/go_vet_n.log
go build ./...

# Only packages that already exist at this commit can be compile-checked: a PR
# may CREATE the package it tests (#23527 adds lib/pattern and
# pkg/auditext/event/project), and `go test` on a missing directory is a setup
# failure, not a dependency problem.
_gate_pkgs=""
for _pkg in [[TEST_PKGS]]; do
    if compgen -G "${_pkg}/*.go" >/dev/null; then
        _gate_pkgs="${_gate_pkgs} ${_pkg}"
    fi
done
go test -run '^$' -count=1 ${_gate_pkgs:-./lib/...}
echo DEPS_OK
"""


# Postgres + Redis, provisioned per graded stage (each stage is a fresh
# container). ~54 of harbor's 58 test packages call InitDatabaseFromEnv() in
# TestMain and FATAL without them -- identically in all three stages, which
# erases every fail->pass transition. The readiness wait is NON-tolerant: a
# database that never comes up must fail the stage loudly rather than be read as
# "the tests failed".
SERVICES_SH = r"""#!/bin/bash
set -eo pipefail

export POSTGRESQL_HOST=localhost
export POSTGRESQL_PORT=5432
export POSTGRESQL_USR=postgres
export POSTGRESQL_PWD=root123
export POSTGRESQL_DATABASE=registry
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USR=postgres
export POSTGRES_PWD=root123
export REDIS_HOST=localhost
export REDIS_PORT=6379
export POSTGRES_MIGRATION_SCRIPTS_PATH=[[REPO_DIR]]/make/migrations/postgresql/

_PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | head -1)"
test -n "$_PG_BIN"
export PGDATA=/var/lib/postgresql/pgdata

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$PGDATA"
    su postgres -c "$_PG_BIN/initdb -A trust -D $PGDATA" >/dev/null
fi

su postgres -c "$_PG_BIN/pg_ctl -D $PGDATA -o '-c listen_addresses=* -c port=5432' -l /tmp/pg.log -w start" >/dev/null

_pg_ready=0
for _i in $(seq 1 30); do
    if su postgres -c "psql -h localhost -p 5432 -c 'select 1'" >/dev/null 2>&1; then
        _pg_ready=1
        break
    fi
    sleep 1
done
test "$_pg_ready" = 1 || {
    echo "services: postgres did not become ready" >&2
    tail -40 /tmp/pg.log >&2 || true
    exit 1
}

su postgres -c "psql -h localhost -p 5432 -c \"ALTER USER postgres PASSWORD 'root123';\"" >/dev/null
su postgres -c "psql -h localhost -p 5432 -tc \"SELECT 1 FROM pg_database WHERE datname='registry'\"" \
    2>/dev/null | grep -q 1 \
    || su postgres -c "psql -h localhost -p 5432 -c 'CREATE DATABASE registry;'" >/dev/null

redis-server --daemonize yes
_redis_ready=0
for _i in $(seq 1 30); do
    if redis-cli -h localhost -p 6379 ping 2>/dev/null | grep -q PONG; then
        _redis_ready=1
        break
    fi
    sleep 1
done
test "$_redis_ready" = 1 || {
    echo "services: redis did not become ready" >&2
    exit 1
}

echo "services: postgres + redis ready"
"""


# The one test command every stage runs. `-p 1` is required: every DB-dependent
# package bootstraps golang-migrate against the single shared `registry`
# database, and parallel packages race on CREATE TABLE schema_migrations.
TEST_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC

# Sourced, not executed: the stage needs the exported DB/Redis env vars.
. /home/services.sh

cd [[SRC_DIR]]

export GOFLAGS=-buildvcs=false
export GOTOOLCHAIN=local
export GO111MODULE=on

_PKGS="[[TEST_PKGS]]"
test -n "$_PKGS" || {
    echo "test: this PR's test patch contains no Go test file; nothing to grade"
    exit 0
}

# Regenerated from THIS stage's spec, not reused from the build: the fix patches
# of #23863 and #23527 edit api/v2.0/swagger.yaml, and handler code written
# against the new spec does not compile against operations generated from the
# old one.
bash /home/gen_apis.sh

go test -p 1 [[GO_TEST_FLAGS]] [[SKIP_FLAG]] $_PKGS 2>&1
"""


class HarborImageDefault(Image):
    """Per-PR layer: patches, scripts, the pin (via prepare.sh), and the scrub."""

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
        return HarborImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _skip_flag(pkgs: list[str]) -> str:
        # go accepts one -skip; HANGING_TESTS holds a single entry today, and a
        # second would need its own `|`-joined pattern per name level.
        names = [HANGING_TESTS[p] for p in pkgs if p in HANGING_TESTS]
        return f"-skip '^{names[0].replace('/', '$/^')}$'" if names else ""

    def _expand(self, template: str) -> str:
        pkgs = target_packages(self.pr)
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SRC_DIR]]", SRC_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_PKGS]]", " ".join(pkgs))
            .replace("[[GO_TEST_FLAGS]]", GO_TEST_FLAGS)
            .replace("[[SKIP_FLAG]]", self._skip_flag(pkgs))
        )

    def install_files(self) -> list[File]:
        """Files the provisioning layer depends on.

        Kept separate from the grading files so that re-cutting a patch does not
        invalidate the `go mod download` + `go build` layer.
        """
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "gen_apis.sh", self._expand(GEN_APIS_SH)),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
            "fix-run.sh": (
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            ),
        }

        staged = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\nbash /home/test.sh\n"
                ),
            )
            for name, patch_step in stage_patches.items()
        ]

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "services.sh", self._expand(SERVICES_SH)),
            File(".", "test.sh", self._expand(TEST_SH)),
        ] + staged

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        # PR images are never touched by DockerfileEnhancer (it returns raw the
        # moment dependency() is an Image), so everything this layer needs is
        # written out here. The scrub lives here rather than in the shared base:
        # the base must keep full history so every PR of the shard can reach its
        # own commit.
        #
        # The scrub deliberately contains no `git reset`, no `git clean` and no
        # path-scoped checkout -- prepare.sh is allowed to have modified tracked
        # files, and any of those would silently revert that work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{copies(self.install_files())}
RUN bash /home/prepare.sh

{copies(self.grading_files())}
WORKDIR {REPO_DIR}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {REPO_DIR}/.gitmodules ]; then \\
        cd {REPO_DIR} && git submodule foreach --recursive ' \\
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

{self.clear_env}

"""


PR_LO, PR_HI = 23078, 23865


@Instance.register("goharbor", "harbor_23865_to_23078")
class HARBOR_23865_TO_23078(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HarborImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        module = re.escape(MODULE_PREFIX)
        # Not anchored at column 0: go indents subtest results. The captured name
        # keeps the full `TestFunc/subtest` path -- stripping the suffix would
        # collapse every subtest of a function onto one colliding name.
        result_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        package_re = re.compile(r"^(?:ok|FAIL|---)\s+(" + module + r"\S*)")
        build_fail_re = re.compile(r"^FAIL\s+(" + module + r"\S*)\s+\[build failed\]")
        build_ok_re = re.compile(
            r"^(?:ok|FAIL)\s+(" + module + r"\S*)\s+(?:[0-9]|\(cached\))"
        )
        no_tests_re = re.compile(r"^\?\s+(" + module + r"\S*)\s+\[no test files\]")
        timeout_re = re.compile(r"^panic: test timed out after ")
        running_re = re.compile(r"^\s+(Test\S+) \([0-9]")
        in_timeout = False

        default_pkg = (target_packages(self.pr) or ["."])[0].lstrip(".").lstrip("/")
        fallback = f"{MODULE_PREFIX}/{default_pkg}" if default_pkg else MODULE_PREFIX

        buckets: dict[str, set[str]] = {
            "PASSED": set(),
            "FAILED": set(),
            "SKIPPED": set(),
        }
        outcome = {"PASS": "PASSED", "FAIL": "FAILED", "SKIP": "SKIPPED"}
        pending: list[tuple[str, str]] = []

        def flush(package: str) -> None:
            # Results are buffered until go names the package they belong to, so
            # every key is `<package>::<TestName>` and identical test names in
            # different packages stay distinct.
            for status, name in pending:
                buckets[outcome[status]].add(f"{package}::{name}")
            pending.clear()

        for line in log.splitlines():
            line = line.rstrip()

            result_match = result_re.match(line)
            if result_match:
                pending.append((result_match.group(1), result_match.group(2)))
                continue

            build_fail_match = build_fail_re.match(line)
            if build_fail_match:
                package = build_fail_match.group(1)
                buckets["FAILED"].add(f"{package}::{BUILD_TEST_NAME}")
                flush(package)
                continue

            build_ok_match = build_ok_re.match(line)
            if build_ok_match:
                package = build_ok_match.group(1)
                buckets["PASSED"].add(f"{package}::{BUILD_TEST_NAME}")
                flush(package)
                continue

            # A timeout panics the binary before any `--- FAIL` is printed; the
            # tests it names as still running are the ones that failed.
            if timeout_re.match(line):
                in_timeout = True
                continue
            if in_timeout:
                running_match = running_re.match(line)
                if running_match:
                    pending.append(("FAIL", running_match.group(1)))
                    continue
                if not line.strip() or line.startswith("goroutine "):
                    in_timeout = False

            if no_tests_re.match(line):
                continue

            package_match = package_re.match(line)
            if package_match:
                flush(package_match.group(1))

        flush(fallback)

        passed = buckets["PASSED"]
        failed = buckets["FAILED"]
        skipped = buckets["SKIPPED"]

        # A name may live in only one bucket; failure wins over both others.
        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


# ---------------------------------------------------------------------------
# Routing, owned entirely by this file -- harbor.py is not modified.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty. These six records carry no
# number_interval, so they arrive on the bare name "goharbor/harbor", which
# harbor.py registers to its own legacy Harbor class.
#
# Instance.register() overwrites rather than erroring, and __init__.py imports
# harbor.py BEFORE this module, so the registration below takes that bare name
# over without touching harbor.py. The incumbent class is captured first and
# every PR outside this interval is handed straight back to it, so no other
# goharbor record changes behaviour. Records that DO carry a number_interval
# never reach here at all -- they key directly on their own interval.
_INCUMBENT = Instance._registry.get("goharbor/harbor")


@Instance.register("goharbor", "harbor")
class HarborBareName(Instance):
    """Not an implementation -- a router for the bare "goharbor/harbor" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return HARBOR_23865_TO_23078(pr, config, *args, **kwargs)

        # Outside this era: hand back to whoever held the bare name before us.
        if _INCUMBENT is None:
            raise ValueError(
                f"goharbor/harbor#{pr.number} falls outside the "
                f"{PR_LO}..{PR_HI} interval this adapter serves, and no previous "
                f"holder of the bare 'goharbor/harbor' key was captured at import "
                f"time. Import multi_swe_bench.harness.repos.golang.goharbor.harbor "
                f"before this module (see goharbor/__init__.py)."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
