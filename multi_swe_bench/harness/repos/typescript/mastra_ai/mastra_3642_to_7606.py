"""mastra-ai/mastra — shared-base era config for PRs 3642 .. 7606.

A separate file from ``mastra_7606_to_3642.py``, which keeps its behaviour and its
own interval key, as do the 43 intervals in ``mastra.py``.

Architecture: **shared base + single-commit fetch**, the third shape the Dockerfile
QC recognises.  The base ships the shard-wide environment and clones nothing; each
PR layer fetches its own commit inside the single ``RUN bash /home/prepare.sh``.

* **One base for all ten PRs.**  The base never consumes ``BASE_COMMIT``, so it is
  pinned to nobody's commit and ten images share one apt install, one PGDG install
  and one DynamoDB download.
* **PR 6845 resolves.**  Its base commit ``8169f230f7`` is on the deleted branch
  ``systemInstructions`` and is an ancestor of nothing, so no clone reaches it.  A
  fetch naming the sha does.
* **Nothing leaks.**  One commit is fetched, in the same ``RUN`` that installs, so
  no layer holds history the merged view hides.
* **No installs in the graded stages.**  ``prepare.sh`` installs at build time, so
  grading needs no network and all three stages see one resolved dependency set.
* **Filtered install.**  This is the change that matters for image size.  A root
  ``pnpm install`` resolves every workspace package in the monorepo -- all stores,
  all integrations, the CLI, the playground -- when a given instance tests one or
  two.  That produced 9.4 GB images, roughly 7 GB of it dependencies no test
  imports.  Installing with ``--filter '<pkg>...'`` per tested package pulls only
  that package and its dependency closure.  ``--include-workspace-root`` is
  required with it: every tested package declares its own vitest, but ``turbo``
  lives only at the root and orders the workspace builds.
* **The base is written verbatim.**  Line 1 is the BuildKit syntax directive, the
  ``DockerfileEnhancer`` opt-out, so the proxy ARGs, CA trust store and OCI labels
  are declared here rather than injected.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "mastra-ai"
REPO = "mastra"
ERA = "mastra_3642_to_7606"
BASE_TAG = "base-3642_to_7606"

NODE_IMAGE = "node:20"
REPO_URL = f"https://github.com/{ORG}/{REPO}.git"
REPO_DIR = f"/home/{REPO}"

PG_MAJOR = "16"
PG_PORT = "5434"
PG_DB = "mastra"
PG_URL = f"postgresql://postgres:postgres@localhost:{PG_PORT}/{PG_DB}"

DDB_PORT = "8000"
DDB_HOME = "/opt/dynamodb-local"
DDB_URL = "https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_latest.tar.gz"

FALLBACK_PNPM = "10.12.4"

SHEBANG = "#!/bin/bash"


# ---------------------------------------------------------------------------
# Test target extraction
# ---------------------------------------------------------------------------


def _test_files(test_patch: str) -> list[str]:
    """Paths of test files touched by the patch, in patch order, deduplicated.

    Snapshot files sit next to their suite and match ``.test.`` too, but vitest
    treats a positional argument as a path filter, so a ``.snap`` argument would
    only contribute a filter matching nothing.
    """
    seen: set[str] = set()
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", test_patch, re.MULTILINE):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        if path.endswith(".snap"):
            continue
        if ".test." in path or ".spec." in path:
            files.append(path)
    return files


def _group_by_package(test_patch: str) -> dict[str, list[str]]:
    """Group test files under their workspace package, keeping paths relative."""
    grouped: dict[str, list[str]] = {}
    for f in _test_files(test_patch):
        parts = f.split("/")
        if len(parts) >= 3:
            pkg = "/".join(parts[:2])
            relative = "/".join(parts[2:])
        else:
            pkg = "."
            relative = f
        grouped.setdefault(pkg, []).append(relative)
    return grouped


def _packages(test_patch: str) -> list[str]:
    return list(_group_by_package(test_patch).keys())


def _install_command(test_patch: str) -> str:
    """Install only the tested packages and their dependency closures.

    ``<pkg>...`` selects the package plus everything it depends on, so a filtered
    install still builds @mastra/core for a package that imports it.
    ``--include-workspace-root`` adds the root's own devDependencies, which is
    where turbo lives.
    """
    pkgs = list(_packages(test_patch))
    # packages/cli's build script does `cd ../cli/src/playground && pnpm build`.
    # That playground is its own workspace project (pnpm-workspace.yaml lists it)
    # and owns vite, but cli reaches it through a shell cd rather than declaring a
    # dependency, so the closure filter cannot see it and the build dies on
    # "vite: not found". Add it explicitly.
    if "packages/cli" in pkgs:
        pkgs.append("packages/cli/src/playground")
    if not pkgs:
        return "pnpm install --no-frozen-lockfile"
    # Braces are load-bearing: `./pkg...` is parsed as one path glob and the
    # trailing `...` is swallowed, selecting the package ALONE. `{./pkg}...` is
    # what makes `...` mean "and its dependencies", so a workspace dep such as
    # @mastra/schema-compat gets its own devDeps and can build.
    filters = " ".join(f"--filter '{{./{pkg}}}...'" for pkg in pkgs)
    return (
        "pnpm install --no-frozen-lockfile --include-workspace-root " + filters
    )


def _build_commands(test_patch: str, strict: bool = False) -> str:
    """Build the tested packages and their workspace dependencies.

    ``strict`` is used only by prepare.sh.  A build failure there means a
    workspace dependency never produced its dist, so every suite would fail to
    resolve its imports and the stage would emit an empty log that report.py
    scores as vacuously valid.  That must abort the image build instead.  The
    graded stages stay tolerant, because there a broken build still has to
    produce an attributable test log.
    """
    pkgs = _packages(test_patch)
    if not pkgs:
        return "true"
    tail = "" if strict else " || true"
    return "\n".join(
        f"pnpm turbo run build --filter='{{./{pkg}}}...' 2>&1{tail}" for pkg in pkgs
    )


# Suites that talk to a live model provider. `agent.test.ts` builds a real
# OpenAI client from OPENAI_API_KEY and calls gpt-4o in many of its cases; with no
# key the run parks on the network and never returns (observed: 25 minutes idle at
# 0% CPU before being killed). For these files only, grading is narrowed to the
# test names the patch itself introduces, which skips the model-calling cases the
# patch never touches. Every other file still runs in full, so the p2p regression
# signal elsewhere is unchanged.
_MODEL_API_TEST_FILES = {
    "packages/core/src/agent/agent.test.ts",
}


def _added_test_names(test_patch: str, path: str) -> list[str]:
    """Test names added by this patch inside one file, in patch order."""
    names: list[str] = []
    seen: set[str] = set()
    current: Optional[str] = None
    for line in test_patch.splitlines():
        m = re.match(r"^diff --git a/\S+ b/(\S+)", line)
        if m:
            current = m.group(1)
            continue
        if current != path or not line.startswith("+") or line.startswith("+++"):
            continue
        m = re.search(r"\b(?:it|test)(?:\.\w+)?\(\s*[\'\"`](.+?)[\'\"`]", line)
        if m:
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _test_commands(test_patch: str) -> str:
    grouped = _group_by_package(test_patch)
    if not grouped:
        return "pnpm vitest run --reporter=verbose 2>&1 || true"
    lines = []
    for pkg, rel_files in grouped.items():
        # Banner first so parse_log sees an ordered, attributable stream even when
        # one package's runner dies before printing anything of its own.
        lines.append(f"echo '===== MSB_PKG {pkg} ====='")
        plain: list[str] = []
        for rel in rel_files:
            full = f"{pkg}/{rel}"
            names = (
                _added_test_names(test_patch, full)
                if full in _MODEL_API_TEST_FILES
                else []
            )
            if names:
                # -t takes a regex matched against the full test name. Escape the
                # literals so punctuation in a title cannot alter the pattern.
                pattern = "|".join(re.escape(n) for n in names)
                # --passWithNoTests matters in the run stage: the patch has not
                # been applied there, so none of these names exist yet and vitest
                # would otherwise exit non-zero on an empty selection.
                lines.append(
                    f"pnpm --filter './{pkg}' exec vitest run --reporter=verbose "
                    f"{rel} -t '{pattern}' --passWithNoTests 2>&1 || true"
                )
            else:
                plain.append(rel)
        if plain:
            lines.append(
                f"pnpm --filter './{pkg}' exec vitest run --reporter=verbose "
                f"{' '.join(plain)} 2>&1 || true"
            )
    return "\n".join(lines)


def _needs_postgres(test_patch: str) -> bool:
    return any(p.startswith("stores/pg") for p in _packages(test_patch))


def _needs_dynamodb(test_patch: str) -> bool:
    return any(p.startswith("stores/dynamodb") for p in _packages(test_patch))


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_RE_PASS = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
_RE_FAIL = re.compile(r"^\s*[×✗]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
_RE_SKIP = re.compile(r"^\s*[↓○]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
_RE_FAIL_FILE = re.compile(
    r"^\s*FAIL\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|mjs))\s*$"
)
_RE_BANNER = re.compile(r"^=====\s+MSB_PKG\s+(\S+)\s+=====$")


def _test_name(pkg: str, raw: str) -> str:
    """``pkg::file > suite > case`` -> ``pkg::file::suite/case``.

    Namespacing by package keeps identically named cases in different workspace
    packages from colliding in the pass/fail sets.
    """
    parts = raw.split(" > ")
    if len(parts) <= 1:
        name = raw
    else:
        name = parts[0] + "::" + "/".join(parts[1:])
    return f"{pkg}::{name}" if pkg else name


def _parse_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    pkg = ""

    for raw_line in test_log.splitlines():
        line = _ANSI.sub("", raw_line).strip()
        if not line:
            continue

        m = _RE_BANNER.match(line)
        if m:
            pkg = m.group(1)
            continue

        m = _RE_FAIL.match(line)
        if m:
            failed.add(_test_name(pkg, m.group(1).strip()))
            continue

        m = _RE_PASS.match(line)
        if m:
            passed.add(_test_name(pkg, m.group(1).strip()))
            continue

        m = _RE_SKIP.match(line)
        if m:
            skipped.add(_test_name(pkg, m.group(1).strip()))
            continue

        m = _RE_FAIL_FILE.match(line)
        if m:
            failed.add(_test_name(pkg, m.group(1).strip()))
            continue

    # A name seen failing anywhere is failing: vitest reprints a case in its
    # failure summary, and a retried case can print both markers.
    passed -= failed
    skipped -= failed
    skipped -= passed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


# ---------------------------------------------------------------------------
# Shared files
# ---------------------------------------------------------------------------

_SERVICES_SH = f"""{SHEBANG}
# Started at the top of every stage script: a server started in a build layer
# does not survive into the container the stage actually runs in.

