from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: jesseduffield/lazygit PRs 5660..5758 (May-Jul 2026, master).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-5758_to_5660"

# go.mod declares `go 1.25.0` at all six base commits and GOTOOLCHAIN=local
# forbids fetching another toolchain, so the image must carry a 1.25.x go.
GO_IMAGE = "golang:1.25-bookworm"

MODULE_PREFIX = "github.com/jesseduffield/lazygit"
REPO_DIR = "/home/lazygit"

# Every dependency is vendored (vendor/modules.txt at all six commits), so builds
# and tests never touch the network.
GO_TEST_FLAGS = "-v -count=1 -timeout 30m"

# lazygit's integration tests are not `func TestXxx`: each file under
# pkg/integration/tests/<dir>/<name>.go registers one test named "<dir>/<name>",
# and pkg/integration/clients/go_test.go runs them all as subtests of
# TestIntegration (headless, in a pty). Four of these six PRs grade through them,
# and #5666 has no other tests at all.
INTEGRATION_PKG = "./pkg/integration/clients"
INTEGRATION_PREFIX = "pkg/integration/tests/"
# Generated index of every integration test, not a test itself.
INTEGRATION_NON_TESTS = {"pkg/integration/tests/test_list.go"}

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


def unit_packages(pr: PullRequest) -> list[str]:
    """Go packages owning this PR's *_test.go files, as ./-relative paths.

    Scoped rather than `./...`: the full suite includes every integration test
    (hundreds of headless lazygit runs) and costs far more than the PR grades.
    """
    owners = {
        path.rsplit("/", 1)[0]
        for path in _test_patch_paths(pr)
        if path.endswith("_test.go") and "/" in path
    }
    return sorted(f"./{owner}" for owner in owners)


def integration_tests(pr: PullRequest) -> list[str]:
    """Integration test names ("<dir>/<name>") this PR adds or changes."""
    names = set()
    for path in _test_patch_paths(pr):
        if (
            path.startswith(INTEGRATION_PREFIX)
            and path.endswith(".go")
            and not path.endswith("_test.go")
            and path not in INTEGRATION_NON_TESTS
        ):
            names.add(path[len(INTEGRATION_PREFIX) : -len(".go")])
    return sorted(names)


def integration_run_pattern(pr: PullRequest) -> str:
    """A `go test -run` pattern selecting exactly those subtests.

    A test name holds a `/`, which go's matcher treats as a level separator, so
    each name becomes `^TestIntegration$/^<dir>$/^<name>$`; top-level `|`
    alternates whole patterns.
    """
    return "|".join(
        "^TestIntegration$/" + "/".join(f"^{re.escape(part)}$" for part in name.split("/"))
        for name in integration_tests(pr)
    )


class LazygitImageBase(Image):
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOFLAGS="-mod=vendor -buildvcs=false" \\
    GOTOOLCHAIN=local \\
    GO111MODULE=on
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
# Dependencies are vendored in the commit itself (vendor/modules.txt), so
# GOFLAGS=-mod=vendor resolves everything from the tree with no network.
test -f vendor/modules.txt

# Warm the build and test-binary caches so the graded stages start compiled.
# Tolerated because the gate below re-runs the same builds non-tolerantly.
go build ./... >/dev/null 2>&1 || true
go test -run '^$' -count=1 ./... >/dev/null 2>&1 || true

# ========== Section 3: GATE (non-tolerant, last) ==========
go version
# -n prints every command it would run to stderr; keep that out of the build log
# unless the load actually fails.
go vet -n ./... >/dev/null 2>/tmp/go_vet_n.log || { tail -50 /tmp/go_vet_n.log >&2; exit 1; }
rm -f /tmp/go_vet_n.log
go build ./...

# Compile the graded test binaries. Only packages that already exist at this
# commit can be checked: a PR may CREATE the package it tests (#5660 adds
# pkg/commands/direnv). The integration client package always exists.
_gate_pkgs="[[INTEGRATION_PKG]]"
for _pkg in [[UNIT_PKGS]]; do
    if compgen -G "${_pkg}/*.go" >/dev/null; then
        _gate_pkgs="${_gate_pkgs} ${_pkg}"
    fi
