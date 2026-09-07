import posixpath
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# nats-io/nats-server spans THREE build eras across its 25 PRs (2018 -> 2024).
# This registry ships ONE shared base image + one PR image per PR (1 base + N pr,
# NOT one base per PR).  The per-era differences are decided HERE in Python and
# interpolated into FLAT, standard shell scripts -- there is no `if era` branching
# in any generated prepare.sh/run.sh.  Empirically (golang:1.24):
#
#   Era 1  GOPATH/gnatsd (no go.mod)      PRs 682,698,794,796,893
#          -> `go build ./...` FAILS ("directory prefix . does not contain main
#             module"); needs GOPATH mode + the gnatsd import path + go-nats/nuid
#             + a re-signed test PKI (the 2018 certs expired Nov-2019).
#   Era 2  modules + vendor               PRs 1175..2761
#   Era 3  modules, no vendor             PRs 2973,3365,3679,4105,5281,5829
#          -> BOTH build & test with the plain, standard `go build/test ./...`
#             (module=github.com/nats-io/nats-server/v2); certs valid to 2029/2032,
#             so no fixture repair is needed.
#
# So the 20 module-era PRs share one identical standard script template; only the
# 5 GOPATH-era PRs carry the extra setup, injected by Python.  Everything runs off
# the single shared base (golang:1.24 + a full-history clone).
# =============================================================================

# The 5 GOPATH-era PRs.  Membership here is the ONLY era switch, evaluated in
# Python at generation time -- the shell scripts never test it.
_GOPATH_PRS = frozenset({682, 698, 794, 796, 893})

# GOPATH import path for the 2018 tree, which imports itself as
# github.com/nats-io/gnatsd (the project was still named gnatsd then).
_GO_IMPORT_DIR = "/go/src/github.com/nats-io/gnatsd"

# Release of go-nats current as of the 2018 base commits; pinned for reproducibility.
_GO_NATS_TAG = "v1.3.0"

# --- Expired-fixture repair (GOPATH era only) ------------------------------
# The 2018 checked-in test PKI expired in November 2019, so every TLS test would
# fail purely on wall-clock time.  ca-key.pem is not shipped, so the chain is
# regenerated: a fresh self-signed CA re-signs new leaf certs that REUSE the
# repo's existing private keys and preserve every subject DN and SAN, changing
# only the validity window.  Module-era certs are valid to 2029/2032, so this is
# injected ONLY for the GOPATH PRs (empty string otherwise).
# No literal `{`/`}` on purpose: this is interpolated into prepare.sh via .format().
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