start_postgres() {{
    pg_ctlcluster {PG_MAJOR} main start >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do
        if su postgres -c "pg_isready -q -p {PG_PORT}"; then
            echo "[services] postgres ready on {PG_PORT}"
            return 0
        fi
        sleep 1
    done
    echo "[services] WARNING: postgres did not become ready" >&2
    return 1
}}

ddb_http_code() {{
    curl -s -o /dev/null -w '%{{http_code}}' "http://localhost:{DDB_PORT}" 2>/dev/null || true
}}

start_dynamodb() {{
    if ddb_http_code | grep -qE '^[1-5][0-9][0-9]$'; then
        echo "[services] dynamodb already listening on {DDB_PORT}"
        return 0
    fi
    nohup java -Djava.library.path={DDB_HOME}/DynamoDBLocal_lib \\
        -jar {DDB_HOME}/DynamoDBLocal.jar -inMemory -port {DDB_PORT} \\
        > /home/dynamodb-local.log 2>&1 &
    for _ in $(seq 1 60); do
        # DynamoDB Local answers a bare GET with 400, so curl -f would report
        # failure on a healthy server. A refused connection yields code 000, so any
        # real 1xx-5xx status means the port is listening.
        if ddb_http_code | grep -qE '^[1-5][0-9][0-9]$'; then
            echo "[services] dynamodb ready on {DDB_PORT}"
            return 0
        fi
        sleep 1
    done
    echo "[services] WARNING: dynamodb did not become ready" >&2
    return 1
}}
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""


