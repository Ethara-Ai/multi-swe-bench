import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Toolchain pinned to the `go` directive in go.mod at this record's base.sha
# (ddf1844455fe -> `go 1.24.0`), measured not guessed. blackbox_exporter is a
# separate repo from prometheus/prometheus, so it does NOT share that repo's
# per-PR Go-era table; this single record maps cleanly to one toolchain. Since
# Go 1.21 the go.mod `go` directive is a HARD floor (an older toolchain refuses
# to build), and the fix patch here bumps go.mod, so 1.24 satisfies both stages.
_GO_MINOR = "1.24"


def _sanitize_patch(patch: str) -> str:
    """Drop binary diff sections, which ``git apply`` rejects for lack of a full
    index line and which would abort the whole apply under ``set -e``.

    Do NOT re-add a ``go.sum`` / ``go.work.sum`` filter here. Stripping the lock
    file leaves go.mod requiring a module go.sum cannot verify, which forces
    ``-mod=mod`` to refetch and rewrite it from the network at eval time. Keeping
    it is what lets the run scripts resolve offline from the warmed module cache.
    """
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        kept.append(sec)
    return "".join(kept)


class BlackboxExporterImageBase(Image):
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
        # Toolchain matched to this record's go.mod, NOT "golang:latest".
        return f"golang:{_GO_MINOR}"

    def image_tag(self) -> str:
        # Per-PR base (ipfs/kubo model): each PR gets its own base image so the
        # DockerfileEnhancer can safely pin it to THIS PR's ${BASE_COMMIT} and
        # prune. A shared toolchain-versioned base could not be enhancer-pinned.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # MUST track image_tag(): the build-context directory is derived from
        # workdir(), so a constant here would collide with other bases.
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{org}/{repo}.git /home/{repo}"
        else:
            code = f"COPY {repo} /home/{repo}"

        # No inline `# syntax` directive: its presence is the enhancer's
        # skip-sentinel (image.py:317). Emitting a MINIMAL base lets
        # DockerfileEnhancer.enhance() run in full and inject TARGETARCH, the
        # proxy ARGs, SSL_CERT_FILE/CA_CERT_PATH ENVs + the 7 CA-cert symlinks,
        # OCI labels, the standardized ${REPO_URL} fetch, `git checkout
        # ${BASE_COMMIT}` and Image._HARDENING_BLOCK -- all into this per-PR base.
        #
        # Deliberately NO apt-get: the official `golang:1.24` image already ships
        # git and /etc/ssl/certs/ca-certificates.crt, which is all this base
        # needs (golang:1.24 is live Debian, so apt-get would also work, but it
        # is unnecessary here).
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}
"""


class BlackboxExporterImageDefault(Image):
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
        return BlackboxExporterImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _sanitize_patch(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _sanitize_patch(self.pr.test_patch),
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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Image build is the only point where the network is legitimately available, so
# both module graphs are warmed here; every eval stage then resolves from the cache.
export GOPROXY=https://proxy.golang.org,direct
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode

go mod download -x 2>&1 || true
go build ./... 2>&1 || true

# The test patch imports github.com/gorilla/websocket, but the requirement for
# it is added by the FIX patch's go.mod hunk -- at base that module is neither
# required nor imported, so warming at base alone provably cannot cache it.
# Apply both patches, pull the post-patch graph, then restore the pristine tree.
git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true
go mod download all 2>&1 || true
git checkout -- . 2>&1 || true
git clean -fd 2>&1 || true

bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git checkout -- go.mod go.sum 2>/dev/null || true

# Resolve only from the module cache prepare.sh warmed: no network at eval time.
# Not GOPROXY=off: the test stage runs against the BASE go.mod, so -mod=mod must
# look gorilla/websocket up to add it, and "off" blocks lookups even for cached
# modules -- masking the real type errors behind "[setup failed]".
export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off  # already verified against sum.golang.org during prepare.sh
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode

# TestChooseProtocol resolves ipv6.google.com over live DNS, so it fails in any
# sealed container at BOTH the run and fix stages. The skip must be byte-identical
# across all three stages; an asymmetric skip would invent a status transition.
go test -v -count=1 -skip '^TestChooseProtocol$' ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
# prepare.sh's post-patch warm can leave go.mod/go.sum rewritten; restore the
# tracked copies so the patch applies against a pristine base tree.
git checkout -- go.mod go.sum 2>/dev/null || true

export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode

git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch

# --reject applies what it can and leaves .rej files behind. Continuing from a
# half-applied tree yields plausible but wrong results, so fail loudly instead.
if [ -n "$(find . -name '*.rej' -print -quit)" ]; then
  echo "test-run: patch application left .rej files, aborting" >&2
  find . -name '*.rej' >&2
  exit 1
fi

# config/ and prober/ cannot link here, so Go emits "[build failed]" per package
# instead of per-test results. Do NOT replay base-commit results to close that
# gap: those tests never ran under this patch, and report.py already re-credits
# them as p2p via reclassified_from_target.
go test -v -count=1 -skip '^TestChooseProtocol$' ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git checkout -- go.mod go.sum 2>/dev/null || true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch

if [ -n "$(find . -name '*.rej' -print -quit)" ]; then
  echo "fix-run: patch application left .rej files, aborting" >&2
  find . -name '*.rej' >&2
  exit 1
fi

export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode
go test -v -count=1 -skip '^TestChooseProtocol$' ./...

""".format(pr=self.pr),
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


@Instance.register("prometheus", "blackbox_exporter")
class BlackboxExporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BlackboxExporterImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Only the per-test result lines are authoritative:
        #     --- PASS: TestFoo (0.00s)
        #     --- FAIL: TestFoo/subcase (0.01s)
        #     --- SKIP: TestFoo (0.00s)
        #
        # Do NOT add a broad `FAIL:?\s?(.+?)\s` pattern: it also matches go's
        # PACKAGE summary lines, e.g.
        #     FAIL	github.com/prometheus/blackbox_exporter/prober	1.234s
        # recording the import path as a phantom failed TEST and corrupting
        # failed_count / the f2p/p2p sets.
        re_pass_tests = [re.compile(r"^--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"^--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"^--- SKIP: (\S+)")]

        # A package that fails to COMPILE emits no `--- FAIL:` lines at all, only
        #     FAIL	github.com/prometheus/blackbox_exporter/prober [build failed]
        # so an unrecorded compile knockout is indistinguishable from a clean run
        # (failed_count == 0). This anchors on the literal TAB `go test` emits, so
        # unlike the broad pattern above it can never match `--- FAIL: TestFoo`.
        # Such markers are never creditable: a package summary line cannot reach
        # PASS, so they stay out of f2p/n2p/p2p and only make the failure visible.
        re_package_fail = re.compile(r"^FAIL\t(\S+)")

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(get_base_name(pass_match.group(1)))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(get_base_name(fail_match.group(1)))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    skipped_tests.add(get_base_name(skip_match.group(1)))

            package_fail_match = re_package_fail.match(line)
            if package_fail_match:
                failed_tests.add(package_fail_match.group(1))

        # `go test` reports subtests separately, and a re-listed name can appear
        # under more than one status. Reconcile with failure winning, then skip,
        # so the three sets stay disjoint -- otherwise TestResult.__post_init__
        # raises and the instance run dies.
        passed_tests -= failed_tests | skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
