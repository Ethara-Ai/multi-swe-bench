import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop binary diff sections, which ``git apply`` rejects for lack of a full
    index line and which would abort the whole apply under ``set -e``.

    Neither patch in this record carries a binary section today (fix touches
    README.md + 4 .go files, test touches pkg/mapper/mapper_test.go only), so
    this is a standing guard rather than a live filter -- it is kept so the file
    behaves identically to its sibling the day a fixture-bearing PR is added.

    Splitting on the ``diff --git`` header instead of parsing it sidesteps R19's
    ``\\S+``-vs-spaces trap entirely: a path containing spaces still starts its
    own section, so its payload can never leak into the previous one.

    Do NOT add a ``go.sum`` filter here. Stripping the lock file leaves go.mod
    requiring a module go.sum cannot verify, which forces ``-mod=mod`` to refetch
    and rewrite it from the network at eval time. Keeping it is what lets the run
    scripts resolve offline from the module cache prepare.sh warmed.
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


class StatsdExporterImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # Toolchain measured at this record's base.sha (64dd103e), NOT
        # "golang:latest": .circleci/config.yml pins `circleci/golang:1.15` and
        # go.mod declares `go 1.13`. Before Go 1.21 the go.mod directive is a
        # floor the toolchain may exceed, so 1.15 -- the version upstream CI
        # actually built and tested with -- satisfies both. Neither the fix nor
        # the test patch touches go.mod, so one toolchain serves all three
        # stages and this repo has a single era.
        return "golang:1.15"

    def image_tag(self) -> str:
        # Per-PR base (blackbox_exporter model): each PR gets its own base image
        # so DockerfileEnhancer can safely pin it to THIS PR's ${BASE_COMMIT} and
        # prune. A shared toolchain-versioned base could not be enhancer-pinned
        # without violating R10.
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
        # proxy ARGs, SSL_CERT_FILE/CA_CERT_PATH ENVs + the CA-cert symlinks,
        # OCI labels, the standardized ${REPO_URL} fetch, `git checkout
        # ${BASE_COMMIT}` and Image._HARDENING_BLOCK -- all into this per-PR base.
        #
        # Deliberately NO apt-get. `golang:1.15` is Debian buster, whose
        # deb.debian.org suites are gone (R11), and the enhancer's EOL rewrite
        # only fires from the DEFAULT dockerfile() -- never from a custom one
        # like this. Not running apt at all is what makes that moot: the official
        # image already ships git and /etc/ssl/certs/ca-certificates.crt, which
        # is everything this base needs.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}