# ---------------------------------------------------------------------------
# Shared base image
#
# BASE_COMMIT is declared and never referenced.  Declaring it silences BuildKit's
# unused-argument warning, since the harness passes it to every image; consuming
# it is what would pin a shared base to one PR's commit and break the other nine.
# ---------------------------------------------------------------------------


class MastraSharedBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return [File(".", "services.sh", _SERVICES_SH)]

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="{REPO_URL}"
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

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

# CA trust first: every RUN below this line talks to the network, and the
# inspecting proxy's certificate has to already be trusted when they do.
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends \\
        build-essential ca-certificates curl g++ git gnupg jq make \\
        default-jre-headless pkg-config python3 wget; \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

# pgvector is not in Debian bookworm, so postgres comes from the PGDG repo, which
# publishes both amd64 and arm64 for bookworm.
RUN set -eux; \\
    install -d /usr/share/postgresql-common/pgdg; \\
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \\
        https://www.postgresql.org/media/keys/ACCC4CF8.asc; \\
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \\
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \\
        > /etc/apt/sources.list.d/pgdg.list; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends \\
        postgresql-{PG_MAJOR} postgresql-client-{PG_MAJOR} postgresql-{PG_MAJOR}-pgvector; \\
    rm -rf /var/lib/apt/lists/*

# listen_addresses is deliberately left at the Debian default (localhost):
# pg_conftool writes the value verbatim, so passing "'localhost'" makes postgres
# try to resolve a host whose name includes the quote characters.
RUN set -eux; \\
    pg_conftool {PG_MAJOR} main set port {PG_PORT}; \\
    pg_conftool {PG_MAJOR} main set fsync off; \\
    pg_conftool {PG_MAJOR} main set full_page_writes off; \\
    pg_conftool {PG_MAJOR} main set synchronous_commit off; \\
    pg_ctlcluster {PG_MAJOR} main start; \\
    su postgres -c "psql -p {PG_PORT} -c \\"ALTER USER postgres PASSWORD 'postgres';\\""; \\
    su postgres -c "createdb -p {PG_PORT} {PG_DB}"; \\
    su postgres -c "psql -p {PG_PORT} -d {PG_DB} -c 'CREATE EXTENSION IF NOT EXISTS vector;'"; \\
    su postgres -c "psql -p {PG_PORT} -d postgres -c 'CREATE EXTENSION IF NOT EXISTS vector;'"; \\
    pg_ctlcluster {PG_MAJOR} main stop

# DynamoDB Local ships native sqlite4java for linux-amd64 and linux-aarch64, so
# the same tarball serves both architectures.
RUN set -eux; \\
    mkdir -p {DDB_HOME}; \\
    curl -fsSL -o /tmp/ddb.tar.gz "{DDB_URL}"; \\
    tar -xzf /tmp/ddb.tar.gz -C {DDB_HOME}; \\
    rm -f /tmp/ddb.tar.gz; \\
    test -f {DDB_HOME}/DynamoDBLocal.jar

# Fixture credentials for the two loopback services above. They are not secrets:
# postgres listens on localhost only, and DynamoDB Local ignores the values.
ENV DB_URL="{PG_URL}" \\
    POSTGRES_URL="{PG_URL}" \\
    DATABASE_URL="{PG_URL}" \\
    PGPORT="{PG_PORT}" \\
    AWS_REGION="local-test" \\
    AWS_ACCESS_KEY_ID="test" \\
    AWS_SECRET_ACCESS_KEY="test" \\
    DYNAMODB_ENDPOINT="http://localhost:{DDB_PORT}" \\
    CI="true" \\
    NODE_OPTIONS="--max-old-space-size=6144"

COPY services.sh /home/
RUN chmod +x /home/services.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# PR layer: acquisition, dependency install, graded stages
# ---------------------------------------------------------------------------


def _prepare_sh(pr: PullRequest) -> str:
    """Build-time script: acquire the commit, install dependencies, gate.

    Everything above the dependency marker leaves the tree provably pristine and
    detached at the base commit.  Everything below it may dirty the tree, which is
    why no clean-tree assertion follows.
    """
    pkgs = _packages(pr.test_patch)
    gate_pkg = pkgs[0] if pkgs else "."
    return f"""{SHEBANG}
set -euo pipefail

BASE_SHA="{pr.base.sha}"

mkdir -p {REPO_DIR}
cd {REPO_DIR}

# Single-commit acquisition. A depth-1 fetch that NAMES the sha reaches commits
# that no surviving branch points at -- PR 6845 branched from `systemInstructions`,
# since deleted, so no clone of any branch can retrieve it.
git init -q
git remote add origin "{REPO_URL}"
git fetch --depth 1 --no-tags origin "$BASE_SHA"
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$BASE_SHA"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "$BASE_SHA"
bash /home/check_git_changes.sh

# --- dependency work below; the tree may legitimately become dirty from here ---

# pnpm version comes from the commit itself, so it can never drift from the tree
# being built. The +sha512 integrity suffix is stripped.
PNPM_VERSION="$(jq -r '.packageManager // ""' package.json \\
    | sed -e 's/^pnpm@//' -e 's/+.*$//')"
if [ -z "$PNPM_VERSION" ]; then
    PNPM_VERSION="{FALLBACK_PNPM}"
fi
echo "[prepare] pnpm $PNPM_VERSION"
npm install -g "pnpm@$PNPM_VERSION"

# Filtered: only the tested packages and their dependency closures. A root
# install would resolve the whole monorepo and add ~7 GB no test imports.
# Not tolerated: a failed install must abort the build rather than surface three
# stages later as an empty report that scores as vacuously valid.
{_install_command(pr.test_patch)} 2>&1 | tail -40

{_build_commands(pr.test_patch, strict=True)}

# The pnpm store duplicates whatever the install could not hard-link. node_modules
# keeps its own links, so dropping the store here is safe and never re-fetched:
# the graded stages install nothing.
pnpm store prune 2>/dev/null || true
rm -rf "$(pnpm store path 2>/dev/null || echo /root/.local/share/pnpm/store)" || true
rm -rf /root/.npm /root/.cache "{REPO_DIR}/.turbo" || true

# Hard gate, last and non-tolerant. It asserts the test runner is executable in
# the package under test, not merely that the package resolves.
if ! pnpm --filter './{gate_pkg}' exec vitest --version > /dev/null 2>&1; then
    echo "[prepare] FATAL: vitest is not runnable in {gate_pkg}" >&2
    exit 1
fi
test -d node_modules || {{ echo "[prepare] FATAL: no node_modules" >&2; exit 1; }}
echo "DEPS_OK"
"""


def _stage_script(pr: PullRequest, apply_block: str) -> str:
    """One graded stage. All three build and test identically; only the patches
    applied differ, so a FAIL->PASS transition can only come from the patches.

    No dependency install here on purpose: prepare.sh installed at build time, so
    grading needs no network and all three stages see one resolved dependency set.
    """
    services = []
    if _needs_postgres(pr.test_patch):
        services.append("start_postgres || true")
    if _needs_dynamodb(pr.test_patch):
        services.append("start_dynamodb || true")
    services_block = "\n".join(services) if services else "true"

    return f"""{SHEBANG}
set -u
source /home/services.sh
{services_block}

cd {REPO_DIR}

{apply_block}

{_build_commands(pr.test_patch)}

{_test_commands(pr.test_patch)}
"""


_APPLY_NOTHING = "# base tree: no patch applied"

_APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
fi"""

_APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
    git apply --whitespace=nowarn --reject /home/fix.patch || true
fi"""


def _pr_files(pr: PullRequest) -> list[File]:
    return [
        File(".", "fix.patch", pr.fix_patch),
        File(".", "test.patch", pr.test_patch),
        File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
        File(".", "prepare.sh", _prepare_sh(pr)),
        File(".", "run.sh", _stage_script(pr, _APPLY_NOTHING)),
        File(".", "test-run.sh", _stage_script(pr, _APPLY_TEST)),
        File(".", "fix-run.sh", _stage_script(pr, _APPLY_FIX)),
    ]


class MastraSharedPRImage(Image):
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
        return MastraSharedBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        dep = self.dependency()
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        # The prune keeps only the base commit's history. It opens with a
        # commit-scoped detach, never `git reset` or `git clean`: prepare.sh is
        # allowed to have modified tracked files, and those edits must survive.
        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{copy_commands}
RUN chmod +x /home/check_git_changes.sh /home/prepare.sh \\
    /home/run.sh /home/test-run.sh /home/fix-run.sh

RUN bash /home/prepare.sh

WORKDIR {REPO_DIR}

RUN set -eux; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
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


@Instance.register(ORG, ERA)
class MastraShared3642To7606(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:  # type: ignore[override]
        return MastraSharedPRImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
