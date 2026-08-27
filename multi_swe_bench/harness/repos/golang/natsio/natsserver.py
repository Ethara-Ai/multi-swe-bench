import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- GOPATH-era constants -------------------------------------------------
# nats-server PR 638 (merged 2018-03-16) predates Go modules by ~5 months, so
# the repo at this base commit has NO go.mod -- only a vendor/ tree. Verified
# with `git ls-tree` at dd3dccc5: no go.mod, vendor/ present.
#
# Two consequences drive everything below:
#
#  1. Modern Go defaults to GO111MODULE=on and refuses to build without a
#     go.mod ("cannot find main module"). GOPATH mode must be forced OFF.
#     Verified in Docker on golang:1.24: with GO111MODULE=off the whole
#     package list resolves and every package compiles.
#
#  2. In 2018 this project was still named **gnatsd**, and the source imports
#     itself as `github.com/nats-io/gnatsd/server`. GOPATH resolves packages
#     by import path, so the tree must appear at
#     $GOPATH/src/github.com/nats-io/gnatsd -- NOT .../nats-server. The
#     harness clones to /home/nats-server and that path is fixed by
#     DockerfileEnhancer._standardize_repo_fetch, so a symlink bridges the two.
#     Verified in Docker: Go resolves through the symlink and all packages
#     compile.
_GO_IMPORT_DIR = "/go/src/github.com/nats-io/gnatsd"

# Test-only dependencies that are NOT in the repo vendor/ tree. Without these
# BOTH packages that this PR touches fail to build:
#   server/client_test.go  -> cannot find package "github.com/nats-io/go-nats"
#   test/*_test.go         -> same
# and surefire-style output is empty, so the instance yields 0 tests.
# `go get` cannot be used to fetch them: under GO111MODULE=off modern Go
# refuses ("modules disabled by GO111MODULE=off"), so they are git-cloned.
# v1.3.0 is the release current as of this PR's March-2018 base commit;
# pinning it keeps the image reproducible and API-compatible with the era.
_GO_NATS_TAG = "v1.3.0"

# --- Expired-fixture repair ------------------------------------------------
# The repo's checked-in test PKI expired in **November 2019**:
#     ca.pem          notAfter=Nov  4 23:06:17 2019 GMT
#     server-cert.pem notAfter=Nov  4 23:06:34 2019 GMT
#     client-cert.pem notAfter=Nov  4 23:10:47 2019 GMT
#     srva-cert.pem   notAfter=Nov  7 22:08:30 2019 GMT
#     srvb-cert.pem   notAfter=Nov  7 22:08:37 2019 GMT
# Every TLS test therefore fails purely because of wall-clock time, not because
# of anything in the code under test. Two of them (TestTLSClusterConfig,
# TestBasicTLSClusterPubSub) failed outright; verified in Docker that with a
# freshly-signed PKI they both pass (`ok github.com/nats-io/gnatsd/test 0.112s`).
#
# The repo ships ca.pem but NOT ca-key.pem, so the existing leaves cannot be
# re-signed by the original CA. Instead the whole chain is regenerated: a new
# self-signed CA, then new leaf certs that **reuse the repo's existing private
# keys** and preserve every subject DN and SAN exactly as the fixtures had them
# (CN=localhost + DNS:localhost,IP:127.0.0.1 for the server; CN=nats-client for
# the client; CN=nats-cluster + the same SANs for srva/srvb). Only the validity
# window changes, so the tests exercise real TLS rather than being skipped.
#
# Written without a shell function on purpose: this constant is interpolated
# into prepare.sh via str.format(), and literal `{`/`}` would be parsed as
# format placeholders.
_CERT_REGEN = r"""
CERTS=/go/src/github.com/nats-io/gnatsd/test/configs/certs
CA_DN="/C=US/ST=CA/L=San Francisco/O=Apcera Inc/OU=nats.io/CN=localhost/emailAddress=derek@nats.io"
openssl req -x509 -new -nodes -newkey rsa:2048 -keyout "$CERTS/ca-key.pem" \
    -out "$CERTS/ca.pem" -days 7300 -sha256 -subj "$CA_DN" >/dev/null 2>&1

printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\n' > /tmp/ext_srv.cnf
printf 'extendedKeyUsage=serverAuth,clientAuth\n' > /tmp/ext_cli.cnf

openssl req -new -key "$CERTS/server-key.pem" -out /tmp/server.csr -subj "/CN=localhost" >/dev/null 2>&1
openssl x509 -req -in /tmp/server.csr -CA "$CERTS/ca.pem" -CAkey "$CERTS/ca-key.pem" \
    -CAcreateserial -out "$CERTS/server-cert.pem" -days 7300 -sha256 -extfile /tmp/ext_srv.cnf >/dev/null 2>&1

openssl req -new -key "$CERTS/client-key.pem" -out /tmp/client.csr -subj "/CN=nats-client" >/dev/null 2>&1
openssl x509 -req -in /tmp/client.csr -CA "$CERTS/ca.pem" -CAkey "$CERTS/ca-key.pem" \
    -CAcreateserial -out "$CERTS/client-cert.pem" -days 7300 -sha256 -extfile /tmp/ext_cli.cnf >/dev/null 2>&1

openssl req -new -key "$CERTS/srva-key.pem" -out /tmp/srva.csr -subj "/CN=nats-cluster" >/dev/null 2>&1
openssl x509 -req -in /tmp/srva.csr -CA "$CERTS/ca.pem" -CAkey "$CERTS/ca-key.pem" \
    -CAcreateserial -out "$CERTS/srva-cert.pem" -days 7300 -sha256 -extfile /tmp/ext_srv.cnf >/dev/null 2>&1

openssl req -new -key "$CERTS/srvb-key.pem" -out /tmp/srvb.csr -subj "/CN=nats-cluster" >/dev/null 2>&1
openssl x509 -req -in /tmp/srvb.csr -CA "$CERTS/ca.pem" -CAkey "$CERTS/ca-key.pem" \
    -CAcreateserial -out "$CERTS/srvb-cert.pem" -days 7300 -sha256 -extfile /tmp/ext_srv.cnf >/dev/null 2>&1

# The CA private key must NOT survive into the shipped image, and ca.srl is
# build noise. The four leaf certs plus the regenerated ca.pem are all the
# tests need.
rm -f "$CERTS/ca-key.pem" "$CERTS/ca.srl" /tmp/*.csr /tmp/ext_srv.cnf /tmp/ext_cli.cnf
openssl x509 -checkend 0 -noout -in "$CERTS/server-cert.pem"
"""

