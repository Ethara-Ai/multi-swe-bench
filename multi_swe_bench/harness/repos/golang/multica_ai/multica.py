import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "multica"
GO_IMAGE = "golang:1.26.10-bookworm"
BASE_TAG = "base-7234_to_9455"

NODE_VERSION = "22.23.3"
NODE_SHA256_X64 = "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de"
NODE_SHA256_ARM64 = "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f"
PNPM_VERSION = "10.28.2"
PG_MAJOR = "17"

GO_PKG_PREFIX = "github.com/multica-ai/multica/"

_ENV = """\
export CI=true
export GOTOOLCHAIN=local
export CGO_ENABLED=1
export DATABASE_URL="postgres://multica:multica@localhost:5432/multica?sslmode=disable"
export REDIS_TEST_URL="redis://localhost:6379/1"
export NODE_OPTIONS="--max-old-space-size=4096"
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
"""

_DEP_FILES = re.compile(r"(^|/)(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml)$")
_VITEST_FILE = re.compile(r"^(apps|packages)/[^/]+/.+\.test\.(ts|tsx|mts|js|jsx|mjs)$")
_NODE_TEST_FILE = re.compile(r"^scripts/.+\.test\.mjs$")


def _patch_paths(patch: str) -> list[str]:
    paths: set[str] = set()
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)", patch, re.M):
        paths.update((a, b))
    return sorted(paths)


def _go_dirs(test_patch: str) -> list[str]:
    dirs: set[str] = set()
    for path in _patch_paths(test_patch):
        if not path.endswith(".go") or not path.startswith("server/"):
            continue
        if "/testdata/" in path:
            continue
        rel = path[len("server/") :]
        dirs.add(rel.rsplit("/", 1)[0] if "/" in rel else ".")
    return sorted(dirs)


def _vitest_targets(test_patch: str) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for path in _patch_paths(test_patch):
        if _VITEST_FILE.match(path):
            workspace = "/".join(path.split("/", 2)[:2])
            targets.setdefault(workspace, []).append(path)
    return targets


def _node_test_files(test_patch: str) -> list[str]:
    return [p for p in _patch_paths(test_patch) if _NODE_TEST_FILE.match(p)]


def _quoted(items: list[str]) -> str:
    return " ".join(f"'{i}'" for i in items)


def _gate_cmd(test_patch: str) -> str:
    lines = [f"cd /home/{REPO_DIR}/server", "go build ./..."]
    go_dirs = _go_dirs(test_patch)
    if go_dirs:
        lines.append(
            f"""\
PKGS=""
for d in {_quoted(go_dirs)}; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then
    PKGS="$PKGS ./$d"
  fi
done
if [ -n "$PKGS" ]; then
  go test -count=1 -run '^$' $PKGS
fi"""
        )
    workspaces = sorted(_vitest_targets(test_patch)) or ["packages/core"]
    for workspace in workspaces:
        lines.append(f"pnpm --dir '/home/{REPO_DIR}/{workspace}' exec vitest --version")
    if _node_test_files(test_patch):
        lines.append("node --version")
    lines.append("echo DEPS_OK")
    return "\n".join(lines)


