import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# amir20/dozzle era "dozzle_5384_to_4775" (PRs #4775 .. #5384).
#
# Single shared base for the whole era. The `go` directive across the era's base
# commits spans 1.26.3 (#4775) .. 1.27.2 (#5384); the Go toolchain is backward
# compatible, so the newest minor in the era builds every PR -> one base image.
_GO_IMAGE = "golang:1.27-bookworm"

# Era shard suffix. Keeps this base tag distinct from the sibling era file
# (dozzle.py, PRs #2478 .. #4691) which tags its base `base` (Check 2F).
_ERA = "5384_to_4775"


# ---------------------------------------------------------------------------
# Build-context scripts. COPY'd into the per-PR image; `.format(pr=...)` fills
# in the PR's own repo name and base.sha, so nothing below is PR-specific code.
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# prepare.sh pins the worktree at this PR's base.sha and provisions the two
# build inputs dozzle's Go packages need before any test can compile. It never
# applies fix.patch or test.patch -- that is run.sh / test-run.sh / fix-run.sh.
_PREPARE_SH = """#!/bin/bash
set -e

git config --global --add safe.directory '*'

# --- Section 1: pin the worktree at this PR's base commit -------------------
cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

# --- Section 2: provisioning (no clean-tree assertions past this point) ------
# main.go carries `//go:embed all:dist`, so the package does not compile without
# a dist/ tree. Upstream's `make fake_assets` target writes exactly this stub
# instead of running the (Node/bun) asset build, which is out of scope here.
# `dist` is gitignored, so the stub stays untracked and survives git gc.
mkdir -p dist
echo "assets build was skipped" > dist/index.html

# main.go also carries `//go:embed shared_cert.pem shared_key.pem`, and the
# agent client tests load the pair at run time. Upstream's `shared_key.pem` /
# `shared_cert.pem` Makefile targets generate them; `*.pem` is gitignored.
openssl genpkey -algorithm Ed25519 -out shared_key.pem
openssl req -new -key shared_key.pem -out shared_request.csr \\
    -subj "/C=US/ST=California/L=San Francisco/O=Dozzle"
openssl x509 -req -in shared_request.csr -signkey shared_key.pem \\
    -out shared_cert.pem -days 1825
rm -f shared_request.csr

# Build with the toolchain this commit declares, not the newest one installed.
# golang.org/x/net gates its http2 internals on the toolchain version, so a
# newer toolchain can fail to compile an older x/net pin. Reading the directive
# out of the checked-out go.mod keeps this generic across the whole era.
export GOTOOLCHAIN=go$(awk '/^go /{{print $2; exit}}' go.mod)
go version

# Warm the module cache so the graded runs do not need the network. This also
# caches the derived toolchain into the image. Tolerated because the gate below
# re-checks the real requirement (everything compiles).
go mod download all || true

# --- Section 3: hard gate (non-tolerant; no `|| true` past this line) -------
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -s dist/index.html
test -s shared_cert.pem
test -s shared_key.pem
# `-run '^$'` matches no test name, so this compiles every package and every
# test binary without executing anything.
go test -run '^$' -count=1 ./...
echo "prepare: OK at {pr.base.sha}"
"""

