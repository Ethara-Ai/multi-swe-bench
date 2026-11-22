import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "opentelemetry-collector_16166_to_11494"
_GO_IMAGE = "golang:1.26-bookworm"
_REPO_DIR = "/home/opentelemetry-collector"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")
_BUILD_FAILED = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]")
_PKG_DONE = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+)")

_RUN_ENV = """export CI=true
export TZ=UTC
export GOFLAGS=-buildvcs=false
export GOTOOLCHAIN=local
export GOPROXY=https://proxy.golang.org,direct
export GOSUMDB=sum.golang.org
export GOMAXPROCS=4
export GORACE=halt_on_error=1
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_GO_TEST = """#!/bin/bash
set -eo pipefail

REPO_DIR="$1"
shift

WORK="$(mktemp -d)"
declare -A MODULE_PKGS

for DIR in "$@"; do
  TARGET="$REPO_DIR/$DIR"
  if [ ! -d "$TARGET" ]; then
    echo "=== skipping package $DIR (directory does not exist) ==="
    continue
  fi
  MOD_DIR="$TARGET"
  while [ "$MOD_DIR" != "/" ] && [ ! -f "$MOD_DIR/go.mod" ]; do
    MOD_DIR="$(dirname "$MOD_DIR")"
  done
  if [ ! -f "$MOD_DIR/go.mod" ]; then
    echo "=== skipping package $DIR (no owning go.mod) ==="
    continue
  fi
  if [ "$TARGET" = "$MOD_DIR" ]; then
    REL="."
  else
    REL="./${TARGET#$MOD_DIR/}"
  fi
  MODULE_PKGS["$MOD_DIR"]="${MODULE_PKGS[$MOD_DIR]:-} $REL"
done

STATUS=0

for MOD_DIR in "${!MODULE_PKGS[@]}"; do
  PKGS="$(echo "${MODULE_PKGS[$MOD_DIR]}" | xargs -n1 | sort -u | xargs)"
  echo "=== testing module $MOD_DIR packages $PKGS ==="
  cd "$MOD_DIR"
  MOD_PATH="$(go list -m 2>/dev/null | head -1)"
  OUT="$(go test -v -count=1 -timeout 25m $PKGS 2>&1)" || STATUS=$?
  printf '%s\\n' "$OUT" > "$WORK/out.txt"
  cat "$WORK/out.txt"
  awk '$1 == "FAIL" && ($3 == "[build" || $3 == "[setup") { print $2 }' "$WORK/out.txt" > "$WORK/broken.txt"
  while read -r BROKEN; do
    if [ "$BROKEN" = "$MOD_PATH" ]; then
      BROKEN_DIR="$MOD_DIR"
    else
      BROKEN_DIR="$MOD_DIR/${BROKEN#$MOD_PATH/}"
    fi
    if ! ls "$BROKEN_DIR"/*_test.go > /dev/null 2>&1; then
      continue
    fi
    echo "=== enumerating unrunnable tests in $BROKEN (package failed to build) ==="
    awk 'match($0, /^func Test[A-Za-z0-9_]*/) { print substr($0, RSTART + 5, RLENGTH - 5) }' "$BROKEN_DIR"/*_test.go | sort -u > "$WORK/names.txt"
    while read -r TNAME; do
      echo "--- FAIL: $TNAME (0.00s)"
    done < "$WORK/names.txt"
    printf 'FAIL\\t%s\\t0.000s\\n' "$BROKEN"
  done < "$WORK/broken.txt"
done

rm -rf "$WORK"
exit $STATUS
"""


_HELPERS = (
    "cat > /home/go-test.sh <<'__GOTEST_EOF__'\n"
    + _GO_TEST
    + "__GOTEST_EOF__\n"
)


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _test_dirs(pr: PullRequest) -> list[str]:
    dirs = []
    for path in _patch_files(pr.test_patch) + _patch_files(pr.fix_patch):
        if not path.endswith("_test.go"):
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        if directory not in dirs:
            dirs.append(directory)
    return sorted(dirs)


def _run_tests(pr: PullRequest) -> str:
    dirs = _test_dirs(pr)
    if not dirs:
        return ""
    return f"bash /home/go-test.sh {_REPO_DIR} {' '.join(dirs)}\n"


class OtelCollector16166To11494ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CI=true \\
    CGO_ENABLED=1 \\
    GOFLAGS=-buildvcs=false \\
    GOTOOLCHAIN=local \\
    GOPATH=/go \\
    GOMODCACHE=/go/pkg/mod \\
    GOCACHE=/go/cache

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl build-essential make patch jq \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN mkdir -p /go/pkg/mod /go/cache

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class OtelCollector16166To11494ImageDefault(Image):
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
        return OtelCollector16166To11494ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        run_tests = _run_tests(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd {repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
git submodule update --init --recursive
bash /home/check_git_changes.sh

{run_env}
{helpers}
find {repo_dir} -name go.mod -not -path "*/vendor/*" -print0 | while IFS= read -r -d '' GOMOD; do
    MOD_DIR="$(dirname "$GOMOD")"
    if ! (cd "$MOD_DIR" && go mod download all) > /tmp/go-mod-download.log 2>&1; then
        cat /tmp/go-mod-download.log
        if ! grep -q "requires go >= " /tmp/go-mod-download.log; then
            echo "prepare.sh: go mod download failed in $MOD_DIR"
            exit 1
        fi
        echo "prepare.sh: go mod download skipped in $MOD_DIR (a dependency needs a newer Go toolchain than this image)"
    fi
done
git diff --name-only -z -- '*go.sum' | xargs -0 -r git checkout --

cd {repo_dir}
go version
go build ./...
echo "DEPS_OK go $(go version | awk '{{print $3}}')"
""".format(
                    repo_dir=_REPO_DIR,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    helpers=_HELPERS,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
{run_tests}""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch

bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run_tests.sh
""".format(repo_dir=_REPO_DIR, run_env=_RUN_ENV),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR {_REPO_DIR}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("open-telemetry", _INTERVAL)
class OPENTELEMETRY_COLLECTOR_16166_TO_11494(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OtelCollector16166To11494ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        pending: list[tuple[str, str]] = []

        def record(status: str, name: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            elif status == "SKIP":
                if name in failed_tests or name in passed_tests:
                    return
                skipped_tests.add(name)

        for raw in _ANSI.sub("", log).splitlines():
            line = raw.strip()

            match = _RESULT.match(line)
            if match:
                pending.append((match.group(1), match.group(2)))
                continue

            match = _BUILD_FAILED.match(line)
            if match:
                package = match.group(1)
                for status, name in pending:
                    record("FAIL", f"{package}::{name}")
                pending.clear()
                continue

            match = _PKG_DONE.match(line)
            if match:
                package = match.group(1)
                for status, name in pending:
                    record(status, f"{package}::{name}")
                pending.clear()

        for status, name in pending:
            record(status, name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