def _test_cmd(test_patch: str) -> str:
    blocks = []

    go_dirs = _go_dirs(test_patch)
    if go_dirs:
        blocks.append(
            f"""\
bash /home/start_services.sh
cd /home/{REPO_DIR}/server
go run ./cmd/migrate up
PKGS=""
for d in {_quoted(go_dirs)}; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then
    PKGS="$PKGS ./$d"
  fi
done
if [ -n "$PKGS" ]; then
  go test -v -count=1 -p 2 -parallel 2 -timeout 30m $PKGS || RC=$?
fi"""
        )

    for i, (workspace, files) in enumerate(sorted(_vitest_targets(test_patch).items())):
        out = f"/tmp/msb-results/vitest-{i}.json"
        blocks.append(
            f"""\
cd /home/{REPO_DIR}
FILES=""
for f in {_quoted(files)}; do
  if [ -f "$f" ]; then
    FILES="$FILES /home/{REPO_DIR}/$f"
  fi
done
if [ -n "$FILES" ]; then
  pnpm --dir '{workspace}' exec vitest run --reporter=json --outputFile='{out}' $FILES || RC=$?
  node /home/emit_results.mjs vitest '{out}' $FILES
fi"""
        )

    for i, path in enumerate(_node_test_files(test_patch)):
        out = f"/tmp/msb-results/node-test-{i}.tap"
        blocks.append(
            f"""\
cd /home/{REPO_DIR}
if [ -f '{path}' ]; then
  node --test --test-reporter=tap --test-reporter-destination='{out}' '{path}' || RC=$?
  node /home/emit_results.mjs tap '{out}' '/home/{REPO_DIR}/{path}'
fi"""
        )

    body = "\n\n".join(blocks)
    return f"""\
RC=0
rm -rf /tmp/msb-results
mkdir -p /tmp/msb-results

{body}

exit $RC"""


_CHECK_GIT_CHANGES = """\
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
"""

_START_SERVICES = """\
#!/bin/bash
set -euo pipefail

if ! pg_isready -q -h localhost -p 5432; then
  pg_ctlcluster PG_MAJOR main start
fi
for _ in $(seq 1 60); do
  pg_isready -q -h localhost -p 5432 && break
  sleep 1
done
pg_isready -h localhost -p 5432

if ! redis-cli -p 6379 ping >/dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --save '' --appendonly no
fi
for _ in $(seq 1 30); do
  redis-cli -p 6379 ping >/dev/null 2>&1 && break
  sleep 1
done
redis-cli -p 6379 ping
""".replace("PG_MAJOR", PG_MAJOR)

_EMIT_RESULTS = """\
import { existsSync, readFileSync } from "node:fs";
import { relative } from "node:path";

const ROOT = "/home/REPO_DIR";
const [kind, resultFile, ...requested] = process.argv.slice(2);
const rel = (p) => relative(ROOT, p);
const emit = (status, name) => console.log(`MSB_TEST|${status}|${name}`);
const seen = new Set();

function read() {
  if (!existsSync(resultFile)) return null;
  try {
    return readFileSync(resultFile, "utf8");
  } catch {
    return null;
  }
}

function vitest() {
  const raw = read();
  if (raw === null) return;
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return;
  }
  for (const file of data.testResults ?? []) {
    const path = rel(file.name);
    seen.add(path);
    let failed = false;
    for (const a of file.assertionResults ?? []) {
      const name = [path, ...(a.ancestorTitles ?? []), a.title].join(" > ");
      if (a.status === "passed") {
        emit("PASS", name);
      } else if (a.status === "failed") {
        emit("FAIL", name);
        failed = true;
      } else {
        emit("SKIP", name);
      }
    }
    if (file.status === "failed" && !failed) emit("FAIL", path);
  }
}

function tap() {
  const raw = read();
  if (raw === null) return;
  const path = rel(requested[0]);
  seen.add(path);
  const stack = [];
  for (const line of raw.split("\\n")) {
    let m = line.match(/^(\\s*)# Subtest: (.*)$/);
    if (m) {
      const depth = Math.floor(m[1].length / 4);
      stack.length = depth;
      stack[depth] = m[2];
      continue;
    }
    m = line.match(/^(\\s*)(not ok|ok) \\d+ - (.*?)(?:\\s+#\\s+(SKIP|TODO)\\b.*)?$/i);
    if (!m) continue;
    const depth = Math.floor(m[1].length / 4);
    const name = [path, ...stack.slice(0, depth), m[3]].join(" > ");
    if (m[4]) emit("SKIP", name);
    else if (m[2] === "ok") emit("PASS", name);
    else emit("FAIL", name);
  }
}

if (kind === "vitest") vitest();
else if (kind === "tap") tap();

for (const file of requested) {
  if (!seen.has(rel(file))) emit("FAIL", rel(file));
}
""".replace("REPO_DIR", REPO_DIR)