# --- The one test that is genuinely unrunnable ------------------------------
# TestTLSCloseClientConnection HANGS rather than failing: it prints
# "!!!! closeConnection is blocked, test will hang !!!" and then sits until Go's
# timeout fires. A Go test timeout PANICS, which kills the entire package test
# binary -- so every test after it in the `server` package never executes. That
# is what silently produced an empty f2p on the first Data6 build: the PR's
# target test, TestRoutedQueueUnsubscribe, lives in server/routes_test.go, and
# Go walks test files in sorted order, so client_test.go's hang landed first.
#
# This is NOT the expired-cert problem. Verified in Docker with a freshly-signed
# PKI: the test still hangs and still burns the full timeout, with the stack
# trace pointing into server.go:307 / RunServer -- a deadlock in the 2018 server
# code under the Go 1.24 runtime, unrelated to the fixtures.
#
# Scope of the skip is deliberately ONE test, not a `TLS` pattern. An earlier
# draft of this fix skipped everything matching /TLS/, which would have silenced
# ~24 tests; once the certs are valid almost all of them pass, so that would
# have thrown away real coverage to work around a single deadlock.
# --- Non-deterministic test, excluded to keep the instance reproducible -----
# TestRequestsAcrossRoutes is intrinsically flaky in a container: it issues a
# request/reply across a cluster route and intermittently trips its own client
# timeout ("Received an error on Request test [0]: nats: timeout",
# client_cluster_test.go:310).
#
# It is NOT broken by fix.patch. Measured in Docker, running the test ALONE so
# there is no cross-package contention:
#     baseline, no patch applied : 19 PASS / 1 FAIL  (20 runs)
#     with test.patch+fix.patch  : 12 PASS / 2 FAIL  (14 runs)
# Identical failure message in both states -- a ~5-15% dice roll independent of
# the patch.
#
# On the first clean Data6 build it happened to land pass/pass/FAIL across the
# three stages, which blocks resolution: an instance resolves only if the f2p
# tests flip to PASS *and* previously-passing tests stay passing. Leaving it in
# would ship an instance that spuriously fails ~10% of the time for reasons
# having nothing to do with the agent being graded.
#
# Anchored deliberately. Go's -skip is an UNANCHORED regexp match, and this repo
# also has TestRequestsAcrossRoutesToQueues -- a different, stable test that an
# unanchored pattern would silently take out too.
_SKIP_TESTS = "^(TestTLSCloseClientConnection|TestRequestsAcrossRoutes)$"

# Explicit timeout, well above the ~45s the full suite now needs, so that any
# future hang fails in bounded time instead of quietly consuming Go's 10-minute
# default and truncating the package the way the original run did.
_GO_TEST_TIMEOUT = "600s"

# Defined once and reused verbatim by run.sh, test-run.sh and fix-run.sh, so the
# three graded stages differ ONLY by which patch was applied. If the command
# varied between stages, a FAIL->PASS transition could come from the command
# rather than from the fix, and the f2p/n2p signal would be meaningless.
_GO_TEST_CMD = (
    f"go test -v -count=1 -timeout {_GO_TEST_TIMEOUT} "
    f"-skip '{_SKIP_TESTS}' $PACKAGES"
)


