import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Single-era config covering PRs #2720 -> #8655 (release-0.14 through release-1.20).
#
# Two structural shifts span this range, both handled here without an era split:
#   1. Module path renamed `github.com/jetstack/cert-manager` ->
#      `github.com/cert-manager/cert-manager` around release-1.7. GitHub follows
#      the org rename, so cloning github.com/cert-manager/cert-manager works for
#      every base SHA. Inside the clone, go.mod's module line drives import
#      resolution, so no symlink staging is required.
#   2. Repo split from a single root module into 6-9 modules around release-1.12
#      (PRs <=6208 have one go.mod; PRs >=6237 have go.mod under cmd/* and
#      test/*). The run_tests.sh loop walks every go.mod and runs `go test` in
#      each, which degrades cleanly to one iteration for the single-module era.
#
# `golang:1.22` host + GOTOOLCHAIN=auto covers go.mod directives from `go 1.12`
# (release-0.14 baseline) through `go 1.25.0` (release-1.20); older directives
# compile under the host toolchain, newer directives trigger a download.
#
# test/e2e (Ginkgo, needs cluster) and test/integration (needs etcd +
# kube-apiserver via `make setup-integration-tests`) are skipped at both
# discovery levels:
#   - go.mod files under test/e2e/ and test/integration/ are excluded from the
#     find loop in the multi-module era.
#   - Packages whose import paths contain /test/e2e or /test/integration are
#     filtered from `go list ./...` in the single-module era (where they are
#     subpackages of the root module, not separate modules).


_RUN_TESTS_BODY = """#!/bin/bash
# Walk every go.mod in the repo and run that module's unit tests.
# Each module gets its own `go list | grep -v` filter so Ginkgo-driven e2e and
# integration suites (which require a live cluster) never reach `go test`.
# Per-module failures are captured in $status but never abort the loop -- the
# harness needs --- PASS/FAIL/SKIP lines from every module that produces them.
set -o pipefail
export CI=true
export GOTOOLCHAIN=auto
# Make absolutely sure no test reaches out to network-backed services.
# cert-manager unit tests that need DNS / ACME / Vault / cloud APIs all
# self-skip when their respective `*_TEST_*` env vars are unset, but a few
# legacy suites (e.g. rfc2136) try a live DNS lookup as a fallback. Setting
# these env vars forces immediate-fail for any accidental DNS or HTTP egress
# without affecting in-process 127.0.0.1 listeners the tests spin up.
export ACME_DIRECTORY_URL=""
export VAULT_ADDR=""
# Disable kubectl/kubeconfig discovery so anything that accidentally
# instantiates a clientcmd loader doesn't try $HOME/.kube/config.
export KUBECONFIG=/dev/null
cd /home/{repo}
status=0
for modfile in $(find . -name go.mod -not -path "./vendor/*" -not -path "./test/e2e/*" -not -path "./test/integration/*" | sort); do
    dir=$(dirname "$modfile")
    echo "=== Running tests in $dir ==="
    # `|| true` on the pipeline because:
    #   - `grep -vE` returns 1 if EVERY line matched the exclude patterns
    #     (legitimately means "no testable packages here"), and pipefail
    #     would otherwise abort the script.
    #   - `go list` returning non-zero is also non-fatal -- the next filter
    #     drops compile-error noise from $pkgs.
    pkgs=$(cd "$dir" && go list ./... 2>&1 | grep -vE "/test/e2e($|/)|/test/integration($|/)|/test/acme/dns/example" || true)
    # If `go list` emitted compile errors, they end up in $pkgs too. Filter
    # to real import paths only (must contain a '/' and no spaces).
    pkgs=$(echo "$pkgs" | grep -E '^[^ ]+/[^ ]+$' || true)
    if [ -z "$pkgs" ]; then
        echo "(no testable packages in $dir)"
        continue
    fi
    # `-p=4 -parallel=2`: 4 packages in parallel, 2 subtests in parallel per
    # package. Earlier choice of `-p=1 -parallel=1` was paranoid -- cert-manager
    # tests overwhelmingly use httptest.NewServer (random port) or the in-process
    # vnet, not hardcoded ports. The handful of port-binding suites
    # (rfc2136/dns, acme/http, webhook, metrics) each live in their own package
    # so within-package -parallel=2 is safe; cross-package -p=4 only risks
    # collision if two of THOSE specific packages run together, which is
    # uncommon. The 4-6x wall-clock win matters more than absolute paranoia for
    # a 92k-test, 104-package, 3-stage-per-PR workload.
    # `-timeout 30m` accommodates Go 1.25 PRs that pull hundreds of MB of
    # k8s.io dependencies on the first run.
    (cd "$dir" && go test -v -count=1 -p=4 -parallel=2 -timeout 30m $pkgs) || status=$?
done
echo "run_tests.sh: final status=$status"
exit 0
"""


