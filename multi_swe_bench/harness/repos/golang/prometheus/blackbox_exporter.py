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
    """Drop diff sections that ``git apply`` cannot take cleanly.

    Two failure modes (see prometheus.py, same engine), both of which abort the
    WHOLE ``git apply`` under ``set -e`` so the real source changes never land:

    * Binary hunks (images/fonts) are emitted without a full index line ->
      ``cannot apply binary patch ... without full index line``. Irrelevant to
      the Go tests.
    * ``go.sum`` / ``go.work.sum`` are lock files whose hunks depend on the exact
      module graph and routinely fail to apply. They are regenerated on demand by
      ``GOFLAGS=-mod=mod`` in the run scripts, so the patched copy is not needed.
      This matters HERE specifically: blackbox_exporter PR #1278's fix patch
      edits go.mod AND go.sum, and applying the go.sum hunk verbatim breaks.
    """
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        m = re.match(r"diff --git a/\S+ b/(\S+)", sec)
        if m and m.group(1).rsplit("/", 1)[-1] in ("go.sum", "go.work.sum"):
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

# -mod=mod lets `go test` add any go.sum entries the (later) fix patch will need
# while warming the module/build cache at image-build time.
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Resilient apply: plain, then 3-way, then reject-tolerant. Patches are already
# binary/go.sum-stripped (see _sanitize_patch), so this absorbs residual
# whitespace/context drift without aborting the stage.
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch || true
# -mod=mod makes `go test` fetch modules and WRITE missing go.sum entries on
# demand -- the fix patch adds new imports, and `go mod download` alone does NOT
# backfill go.sum for newly-imported packages.
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode
go test -v -count=1 ./...

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