rm -f "$CERTS/ca-key.pem" "$CERTS/ca.srl" /tmp/*.csr /tmp/ext_srv.cnf /tmp/ext_cli.cnf
openssl x509 -checkend 0 -noout -in "$CERTS/server-cert.pem"
"""

# TestTLSCloseClientConnection deadlocks under the modern runtime and burns the
# whole timeout, killing the package binary; TestRequestsAcrossRoutes is a
# container-flaky cross-route request. Both are excluded so the instances stay
# reproducible. Anchored, because unanchored -skip would also take out the stable
# TestRequestsAcrossRoutesToQueues.
_SKIP_TESTS = "^(TestTLSCloseClientConnection|TestRequestsAcrossRoutes)$"

_GO_TEST_TIMEOUT = "1200s"

# Fallback package list (everything except vendor), used only when a PR's test.patch
# touches no .go files -- normally we scope to just the touched packages (see
# natsserverImageDefault._pkg_line), because nats-server's full suite is far too slow
# and flaky to run ./... across three stages for every PR.
_PKG_LINE_ALL = (
    "PACKAGES=$(go list ./... 2>/dev/null | grep -v '/vendor/' " '|| echo "./...")'
)


def _touched_packages(test_patch: str) -> list[str]:
    """Directories of the .go files a test.patch adds/modifies, as ./relative Go
    package paths. The graded f2p/n2p tests live in these files, so running just
    these packages captures every target test (added OR modified) while skipping the
    rest of nats-server's large suite. Same relative paths work in GOPATH and module
    mode. Returns [] if the patch touches no .go file (caller falls back to ./...)."""
    dirs: set[str] = set()
    for line in (test_patch or "").splitlines():
        m = re.match(r"^\+\+\+ b/(.+\.go)\s*$", line)
        if m:
            d = posixpath.dirname(m.group(1))
            dirs.add("./" + d if d else ".")
    return sorted(dirs)


def _target_tests(test_patch: str) -> list[str]:
    """Top-level Test functions the test.patch adds or modifies, within *_test.go
    files. Used to build `go test -run '^(...)$'` so we run ONLY the graded tests,
    not nats-server's whole `./server` package -- which for the 2022+ era exceeds the
    600s Go test timeout, panics, and truncates results (p2p=0, no clean transition).

    Both added (`+func Test...`) and modified (context ` func Test...`) declarations
    are captured, so f2p (modified existing test) and n2p (newly added test) both work.
    Running the top-level Test name also runs all its t.Run subtests. Returns [] if no
    Test decl is found (caller then runs the whole package, relying on the timeout)."""
    names: set[str] = set()
    in_test_file = False
    for line in (test_patch or "").splitlines():
        if line.startswith("+++ b/"):
            in_test_file = line.rstrip().endswith("_test.go")
            continue
        if not in_test_file:
            continue
        # hunk body lines only (added or context), a func declaration line
        if line[:1] in ("+", " "):
            m = re.search(r"func\s+(?:\([^)]*\)\s+)?(Test\w+)\s*\(", line)
            if m:
                names.add(m.group(1))
    return sorted(names)


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
        # Pinned + multi-arch (amd64 + arm64). Builds every era of this tree once
        # GOPATH mode is forced for the 2018 PRs; preferred over an EOL golang:1.10
        # which is unpatched and has no arm64 image.
        return "golang:1.24"

    def image_tag(self) -> str:
        # ONE shared base for all 25 PRs. The repo is cloned with FULL history by
        # clone_repo.sh (a COPY'd script), so each PR image can `git checkout` its
        # own base commit out of this single image. Because the clone lives in a
        # script and not as a `git clone` line in the Dockerfile, the harness
        # DockerfileEnhancer neither rewrites it to a single ${BASE_COMMIT} checkout
        # nor injects its history scrub -- which is exactly what would otherwise pin
        # a shared base to one commit and force one base image per PR.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "clone_repo.sh",
                # Identical for every PR of this repo (hardcoded URLs), so the harness
                # dedupes the base to a single build.
                """#!/bin/bash
set -e
git config --global --add safe.directory '*'
mkdir -p /go/src/github.com/nats-io

# Main repo: FULL history, no checkout, no scrub -- each PR image detaches its own
# base commit out of this shared clone.
git clone --quiet https://github.com/nats-io/nats-server.git /home/nats-server

# GOPATH-era (2018 gnatsd) build deps that are not in that era's vendor tree.
# Harmless for the module-era PRs, which ignore GOPATH entirely.
git clone --quiet --branch v1.3.0 --depth 1 https://github.com/nats-io/go-nats.git /go/src/github.com/nats-io/go-nats
git clone --quiet --depth 1 https://github.com/nats-io/nuid.git /go/src/github.com/nats-io/nuid

# GOPATH import path: the 2018 tree imports itself as github.com/nats-io/gnatsd.
ln -sfn /home/nats-server /go/src/github.com/nats-io/gnatsd

cd /home/nats-server && git rev-parse HEAD >/dev/null
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # GOPATH + CGO are set once here (shared). GO111MODULE is deliberately NOT
        # set in the base: modern Go defaults it to "on" (correct for the 20 module
        # PRs), and only the 5 GOPATH PRs flip it OFF, in their own PR Dockerfile.
        # No `git clone` token appears in this Dockerfile (the clone is inside
        # clone_repo.sh), so the enhancer leaves the base's full history intact.
        return f"""FROM {image_name}

{self.global_env}

ENV GOPATH=/go
ENV CGO_ENABLED=0

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*

{copy_commands}
RUN bash /home/clone_repo.sh

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

    @property
    def is_gopath(self) -> bool:
        return self.pr.number in _GOPATH_PRS

    def dependency(self) -> Image | None:
        return natsserverImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The ONLY per-era values, all computed in Python. The shell templates
        # below stay flat and identical in structure across every PR.
        go_dir = _GO_IMPORT_DIR if self.is_gopath else "/home/nats-server"
        cert_regen = (
            _CERT_REGEN
            if self.is_gopath
            else "# module-era certs are valid to 2029/2032; no fixture repair needed"
        )
        # Scope the graded run to the packages this PR's test.patch touches (Python-
        # computed; the shell template just receives a PACKAGES= line either way).
        touched = _touched_packages(self.pr.test_patch)
        pkg_line = (
            'PACKAGES="' + " ".join(touched) + '"' if touched else _PKG_LINE_ALL
        )
        # ...and to the specific Test funcs the patch touches, so we do not run all of
        # ./server (which for the 2022+ era exceeds the 600s timeout and panics). If no
        # Test decl is found, fall back to the whole package (no -run filter).
        targets = _target_tests(self.pr.test_patch)
        run_filter = (
            "-run '^(" + "|".join(targets) + ")$' " if targets else ""
        )
        # -vet=off: `go test` runs `go vet` by default, and Go 1.24's vet rejects the
        # 2018 GOPATH-era code ("non-constant format string in call to Fatalf/Errorf"),
        # which fails the BUILD before any test runs -> 0 results. Skipping vet lets the
        # old code compile+test; harmless for the module eras (they already pass vet).
        test_cmd = (
            f"go test -v -count=1 -vet=off -timeout {_GO_TEST_TIMEOUT} "
            f"{run_filter}-skip '{_SKIP_TESTS}' $PACKAGES"
        )

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
                """#!/bin/bash
set -e

cd {go_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Scrub THIS PR image down to its base commit's ancestry (the shared base keeps
# full history). After this the shipped PR image carries no future/fix commits.
git checkout --detach {sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all || true
git gc --prune=now --aggressive
git repack -a -d -l --quiet || true
rm -f .git/objects/info/alternates
git config --local gc.auto 0

# Era-specific fixture repair (empty for module-era PRs). Runs AFTER the clean-tree
# assertions and the scrub, because re-signing certs dirties tracked files.
{cert_regen}

# Warm the build cache so the three graded runs do not each pay a full compile.
{pkg_line}
go build $PACKAGES || true

""".format(go_dir=go_dir, sha=self.pr.base.sha, cert_regen=cert_regen, pkg_line=pkg_line),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {go_dir}
{pkg_line}
{go_test_cmd}

""".format(go_dir=go_dir, pkg_line=pkg_line, go_test_cmd=test_cmd),
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

""".format(go_dir=go_dir, pkg_line=pkg_line, go_test_cmd=test_cmd),
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

""".format(go_dir=go_dir, pkg_line=pkg_line, go_test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The only per-era ENV. Module PRs inherit the base default (GO111MODULE on);
        # the 5 GOPATH PRs flip it off here. No `git clone` token in this Dockerfile
        # (checkout happens inside prepare.sh), so the enhancer does not re-scrub.
        module_env = "ENV GO111MODULE=off" if self.is_gopath else "ENV GO111MODULE=on"

        return f"""FROM {name}:{tag}

{self.global_env}

{module_env}

{copy_commands}
RUN bash /home/prepare.sh

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

        # Strip ANSI first (gotestsum / CI wrappers colourize).
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Anchored on `go test -v` result lines only. Deliberately NOT a bare
        # `FAIL\s+(\S+)`: go prints a per-package summary line that a broad pattern
        # would turn into a phantom failing "test".
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

        # Enforce TestResult disjointness: failure wins, then skip, then pass.
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