class MulticaImageBase(Image):
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

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=1 \\
    GOPROXY=https://proxy.golang.org,direct \\
    GORACE=halt_on_error=1 \\
    CI=true

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential curl pkg-config python3 xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN install -d /usr/share/postgresql-common/pgdg && \\
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \\
        https://www.postgresql.org/media/keys/ACCC4CF8.asc && \\
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \\
        > /etc/apt/sources.list.d/pgdg.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
        postgresql-{PG_MAJOR} postgresql-client-{PG_MAJOR} redis-server \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    pg_lsclusters | grep -q '^{PG_MAJOR} *main' || pg_createcluster {PG_MAJOR} main; \\
    pg_ctlcluster {PG_MAJOR} main start; \\
    su postgres -c "psql -v ON_ERROR_STOP=1 -c \\"CREATE ROLE multica WITH LOGIN SUPERUSER PASSWORD 'multica';\\""; \\
    su postgres -c "createdb -O multica multica"; \\
    pg_ctlcluster {PG_MAJOR} main stop

RUN arch="${{TARGETARCH:-$(dpkg --print-architecture)}}" \\
    && case "$arch" in \\
        amd64) node_arch=x64; sum={NODE_SHA256_X64} ;; \\
        arm64) node_arch=arm64; sum={NODE_SHA256_ARM64} ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac \\
    && curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" -o /tmp/node.tar.xz \\
    && echo "$sum  /tmp/node.tar.xz" | sha256sum -c - \\
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \\
    && rm -f /tmp/node.tar.xz \\
    && node --version \\
    && npm --version

RUN npm install -g "pnpm@{PNPM_VERSION}" && pnpm --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class MulticaImageDefault(Image):
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
        return MulticaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = _test_cmd(self.pr.test_patch)

        resync = ""
        if any(_DEP_FILES.search(p) for p in _patch_paths(self.pr.fix_patch)):
            resync = "pnpm install --frozen-lockfile\n"

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK
trap 'pg_ctlcluster PG_MAJOR main stop -m fast >/dev/null 2>&1 || true; redis-cli -p 6379 shutdown nosave >/dev/null 2>&1 || true' EXIT

pnpm install --frozen-lockfile
cd /home/REPO/server
go mod download
bash /home/start_services.sh
go run ./cmd/migrate up

GATE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
RESYNC
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("RESYNC\n", resync)
                .replace("TEST_CMD", test_cmd)
                .replace("GATE_CMD", _gate_cmd(self.pr.test_patch))
                .replace("PG_MAJOR", PG_MAJOR)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("REPO", repo)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "start_services.sh", _START_SERVICES),
            File(".", "emit_results.mjs", _EMIT_RESULTS),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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

{self.clear_env}
"""


@Instance.register("multica-ai", "multica")
class Multica(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MulticaImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

        re_msb = re.compile(r"^MSB_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")
        re_go = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        re_pkg = re.compile(r"^(ok|FAIL)\s+" + re.escape(GO_PKG_PREFIX) + r"(\S+)")

        pending: list[tuple[str, str]] = []

        for line in log.splitlines():
            m = re_msb.match(line)
            if m:
                buckets[m.group(1)].add(m.group(2))
                continue
            m = re_go.match(line)
            if m:
                pending.append((m.group(1), m.group(2)))
                continue
            m = re_pkg.match(line)
            if m:
                pkg = m.group(2)
                for status, name in pending:
                    buckets[status].add(f"{pkg}::{name}")
                if m.group(1) == "FAIL" and not any(s == "FAIL" for s, _ in pending):
                    failed_tests.add(pkg)
                pending.clear()

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