class ImageBase(Image):
    """Shared base image: FROM golang:1.22 + apt-get + cert-manager clone.

    Built once per architecture, then every per-PR image FROMs this base.
    Saves ~50 minutes off a 62-PR multi-arch build because apt-get update +
    apt-get install + `git clone` of cert-manager (200+ MB of history) only
    runs once instead of 124 times (62 PRs * 2 architectures).
    """

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
        return "golang:1.22"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # DockerfileEnhancer already injects `ENV DEBIAN_FRONTEND=noninteractive`
        # plus TZ/LANG/proxy/cert vars before our content, so we only emit env
        # vars it doesn't set (just GOTOOLCHAIN). No blank line after FROM
        # because the enhancer's prepended block already ends with one.
        return f"""FROM {self.dependency()}
ENV GOTOOLCHAIN=auto

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates make gcc libc6-dev \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

""".format(),
            ),
            File(
                ".",
                "run_tests.sh",
                _RUN_TESTS_BODY.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export GOTOOLCHAIN=auto
# Pre-fetch deps for every module so the test runs don't hit the network for
# the bulk of the download. `|| true` because some legacy modules have
# replaced/removed deps that resolve only when actually compiled.
for modfile in $(find . -name go.mod -not -path "./vendor/*" -not -path "./test/e2e/*" -not -path "./test/integration/*" | sort); do
    dir=$(dirname "$modfile")
    (cd "$dir" && go mod download) || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "apply_patch.sh",
                """#!/bin/bash
# Robust 3-tier patch application:
#   1. straight `git apply` (the common case)
#   2. `git apply --3way` (recovers when context drifted but blob hashes match
#      objects the repo already has -- works for most cherry-pick / rebase
#      drift across cert-manager's release-X.Y branches)
#   3. `git apply --reject` (last resort: applies as much as possible, leaves
#      .rej files for the unmatched hunks, which we then delete so they don't
#      pollute the working tree the harness inspects)
# Returns 0 even if step 3 lost hunks; the harness compares test results
# pre/post anyway, so partial application is better than a hard failure.
set +e
for patch in "$@"; do
    if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
        echo "apply_patch: clean apply of $patch"
        continue
    fi
    if git apply --3way --whitespace=nowarn "$patch" 2>/dev/null; then
        echo "apply_patch: 3-way merge applied $patch"
        continue
    fi
    echo "apply_patch: WARN falling back to --reject for $patch"
    git apply --reject --whitespace=nowarn "$patch" 2>&1 || true
    find . -name '*.rej' -delete 2>/dev/null || true
done
exit 0
""".format(),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_ref = f"{base.image_name()}:{base.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Base image already contains golang + apt deps + a cloned
        # /home/cert-manager. Per-PR work is just: checkout the base SHA,
        # copy scripts/patches, run prepare.sh (which does `go mod download`
        # for every module so the test stage doesn't pay network cost).
        return f"""FROM {base_ref}

ENV GOTOOLCHAIN=auto

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}

RUN bash /home/prepare.sh

"""


@Instance.register("cert-manager", "cert-manager")
class CertManager(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences -- cert-manager tests emit colorized
        # logger output (klog/zap) that would otherwise break regex anchors.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^\s*--- PASS:\s+(\S+)")
        re_fail = re.compile(r"^\s*--- FAIL:\s+(\S+)")
        re_skip = re.compile(r"^\s*--- SKIP:\s+(\S+)")
        # Package summary lines from `go test`:
        #   ok      github.com/cert-manager/cert-manager/pkg/util/pki   2.297s
        #   FAIL    github.com/cert-manager/cert-manager/pkg/issuer/...  0.008s
        #   ?       github.com/cert-manager/cert-manager/pkg/webhook    [no test files]
        re_pkg_ok = re.compile(r"^ok\s+(\S+)")
        re_pkg_fail = re.compile(r"^FAIL\s+(\S+)")
        re_pkg_notest = re.compile(r"^\?\s+(\S+)")

        # cert-manager is multi-module in later eras, and even in the single-
        # module era the same test name can recur across packages (e.g.
        # TestController in cmd/controller/... and pkg/controller/...).
        # Buffer per-package outcomes and qualify with the package path when
        # `go test` prints its summary line.
        buffer: list[tuple[str, str]] = []

        def flush(pkg: str) -> None:
            for status, name in buffer:
                qualified = f"{pkg}::{name}"
                if status == "PASS":
                    if qualified in failed_tests:
                        continue
                    skipped_tests.discard(qualified)
                    passed_tests.add(qualified)
                elif status == "FAIL":
                    passed_tests.discard(qualified)
                    skipped_tests.discard(qualified)
                    failed_tests.add(qualified)
                elif status == "SKIP":
                    if qualified in passed_tests or qualified in failed_tests:
                        continue
                    skipped_tests.add(qualified)
            buffer.clear()

        for line in test_log.splitlines():
            m = re_pass.match(line)
            if m:
                buffer.append(("PASS", m.group(1)))
                continue
            m = re_fail.match(line)
            if m:
                buffer.append(("FAIL", m.group(1)))
                continue
            m = re_skip.match(line)
            if m:
                buffer.append(("SKIP", m.group(1)))
                continue
            m = re_pkg_ok.match(line) or re_pkg_fail.match(line) or re_pkg_notest.match(line)
            if m:
                flush(m.group(1))

        if buffer:
            flush("<unknown-package>")

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