# Shared helpers sourced by all three run scripts, so the test command is
# byte-identical in the run / test-patch / fix-patch stages (Check 3B/4B).
_COMMON_SH = """#!/bin/bash
# Shared helpers for the dozzle run/test/fix scripts.
#
# dozzle's Go suite is fast and hermetic, so every package is graded
# (`go test ./...`) rather than only the packages a patch touches -- that keeps
# the reported test set identical across the three stages. The TypeScript/Vue
# unit tests and the docker-compose Playwright e2e suite are out of scope (they
# need a Node toolchain and a live Docker daemon).

# Non-source paths that occasionally appear in a dozzle patch. Excluding them
# keeps `git apply` from choking on a binary hunk or a lockfile it cannot merge.
# Array, not a string: the glob patterns must reach git unexpanded by the shell.
EXCLUDES=(
  --exclude='*.lock'
  --exclude='*.png'
  --exclude='*.ico'
  --exclude='*.mp4'
  --exclude='*.svg'
  --exclude='*.gif'
  --exclude='*.jpg'
  --exclude='*.jpeg'
  --exclude='*.webp'
  --exclude='*.pdf'
  --exclude='*.woff'
  --exclude='*.woff2'
  --exclude='docs/*'
)

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn "${EXCLUDES[@]}" "$f"
}

# Must match prepare.sh: the toolchain that compiled the module cache at build
# time is the one the graded stages have to use, or the run re-resolves it and
# hits the x/net http2 build-tag mismatch prepare.sh already worked around.
use_declared_toolchain() {
  export GOTOOLCHAIN=go$(awk '/^go /{print $2; exit}' go.mod)
}

run_go_tests() {
  use_declared_toolchain
  echo "=== Running go test on all packages ==="
  go test -v -count=1 -timeout=900s ./...
}
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

run_go_tests
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
run_go_tests
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
source /home/common.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests
"""


class DozzleEraImageBase(Image):
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
        return f"base-{_ERA}"

    def workdir(self) -> str:
        return f"base-{_ERA}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # The leading `# syntax` directive opts this shared base out of
        # DockerfileEnhancer (image.py:317-318). Without it the enhancer would
        # rewrite the clone below into `git checkout ${BASE_COMMIT}` + history
        # prune, pinning the SHARED base to one PR's base.sha and breaking every
        # other PR in the era. The base therefore keeps FULL history and carries
        # no BASE_COMMIT consumer; pinning + scrubbing happen in the PR layer.
        # Because the enhancer is opted out, the proxy/cert/label scaffolding it
        # would have injected is written out in full here.
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
    GOFLAGS=-mod=mod \\
    GOTOOLCHAIN=auto \\
    CGO_ENABLED=1 \\
    CI=true

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
        git ca-certificates openssl build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class DozzleEraImageDefault(Image):
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
        return DozzleEraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH.format(pr=self.pr)),
            File(".", "common.sh", _COMMON_SH),
            File(".", "run.sh", _RUN_SH.format(pr=self.pr)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(pr=self.pr)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(pr=self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
"""


@Instance.register("amir20", "dozzle_5384_to_4775")
class DOZZLE_5384_TO_4775(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DozzleEraImageDefault(self.pr, self._config)

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
        # ANSI first: `go test` is not colorized by default, but a log captured
        # through a colorizing tee would otherwise poison every test name.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Capture only the test name -- the trailing " (0.42s)" timing is
        # variable metadata and must not become part of the identity.
        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        # A package summary line ("ok <import-path>", "FAIL <import-path>",
        # "? <import-path>") closes the block of tests printed above it.
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        # Buffer per package so the import path can be prepended: dozzle has
        # same-named tests in different packages, so the bare Go test name is
        # not globally unique.
        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            # Subtest results are indented ("    --- PASS: Parent/child"), so
            # strip before matching to catch them too.
            line = raw_line.strip()

            pass_match = re_pass.match(line)
            if pass_match:
                pending_pass.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                pending_fail.add(fail_match.group(1))
                continue

            skip_match = re_skip.match(line)
            if skip_match:
                pending_skip.add(skip_match.group(1))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Tests with no trailing summary line (truncated or timed-out log) are
        # still reported rather than silently dropped.
        flush("unknown")

        # TestResult disjointness: a test seen as both passed and failed (flaky
        # retry) counts as failed; a skip never outranks a real result.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# --- Registration keys ------------------------------------------------------
# `Instance.create()` (instance.py:41-51) keys off `number_interval` first, then
# falls back to `org/repo`. The dataset records for this era
# (output/amir20__dozzle.jsonl, PRs #4775 .. #5384) carry neither
# `number_interval` nor `tag`, so every one of them resolves to `amir20/dozzle`.
# This era file is imported LAST from amir20/__init__.py, so it owns that key;
# the sibling era (dozzle.py, PRs #2478 .. #4691) keeps routing through its own
# 83 dash-joined bundle `number_interval` keys, which are untouched here.
Instance.register("amir20", "dozzle")(DOZZLE_5384_TO_4775)
