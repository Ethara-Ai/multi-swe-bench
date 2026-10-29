from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: grafana/k6 PRs 6360..6528 (module go.k6.io/k6/v2, master).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-6528_to_6360"

# go.mod declares `go 1.25.0` (+ toolchain go1.25.12) .. `go 1.26.0` across these
# four commits; GOTOOLCHAIN=local forbids fetching a toolchain, so the image must
# already carry >= 1.26.0.
GO_IMAGE = "golang:1.26-bookworm"

MODULE_PREFIX = "go.k6.io/k6/v2"
REPO_DIR = "/home/k6"

# Mirrors upstream CI (.github/actions/test: `go test -p 2 -race`), scoped to the
# PR's packages. The timeout only bounds a hang; upstream allows 1600s for ./...
# -race is not in this string: it comes from RACE_PROBE_SH at run time.
GO_TEST_FLAGS = "-p 2 -v -count=1 -timeout 20m"

# The race detector (ThreadSanitizer) needs a 48-bit virtual address space. arm64
# under QEMU user emulation (how buildx builds the arm64 half on an amd64 host)
# exposes 47 bits, and every -race binary aborts at startup with "unsupported VMA
# range". Probe once instead of keying on the arch, so amd64 and real arm64 hosts
# keep -race and only a host that cannot run it drops it.
RACE_PROBE_SH = r"""_RACE=""
if (cd "$(mktemp -d)" && printf 'package main\nfunc main() {}\n' > probe.go \
        && GOFLAGS= go run -race probe.go) >/dev/null 2>&1; then
    _RACE="-race"
fi"""

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
    return sorted(p for p in paths if not p.startswith("vendor/"))


def target_packages(pr: PullRequest) -> list[str]:
    """Go packages owning those files, as ./-relative paths from the module root.

    Scoped rather than `./...`: the full suite under -race takes ~25min per stage
    and includes browser/network suites unrelated to these PRs. Helper files the
    test patch also touches (e.g. internal/lib/testutils/tlstest, a non-test
    package) compile as dependencies of the graded packages.
    """
    owners = {path.rsplit("/", 1)[0] for path in target_test_files(pr) if "/" in path}
    return sorted(f"./{owner}" for owner in owners)


class K6ImageBase(Image):
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

# -race needs cgo. Dependencies are vendored at every commit here, so builds
# never touch the network.
ENV GOFLAGS="-buildvcs=false -mod=vendor" \\
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
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {REPO_DIR} && \\
    cd {REPO_DIR} && git rev-parse HEAD >/dev/null

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
export GOFLAGS="-buildvcs=false -mod=vendor"
export GOTOOLCHAIN=local
export GO111MODULE=on
export CGO_ENABLED=1

[[RACE_PROBE]]

# Dependencies come from vendor/, so there is nothing to download. Warm the build
# cache (plain and $_RACE) so the graded stages start compiled. Tolerated because
# the gate below re-runs the same builds non-tolerantly.
go build ./... >/dev/null 2>&1 || true
go test $_RACE -run '^$' -count=1 [[TEST_PKGS]] >/dev/null 2>&1 || true

# ========== Section 3: GATE (non-tolerant, last) ==========
go version
# -n prints every command it would run to stderr; keep that out of the build log
# unless the load actually fails.
go vet -n ./... >/dev/null 2>/tmp/go_vet_n.log || { tail -50 /tmp/go_vet_n.log >&2; exit 1; }
rm -f /tmp/go_vet_n.log
go build ./...

# Only packages that already exist at this commit can be compile-checked: a PR
# may CREATE the package it tests, and `go test` on a missing directory is a
# setup failure, not a dependency problem.
_gate_pkgs=""
for _pkg in [[TEST_PKGS]]; do
    if compgen -G "${_pkg}/*.go" >/dev/null; then
        _gate_pkgs="${_gate_pkgs} ${_pkg}"
    fi
done
echo "race detector: ${_RACE:-unavailable on this host}"
go test $_RACE -run '^$' -count=1 ${_gate_pkgs:-./lib/...}
echo DEPS_OK
"""


# The one test command every stage runs.
TEST_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC

cd [[REPO_DIR]]

export GOFLAGS="-buildvcs=false -mod=vendor"
export GOTOOLCHAIN=local
export GO111MODULE=on
export CGO_ENABLED=1

_PKGS="[[TEST_PKGS]]"
test -n "$_PKGS" || {
    echo "test: this PR's test patch contains no Go test file; nothing to grade"
    exit 0
}

[[RACE_PROBE]]

go test $_RACE [[GO_TEST_FLAGS]] $_PKGS 2>&1
"""


class K6ImageDefault(Image):
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
        return K6ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        pkgs = target_packages(self.pr)
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_PKGS]]", " ".join(pkgs))
            .replace("[[GO_TEST_FLAGS]]", GO_TEST_FLAGS)
            .replace("[[RACE_PROBE]]", RACE_PROBE_SH)
        )

    def install_files(self) -> list[File]:
        """Files the provisioning layer depends on.

        Kept separate from the grading files so that re-cutting a patch does not
        invalidate the build-cache warm-up layer.
        """
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
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


PR_LO, PR_HI = 6360, 6528


@Instance.register("grafana", "k6_6528_to_6360")
class K6_6528_TO_6360(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return K6ImageDefault(self.pr, self._config)

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
# Routing for the bare "grafana/k6" key.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty. These records carry no
# number_interval, so they arrive on "grafana/k6". Nothing else registers that
# key today; the router still captures any earlier holder and hands every PR
# outside this interval back to it, so a future k6 config keeps working.
_INCUMBENT = Instance._registry.get("grafana/k6")


@Instance.register("grafana", "k6")
class K6BareName(Instance):
    """Not an implementation -- a router for the bare "grafana/k6" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return K6_6528_TO_6360(pr, config, *args, **kwargs)

        if _INCUMBENT is None:
            raise ValueError(
                f"grafana/k6#{pr.number} falls outside the {PR_LO}..{PR_HI} "
                f"interval this config serves, and no other config is "
                f"registered for grafana/k6."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
