from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: JuliusBrussee/caveman PRs 1261..1315 (Oct 2026, main).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-1315_to_1261"

# go.mod declares `go 1.26.9` .. `go 1.26.10` across the four base commits and
# GOTOOLCHAIN=local forbids fetching another toolchain, so the image must carry
# a go >= 1.26.10.
GO_IMAGE = "golang:1.26-bookworm"

MODULE_PREFIX = "github.com/JuliusBrussee/caveman"
REPO_DIR = "/home/caveman"

# caveman is a polyglot monorepo (Go engine/proxy/mcp/mem + a pnpm JS/TS
# workspace + two Python packages). Only the Go side is graded: it is the repo's
# primary language, every PR in this shard carries Go tests, and all of it builds
# from the single root go.mod. The JS/Python tests of the same PRs are not run.
GO_TEST_FLAGS = "-v -count=1 -timeout 20m"

# Package-level key emitted from go's own verdict line. Go gates tests at COMPILE
# time: when a test patch references a symbol only the fix adds, the package fails
# to build, `go test` prints no `--- PASS/FAIL` lines at all, and the graded test
# can never pair FAILED(test) with PASSED(fix). This key is emitted only when go
# actually printed a verdict for the package, so a stage that never ran cannot
# manufacture a transition.
BUILD_TEST_NAME = "<build>"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _test_patch_paths(pr: PullRequest) -> list[str]:
    """Post-patch paths the test patch touches; deleted files are dropped."""
    paths = set()
    for block in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header and "\ndeleted file mode" not in block:
            paths.add(header.group(2))
    return sorted(paths)


def test_packages(pr: PullRequest) -> list[str]:
    """Go packages owning this PR's *_test.go files, as ./-relative paths.

    Scoped rather than `./...`: the module spans the engine, proxy, mcp and mem
    trees, and running every package per stage grades far more than the PR.
    """
    owners = {
        path.rsplit("/", 1)[0] if "/" in path else "."
        for path in _test_patch_paths(pr)
        if path.endswith("_test.go")
    }
    return sorted(owner if owner == "." else f"./{owner}" for owner in owners)


class CavemanImageBase(Image):
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
        #
        # CGO_ENABLED=1 + build-essential: engine/compressors/code_cgo.go
        # (`//go:build cgo`) links the tree-sitter C grammars.
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOFLAGS=-buildvcs=false \\
    GOTOOLCHAIN=local \\
    GO111MODULE=on \\
    CGO_ENABLED=1
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
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
WORKDIR /home/
RUN git clone "${{REPO_URL}}" {REPO_DIR}
WORKDIR {REPO_DIR}
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
# Dependencies are not vendored: resolve them from this commit's own go.mod and
# go.sum (checksum-verified) into the module cache, so the graded stages build
# offline.
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

# Warm the build and test-binary caches so the graded stages start compiled.
# Tolerated because the gate below re-runs the same builds non-tolerantly.
go build ./... >/dev/null 2>&1 || true
go test -run '^$' -count=1 [[TEST_PKGS]] >/dev/null 2>&1 || true

# ========== Section 3: GATE (non-tolerant, last) ==========
go version
# -n prints every command it would run to stderr; keep that out of the build log
# unless the load actually fails.
go vet -n ./... >/dev/null 2>/tmp/go_vet_n.log || { tail -50 /tmp/go_vet_n.log >&2; exit 1; }
rm -f /tmp/go_vet_n.log
go build ./...

# Compile the graded test binaries. Only packages that already exist at this
# commit can be checked: a PR may CREATE the package it tests.
_gate_pkgs=""
for _pkg in [[TEST_PKGS]]; do
    if compgen -G "${_pkg}/*.go" >/dev/null; then
        _gate_pkgs="${_gate_pkgs} ${_pkg}"
    fi
done
go test -run '^$' -count=1 ${_gate_pkgs:-./engine}
echo DEPS_OK
"""


# The one test command every stage runs. A package the test patch creates exists
# only once that patch is applied; the same filter runs in every stage, so the
# baseline simply has fewer packages instead of failing the whole `go test` on a
# missing directory.
TEST_CMD = r"""export TZ=UTC
export GOFLAGS=-buildvcs=false
export GOTOOLCHAIN=local
export GOPROXY=off

_pkgs=""
for _pkg in [[TEST_PKGS]]; do
    if compgen -G "${_pkg}/*_test.go" >/dev/null; then
        _pkgs="${_pkgs} ${_pkg}"
    fi
done
test -n "${_pkgs}" || {
    echo "test: none of this PR's Go test packages exist in this stage"
    exit 1
}

go test -p 1 [[GO_TEST_FLAGS]] ${_pkgs} 2>&1
"""


class CavemanImageDefault(Image):
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

    def dependency(self) -> Image:
        return CavemanImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_PKGS]]", " ".join(test_packages(self.pr)))
            .replace("[[GO_TEST_FLAGS]]", GO_TEST_FLAGS)
        )

    def files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
            "fix-run.sh": (
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            ),
        }
        stages = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\n" + TEST_CMD
                ),
            )
            for name, patch_step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

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
{self.global_env}
{copies}RUN bash /home/prepare.sh
WORKDIR {REPO_DIR}
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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
{self.clear_env}
"""


PR_LO, PR_HI = 1261, 1315


@Instance.register("JuliusBrussee", "caveman_1315_to_1261")
class CAVEMAN_1315_TO_1261(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CavemanImageDefault(self.pr, self._config)

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
        build_fail_re = re.compile(
            r"^FAIL\s+(" + module + r"\S*)\s+\[(?:build|setup) failed\]"
        )
        build_ok_re = re.compile(
            r"^(?:ok|FAIL)\s+(" + module + r"\S*)\s+(?:[0-9]|\(cached\))"
        )
        timeout_re = re.compile(r"^panic: test timed out after ")
        running_re = re.compile(r"^\s+(Test\S+) \([0-9]")
        in_timeout = False

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

        # Results go never attributed to a package (output cut off mid-run) are
        # dropped rather than guessed at: a wrong package would mint a test name
        # that exists in only one stage.
        pending.clear()

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
# Routing for the bare "JuliusBrussee/caveman" key.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty -- which it is for all four
# records of this shard. No other config holds the bare name today; the
# incumbent is still captured so a later-registered era cannot be clobbered.
_INCUMBENT = Instance._registry.get("JuliusBrussee/caveman")


@Instance.register("JuliusBrussee", "caveman")
class CavemanBareName(Instance):
    """Not an implementation -- a router for the bare "JuliusBrussee/caveman" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return CAVEMAN_1315_TO_1261(pr, config, *args, **kwargs)

        if _INCUMBENT is None:
            raise ValueError(
                f"JuliusBrussee/caveman#{pr.number} falls outside the "
                f"{PR_LO}..{PR_HI} interval this config serves, and no other "
                f"caveman config is registered."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