done
go test -run '^$' -count=1 ${_gate_pkgs}
echo DEPS_OK
"""


# The one test command every stage runs. Both suites always run, and the stage
# exits non-zero if either one failed: `|| _rc=$?` records the status instead of
# swallowing it, so a broken stage still fails loudly.
TEST_CMD = r"""export TZ=UTC
export GOFLAGS="-mod=vendor -buildvcs=false"
export GOTOOLCHAIN=local

_rc=0

# Unit tests. A package the test patch creates exists only once that patch is
# applied; the same filter runs in every stage, so the baseline simply has fewer
# packages instead of failing the whole `go test` on a missing directory.
_unit=""
for _pkg in [[UNIT_PKGS]]; do
    if compgen -G "${_pkg}/*_test.go" >/dev/null; then
        _unit="${_unit} ${_pkg}"
    fi
done
if [ -n "${_unit}" ]; then
    go test -p 1 [[GO_TEST_FLAGS]] ${_unit} 2>&1 || _rc=$?
fi

# Integration tests: exactly the ones this PR adds or changes. Names that do not
# exist yet (baseline stage) match nothing.
_pattern='[[INTEGRATION_PATTERN]]'
if [ -n "${_pattern}" ]; then
    go test [[GO_TEST_FLAGS]] [[INTEGRATION_PKG]] -run "${_pattern}" 2>&1 || _rc=$?
fi

exit ${_rc}
"""


class LazygitImageDefault(Image):
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
        return LazygitImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[UNIT_PKGS]]", " ".join(unit_packages(self.pr)))
            .replace("[[INTEGRATION_PKG]]", INTEGRATION_PKG)
            .replace("[[INTEGRATION_PATTERN]]", integration_run_pattern(self.pr))
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


PR_LO, PR_HI = 5660, 5758


@Instance.register("jesseduffield", "lazygit_5758_to_5660")
class LAZYGIT_5758_TO_5660(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LazygitImageDefault(self.pr, self._config)

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
        # keeps the full `TestFunc/subtest` path (integration tests are
        # `TestIntegration/<dir>/<name>`) -- stripping the suffix would collapse
        # every subtest of a function onto one colliding name.
        result_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        build_fail_re = re.compile(
            r"^FAIL\s+(" + module + r"\S*)\s+\[(?:build|setup) failed\]"
        )
        build_ok_re = re.compile(
            r"^(?:ok|FAIL)\s+(" + module + r"\S*)\s+(?:[0-9]|\(cached\))"
        )
        no_tests_re = re.compile(r"^\?\s+(" + module + r"\S*)\s+\[no test files\]")
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

            if no_tests_re.match(line):
                continue

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
# Routing, owned entirely by this file -- lazygit.py is not modified.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty. These six records carry no
# number_interval, so they arrive on the bare name "jesseduffield/lazygit", which
# lazygit.py registers to its own legacy Lazygit class.
#
# Instance.register() overwrites rather than erroring, so this module must be
# imported AFTER lazygit.py. The incumbent class is captured first and every PR
# outside this interval is handed straight back to it, so no other lazygit record
# changes behaviour. Records that DO carry a number_interval never reach here at
# all -- they key directly on their own interval.
_INCUMBENT = Instance._registry.get("jesseduffield/lazygit")


@Instance.register("jesseduffield", "lazygit")
class LazygitBareName(Instance):
    """Not an implementation -- a router for the bare "jesseduffield/lazygit" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return LAZYGIT_5758_TO_5660(pr, config, *args, **kwargs)

        # Outside this era: hand back to whoever held the bare name before us.
        if _INCUMBENT is None:
            raise ValueError(
                f"jesseduffield/lazygit#{pr.number} falls outside the "
                f"{PR_LO}..{PR_HI} interval this adapter serves, and no previous "
                f"holder of the bare 'jesseduffield/lazygit' key was captured at "
                f"import time. Import "
                f"multi_swe_bench.harness.repos.golang.jesseduffield.lazygit "
                f"before this module (see jesseduffield/__init__.py)."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