"""


class StatsdExporterImageDefault(Image):
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
        return StatsdExporterImageBase(self.pr, self.config)

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
# the module graph is warmed here; every eval stage then resolves from the cache.
# No GOWORK handling: go.work is a Go 1.18 feature and this 2020-era tree has
# none, so there is no workspace mode for -mod=mod to be invalid in.
export GOPROXY=https://proxy.golang.org,direct
export GOFLAGS=-mod=mod
# Pinned, not inherited. CGO_ENABLED is part of the build cache key, so it must
# be the SAME here as in the three eval stages or every stage recompiles from
# scratch and this warm is wasted. 1 is the golang:1.15 default this record was
# measured under; the dependency graph is pure Go, so nothing actually links C.
export CGO_ENABLED=1

go mod download -x 2>&1 || true
go build ./... 2>&1 || true

# Warms the TEST build cache as well, so the three eval stages only recompile
# what the patches actually change. Neither patch adds a go.mod requirement --
# fix touches README.md + 4 .go files, test touches mapper_test.go only -- so
# warming at base provably covers the post-patch graph too, and there is no need
# to apply the patches here just to pull new modules.
go test -count=1 ./... 2>&1 || true

# `go mod download`/`go build` may rewrite go.sum; restore the tracked tree so
# the eval stages start from a pristine base and `git apply` cannot conflict.
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

# Resolve only from the module cache prepare.sh warmed: no network at eval time.
export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off  # already verified against sum.golang.org during prepare.sh
export GOFLAGS=-mod=mod
export CGO_ENABLED=1  # must match prepare.sh: it is part of the build cache key
export CI=true

go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off
export GOFLAGS=-mod=mod
export CGO_ENABLED=1
export CI=true

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

# The gold tests are YAML-driven table cases, so pkg/mapper still COMPILES with
# only the test patch applied and the new cases fail at RUNTIME. That is why
# this instance grades as a true FAIL->PASS (f2p) rather than the NONE->PASS
# (n2p) shape a compile-coupled Go patch would produce.
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

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
export CGO_ENABLED=1
export CI=true

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


# The dataset row carries no `number_interval`, so Instance.create()
# (instance.py:41-49) computes the PLAIN key `prometheus/statsd_exporter`. Under
# R26/§17.4 that is exactly the case where a `<repo>_<hi>_to_<lo>` range file
# would be unreachable, so this repo ships as a single `<repo>.py` registered
# under the plain key -- the same shape as its sibling blackbox_exporter.py.
@Instance.register("prometheus", "statsd_exporter")
class StatsdExporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StatsdExporterImageDefault(self.pr, self._config)

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

        # Strip ANSI ONCE, before the loop, not per line. `go test` does not
        # colourize its own output -- all three captured stage logs contain zero
        # escape bytes -- but a wrapper or a future toolchain that did would
        # defeat every anchored pattern below, and the resulting empty sets are
        # reported by the harness as "no test results were captured" rather than
        # as the parse failure they actually are.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Only the per-test result lines are authoritative:
        #     --- PASS: TestFoo (0.00s)
        #     --- FAIL: TestFoo/subcase (0.01s)
        #     --- SKIP: TestFoo (0.00s)
        #
        # Do NOT add a broad `FAIL:?\s?(.+?)\s` pattern: it also matches go's
        # PACKAGE summary lines, e.g.
        #     FAIL	github.com/prometheus/statsd_exporter/pkg/mapper	0.051s
        # recording the import path as a phantom failed TEST and corrupting
        # failed_count / the f2p/p2p sets.
        re_pass_tests = [re.compile(r"^--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"^--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"^--- SKIP: (\S+)")]

        # The line that TERMINATES one package's output block:
        #     ok  	github.com/prometheus/statsd_exporter/pkg/mapper	0.037s
        #     FAIL	github.com/prometheus/statsd_exporter/pkg/mapper	0.051s
        #     FAIL	github.com/prometheus/statsd_exporter/pkg/mapper [build failed]
        #     ?   	github.com/prometheus/statsd_exporter/pkg/metrics	[no test files]
        # `go test ./...` buffers each package's output and emits it contiguously,
        # so every result line still pending when a terminator appears belongs to
        # the package that terminator names. Matched against the RAW line, since a
        # real terminator always starts at column 0 -- stripping first would let
        # an indented `t.Log("ok  something")` masquerade as one.
        re_package_end = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

        def qualified(package: str, test_name: str) -> str:
            # `go test -v` prints a BARE `TestFoo`, which is not unique across
            # packages: this repo defines TestTtlExpiration twice, in the root
            # package (bridge_test.go:636) and in pkg/exporter
            # (pkg/exporter/exporter_test.go:843). Unqualified, those two merge
            # into one entry -- and `passed_tests -= failed_tests` below would
            # then let either one erase the other, inventing or destroying an f2p
            # transition with nothing in the log to show for it.
            #
            # The subtest suffix is deliberately NOT collapsed with rfind("/"):
            # the gold transition here is 9 SUBTESTS of TestMetricMapperYAML plus
            # their parent, and collapsing would throw exactly that away.
            #
            # Qualification is a pure function of what go itself printed, so the
            # name is byte-identical in all three stages (R3).
            return f"{package}::{test_name}"

        pending: list[tuple[set, str]] = []

        for line in clean_log.splitlines():
            stripped = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(stripped)
                if pass_match:
                    pending.append((passed_tests, pass_match.group(1)))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(stripped)
                if fail_match:
                    pending.append((failed_tests, fail_match.group(1)))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(stripped)
                if skip_match:
                    pending.append((skipped_tests, skip_match.group(1)))

            package_end_match = re_package_end.match(line)
            if package_end_match:
                verdict, package = package_end_match.groups()
                for bucket, test_name in pending:
                    bucket.add(qualified(package, test_name))
                pending = []

                # A package that fails to COMPILE emits no `--- FAIL:` lines at
                # all, only `FAIL\t<pkg> [build failed]`, so an unrecorded compile
                # knockout is indistinguishable from a clean run (failed_count ==
                # 0). Record the BARE import path -- never qualified, so it can
                # never collide with a real test name. It is not creditable: a
                # package summary cannot reach PASS, so it stays out of
                # f2p/n2p/p2p and only makes the failure visible.
                if verdict == "FAIL":
                    failed_tests.add(package)

        # Result lines with no terminator mean the log was truncated mid-package
        # (an OOM kill; `docker_util.run` has no timeout to hit). Their package is
        # unknowable, so they CANNOT be named consistently with the other stages
        # -- inventing a placeholder would manufacture transitions. Drop them and
        # record one fixed marker instead: it never reaches PASS in any stage, so
        # it credits nothing, and it makes a truncated stage visible as a failure
        # rather than as a suspiciously short clean run.
        if pending:
            failed_tests.add("[truncated: package block without terminator]")

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