class natsserverImageBase(Image):
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
        # Pinned, and multi-arch: `docker manifest inspect golang:1.24` lists
        # both linux/amd64 and linux/arm64. Despite being far newer than the
        # 2018 code, it builds this tree correctly once GOPATH mode is forced
        # (verified in Docker) -- preferred over an EOL golang:1.10 image,
        # which would be unpatched and is not published for arm64.
        return "golang:1.24"

    def image_tag(self) -> str:
        # Per-PR, NOT a shared "base" tag. The hardening block injected into
        # the rendered base Dockerfile detaches at one ${BASE_COMMIT}, deletes
        # every other ref and gc-prunes unreachable objects, then asserts
        # rev-list --all == rev-list HEAD. A tag shared across PRs would stay
        # permanently pinned to whichever PR built it FIRST, and any second PR
        # reusing it would find its own base commit already pruned away.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        # NOTE ON ORDERING: the GOPATH scaffolding below must come BEFORE the
        # clone line. DockerfileEnhancer._standardize_repo_fetch() replaces that
        # clone line with the parameterized clone + checkout + hardening block +
        # CMD, so anything placed after it in this template would land after CMD
        # and never execute. Creating the symlink before its target exists is
        # fine -- it dangles until the clone materialises /home/<repo>.
        return f"""FROM {image_name}

{self.global_env}

ENV GOPATH=/go
ENV GO111MODULE=off
ENV CGO_ENABLED=0

WORKDIR /home/

RUN mkdir -p /go/src/github.com/nats-io && \\
    git clone --quiet --branch {_GO_NATS_TAG} --depth 1 \\
        https://github.com/nats-io/go-nats.git /go/src/github.com/nats-io/go-nats && \\
    git clone --quiet --depth 1 \\
        https://github.com/nats-io/nuid.git /go/src/github.com/nats-io/nuid && \\
    ln -sfn /home/{self.pr.repo} {_GO_IMPORT_DIR}

{code}

{self.clear_env}

"""


class natsserverImageDefault(Image):
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
        return natsserverImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Every script cds to the GOPATH path rather than /home/<repo>. They are
        # the same directory (symlinked in the base image), but Go only resolves
        # the `github.com/nats-io/gnatsd/...` self-imports when the tree is
        # reached through its GOPATH import path. git operations (apply, reset,
        # status) work identically through the symlink.
        #
        # The package list deliberately does NOT filter out the `test` package.
        # The previous config excluded it with `grep -v '/test$'`, but this PR
        # modifies test/routes_test.go -- filtering it silently dropped half the
        # PR's tests from every stage. It only failed to build because go-nats
        # was missing, which the base image now provides.
        pkg_line = (
            "PACKAGES=$(go list ./... 2>/dev/null | grep -v '/vendor/' "
            '|| echo "./...")'
        )
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd {go_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Re-sign the expired test PKI. This runs AFTER both clean-tree assertions on
# purpose: the certs are tracked files, so regenerating them dirties the working
# tree, and doing it earlier would make check_git_changes.sh fail. It also has
# to be here rather than in the base image, because the `git reset --hard`
# above would revert anything the base image had written. No `|| true` -- this
# is deterministic local openssl work with no network, so a failure here is a
# real problem and should stop the build rather than silently ship dead certs.
{cert_regen}

# Warm the build cache so the three graded runs do not each pay a full
# compile. `|| true` because a warm-up hiccup must not fail the image build --
# the graded runs decide pass/fail, not this.
{pkg_line}
go build $PACKAGES || true

""".format(
                    go_dir=_GO_IMPORT_DIR,
                    sha=self.pr.base.sha,
                    pkg_line=pkg_line,
                    cert_regen=_CERT_REGEN,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {go_dir}
{pkg_line}
{go_test_cmd}

""".format(go_dir=_GO_IMPORT_DIR, pkg_line=pkg_line, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {go_dir}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{pkg_line}
{go_test_cmd}

""".format(go_dir=_GO_IMPORT_DIR, pkg_line=pkg_line, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {go_dir}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{pkg_line}
{go_test_cmd}

""".format(go_dir=_GO_IMPORT_DIR, pkg_line=pkg_line, go_test_cmd=_GO_TEST_CMD),
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


@Instance.register("nats-io", "nats-server")
class natsserver(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return natsserverImageDefault(self.pr, self._config)

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

        # Strip ANSI first: `go test` is usually uncoloured, but CI wrappers and
        # gotestsum are not, and a stray escape sequence silently breaks every
        # anchored pattern below.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Anchored on `go test -v` result lines only:
        #   --- PASS: TestFoo (0.01s)
        #   --- FAIL: TestFoo/subtest (0.00s)
        #   --- SKIP: TestFoo (0.00s)
        #
        # Deliberately NOT a bare `FAIL\s+(\S+)`: go prints a package summary
        # line (`FAIL\tgithub.com/nats-io/gnatsd/server\t0.5s`) for every failing
        # package, and a broad pattern turns that package PATH into a phantom
        # failing "test", inflating failed_count with entries that are not tests.
        # The previous config carried exactly that pattern.
        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))

        # Enforce TestResult's disjointness invariants explicitly rather than
        # relying on line order. TestResult.__post_init__ raises ValueError if
        # any two sets intersect, which would crash the whole run.
        # Precedence: a failure anywhere wins, then skip, then pass -- so a test
        # that is retried and fails is never also counted as passing.
        # (The previous config only recorded a SKIP when the test was ALREADY in
        # failed_tests, which both suppressed real skips and could produce a
        # failed/skipped intersection -> ValueError.)
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )