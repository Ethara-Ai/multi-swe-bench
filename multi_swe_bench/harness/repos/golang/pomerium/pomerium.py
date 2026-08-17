import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Toolchain pinned from the `go` directive in go.mod at PR #1479's base.sha
# (7613f4c6, measured -> "go 1.14"), NOT "golang:latest". pomerium at this era
# pulls x/sys / k8s deps that no longer assemble on a modern toolchain, so the
# era-matched image is required.
_GO_MINOR = "1.14"


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections that ``git apply`` cannot take cleanly.

    Two failure modes abort the WHOLE ``git apply`` under ``set -e`` so the real
    source changes never land:

    * Binary hunks (fonts, .ico/.png/.gif) are emitted without a full index line
      -> ``cannot apply binary patch ... without full index line``. Irrelevant to
      the Go tests.
    * ``go.sum`` / ``go.work.sum`` are lock files whose hunks depend on the exact
      module graph and routinely fail to apply. They are regenerated on demand by
      ``GOFLAGS=-mod=mod`` in the run scripts, so the patched copy is not needed.
      (PR #1479's fix patch touches both go.mod AND go.sum -- stripping go.sum
      here is what lets the fix stage apply.)
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


class PomeriumImageBase(Image):
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
        return f"golang:{_GO_MINOR}"

    def image_tag(self) -> str:
        # Per-PR (kubo model): the enhancer bakes `git checkout ${BASE_COMMIT}` +
        # a history prune into this base, so it must belong to exactly one PR.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
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

        # MINIMAL base in the ipfs/kubo shape: NO inline `# syntax` directive, so
        # DockerfileEnhancer.enhance() (build_dataset.py -> image.py:317) RUNS and
        # injects the shared infrastructure -- TARGETARCH, the proxy ARGs, the
        # SSL_CERT_FILE/CA_CERT_PATH ENVs + CA-cert symlinks, OCI labels -- and
        # then rewrites the `git clone` above into the standardized fetch plus the
        # canonical Image._HARDENING_BLOCK (detach onto ${BASE_COMMIT}, strip every
        # ref/reflog, gc --prune, and the git rev-list dataset-leakage assertion).
        # Because the base is per-PR (see image_tag), pinning it to one BASE_COMMIT
        # is correct.
        #
        # Deliberately NO apt-get here: the official `golang:1.14` image already
        # ships git and /etc/ssl/certs/ca-certificates.crt (all this base needs),
        # and golang:1.14 is Debian *buster*, whose repositories have been retired
        # to archive.debian.org -- `apt-get update` fails outright there. kubo can
        # run apt-get because it is on golang:1.19 (a live Debian); this era cannot.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}
"""


class PomeriumImageDefault(Image):
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
        return PomeriumImageBase(self.pr, self.config)

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
# Resilient apply: plain, then 3-way, then reject-tolerant. The patches are
# already binary/go.sum-stripped (see _sanitize_patch), so this handles residual
# whitespace/context drift without aborting the stage.
git apply --whitespace=nowarn /home/test.patch \\
  || git apply --whitespace=nowarn --3way /home/test.patch \\
  || git apply --whitespace=nowarn --reject /home/test.patch || true
# -mod=mod makes `go test` fetch modules and WRITE the missing go.sum entries on
# demand -- the fix patch adds new imports, and `go mod download` alone does NOT
# backfill go.sum for newly-imported packages, so it must be -mod=mod here.
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


@Instance.register("pomerium", "pomerium")
class Pomerium(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PomeriumImageDefault(self.pr, self._config)

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
        # A loose pattern like `FAIL:?\s?(.+?)\s` also matches go's PACKAGE
        # summary lines, e.g.
        #     FAIL	github.com/pomerium/pomerium/internal/directory	1.234s
        # recording an import path as a phantom failed TEST and corrupting
        # failed_count / the f2p/p2p sets. Anchor to `^--- (PASS|FAIL|SKIP):`.
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

        # `go test` reports subtests separately, and a retried or re-listed name
        # can appear under more than one status. Reconcile with failure winning,
        # then skip, so the three sets stay disjoint -- otherwise
        # TestResult.__post_init__ raises and the instance run dies.
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
