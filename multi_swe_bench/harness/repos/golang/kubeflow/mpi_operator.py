import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MpiOperatorImageBase(Image):
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
        # Single toolchain for every PR in this dataset.
        #
        # go.mod declares a MINIMUM, not a pin: "go 1.13" for PRs 434/438 and
        # "go 1.19" for PRs 523/561/566. golang:1.19 satisfies both, so one base
        # image covers the whole range. Verified empirically - both 2021-era
        # commits (8943cf73.., 285cb98d..) compile under go1.19.13 with
        # `go build ./pkg/...` AND `go test -run <no-match> ./pkg/...` (which
        # compiles the test binaries) returning 0.
        return "golang:1.19"

    def image_tag(self) -> str:
        # One shared base image for all 5 PRs -> one base Dockerfile, matching
        # the one repo config (config:base = 1:1).
        #
        # IMPORTANT - BUILD ORDER IS LOAD-BEARING.
        # build_dataset.py sets BASE_COMMIT = image.pr.base.sha and skips any
        # image whose tag already exists, so the FIRST PR to build decides which
        # commit this shared base is pinned to. DockerfileEnhancer's hardening
        # block then deletes all history unreachable from that commit.
        #
        # All 5 base commits are linear on master:
        #   438 (2021-10-08) -> 434 (2021-11-22) -> 523 (2023-02)
        #   -> 561 (2023-06-01) -> 566 (2023-06-08)
        # so the base MUST be pinned to PR 566 (the newest); every other PR's
        # commit is then an ancestor and prepare.sh can check it out.
        #
        # Build it in two passes:
        #   1) --mode image --specifics "kubeflow/mpi-operator:pr-566"
        #   2) --mode image            (base already exists -> skipped)
        # Rebuilding from scratch without pass 1, or with --force_build true,
        # re-pins the base to whichever PR runs first and breaks the newer PRs
        # with "fatal: reference is not a tree".
        return "base"

    def workdir(self) -> str:
        return "base"

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

        # NOTE: no syntax directive, proxy/TARGETARCH ARGs, cert symlinks or OCI
        # labels here - DockerfileEnhancer.enhance() injects all of those. The
        # clone/COPY line MUST be last: _standardize_repo_fetch() expands it into
        # clone + WORKDIR + reset + checkout + hardening + CMD, so anything after
        # it would land after CMD.
        # The official golang images already ship git and ca-certificates, so no
        # apt block is needed (and golang:1.13 is Debian buster, whose apt repos
        # are now archived - an apt-get update there would fail).
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}
"""


class MpiOperatorImageDefault(Image):
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
        return MpiOperatorImageBase(self.pr, self.config)

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

""",
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export CGO_ENABLED=0

cd /home/{pr.repo}

# Single definition of the graded test command. run.sh, test-run.sh and
# fix-run.sh all source this file, so the command provably cannot drift
# between the three stages.
#
# Scope note: ./pkg/... only.
#   - test/e2e needs a live kind cluster + docker-in-docker (impossible here)
#   - test/integration needs downloaded kube-apiserver/etcd envtest binaries,
#     which would break the offline requirement
#   - ./pkg/... uses fake clients and needs neither; it contains every test
#     touched by the resolvable PRs in this dataset.
go test -v -count=1 ./pkg/...

""".format(pr=self.pr),
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

export CGO_ENABLED=0

# Warm the module cache. "|| true" because a partial failure here is not
# necessarily fatal, but it is followed by a hard verification below so a
# genuinely broken environment fails loudly instead of producing a hollow image.
go mod download || true
go build ./pkg/... || true

# Hard verification - no "|| true". If the module graph cannot be resolved the
# image build fails here rather than at test time with 0 collected tests.
go list ./pkg/... > /dev/null

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

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


@Instance.register("kubeflow", "mpi-operator")
class MpiOperator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MpiOperatorImageDefault(self.pr, self._config)

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
        # ANSI codes are stripped FIRST - every pattern below assumes clean text.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` emits, at any indentation depth for subtests:
        #     --- PASS: TestFoo (0.00s)
        #     --- FAIL: TestFoo/subtest_name (0.01s)
        #     --- SKIP: TestBar (0.00s)
        # Capturing (\S+) takes the test node id only and stops before the
        # timing, so the same test yields an identical name in all three stages.
        # The subtest suffix ("Parent/child") is deliberately preserved -
        # truncating to the parent would collapse table-driven cases into one
        # colliding name.
        re_result = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")

        # Test names must be qualified by package. This repo defines identical
        # test function names in several packages (e.g. TestWorkerReady exists in
        # pkg/controllers/v1, /v1alpha1 and /v1alpha2), and bare names collapse
        # them into a single entry - 51 raw PASS lines parsed to 25 names on
        # pr-438 before this was added.
        #
        # `go test` buffers each package and terminates its block with a summary
        # line, so tests are held in `pending` and attributed when that line
        # arrives:
        #     ok      github.com/org/repo/pkg/foo   0.10s
        #     FAIL    github.com/org/repo/pkg/foo   [build failed]
        #     ?       github.com/org/repo/pkg/foo   [no test files]
        # A bare "FAIL"/"ok" with no package does not match (\s+\S+ is required).
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+)")

        module_prefix = f"github.com/{self.pr.org}/{self.pr.repo}/"
        pending: list[tuple[str, str]] = []

        def flush(package: str) -> None:
            # Strip the module path for readability; the remainder is still
            # unique within the repo. Unqualified only if no summary line was
            # seen (a panicking package) - rare, and preferable to guessing.
            short = package[len(module_prefix):] if package.startswith(module_prefix) else package
            for status, name in pending:
                full = f"{short}.{name}" if short else name
                if status == "PASS":
                    passed_tests.add(full)
                elif status == "FAIL":
                    failed_tests.add(full)
                else:
                    skipped_tests.add(full)
            pending.clear()

        for line in log.splitlines():
            match = re_result.match(line)
            if match:
                pending.append((match.group(1), match.group(2)))
                continue

            pkg_match = re_pkg.match(line)
            if pkg_match:
                flush(pkg_match.group(1))

        # Anything still unattributed (package produced no summary line) is
        # emitted bare rather than dropped.
        flush("")

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A retried or re-reported test can appear under more than one
        # status, so failure wins, then skip.
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
