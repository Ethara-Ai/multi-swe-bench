import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop binary diff sections, which ``git apply`` rejects for lack of a full
    index line and which would abort the whole apply under ``set -e``.

    No patch in this dataset carries a binary section today -- the five records
    touch only .go, .md, .yaml and .json paths -- so this is a standing guard
    rather than a live filter. It is kept because the CRD/bundle regeneration
    these PRs perform is exactly the kind of change that grows a fixture, and
    because a rejected apply grades as "0 tests collected" (§8) rather than as
    the patch failure it actually is.

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


class PrometheusOperatorImageBase(Image):
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
        # Carrier only. golang:1.20 is the newest toolchain any PR in this range
        # needs (PR 5221), and it also supplies the git/gcc/curl/ca-certificates
        # this base is built on. The four OLDER toolchains are installed beside
        # it below -- see the dockerfile() note on why one base must carry five.
        return "golang:1.20"

    def image_tag(self) -> str:
        # ONE shared base for the whole PR range, named from the observed
        # endpoints, highest first (§17.1) -- the same shape as the sibling
        # surrealdb base `base-5831-to-241`.
        return "base-5221-to-3810"

    def workdir(self) -> str:
        # MUST track image_tag(): the build-context directory is derived from
        # workdir().
        return "base-5221-to-3810"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The `# syntax` directive on line 1 is the enhancer's skip-sentinel
        # (image.py:317): DockerfileEnhancer.enhance() returns this text
        # VERBATIM. That is exactly what a shared base needs, because otherwise
        # _standardize_repo_fetch (image.py:355) would rewrite the clone below
        # into `clone + git checkout ${BASE_COMMIT} + hardening`, pinning a base
        # shared by five PRs to whichever one happened to build it first and
        # pruning the other four commits out of the image (R10). Bypassing the
        # enhancer means the infrastructure it would have injected -- TARGETARCH,
        # the proxy ARGs, the SSL_CERT_FILE/CA_CERT_PATH ENVs, the OCI labels and
        # the CA-cert symlinks -- has to be written out here by hand, which is
        # what the block below does.
        #
        # NO TOOLCHAIN PINNING HERE. Each PR's base commit wants a different Go
        # version, so pinning is a PER-PR concern and lives in that image's
        # staged prepare.sh. Installing all five here
        # would put four unused SDKs (~1.8 GB) into a layer every instance
        # inherits; installing one here would force a 2021 Kubernetes dependency
        # graph through whichever compiler the base happened to pick. This image
        # supplies only what every era shares: git, gcc, curl, ca-certificates
        # and the clone.
        #
        # Deliberately NO apt-get: the golang image already ships git, gcc, curl
        # and /etc/ssl/certs/ca-certificates.crt, and the packages under test are
        # pure Go with no system library to link against. That also sidesteps
        # R11 entirely.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PrometheusOperatorImageDefault(Image):
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
        return PrometheusOperatorImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def go_version(self) -> str:
        # The ONE era-dependent value in this image, resolved in ONE place and
        # consumed only by prepare.sh, so nothing can disagree about which SDK
        # the caches were warmed with. Each entry is the last patch release
        # of the minor version that PR's own base commit pins, read from its
        # go.mod directive and its CI pin:
        #     5221 dd19613873  go.mod 1.20  .github/env 1.20
        #     5203 196b46eef9  go.mod 1.18  .github/env 1.19   <- CI wins
        #     4988 01c771cefb  go.mod 1.18  .github/env 1.18
        #     4590 961c3336c8  go.mod 1.17  .github/env 1.17
        #     3810 be7ac03166  go.mod 1.15  ci.yaml     1.15
        # Thresholds are the observed PR numbers (Option B, §3.3): the dataset
        # carries no number_interval, so a file-per-range split is unreachable
        # (R26, §17.4) and the era table has to live inside the one class.
        if self.pr.number >= 5221:
            return "1.20.14"
        if self.pr.number >= 5203:
            return "1.19.13"
        if self.pr.number >= 4988:
            return "1.18.10"
        if self.pr.number >= 4590:
            return "1.17.13"
        return "1.15.15"

    def files(self) -> list[File]:
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        # Fixed path, deliberately not a per-era one: extra_setup() has already
        # placed THIS PR's SDK there, so the staged scripts need no era value and
        # cannot drift from the image they run in.
        go_root = "/usr/local/go-pinned"

        return [
            File(
                ".",
                "fix.patch",
                filtered_fix_patch,
            ),
            File(
                ".",
                "test.patch",
                filtered_test_patch,
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
# The shared base cloned at the default branch, not at this PR's commit -- the
# `# syntax` directive keeps DockerfileEnhancer from pinning a five-PR base to
# one arbitrary sha (R10), so pinning is this script's job. All five base
# commits sit on master/main and are reachable from a plain clone, so no R12
# refs/pull fetch is needed; verified by checking each sha out in a throwaway
# container.
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# This PR's Go toolchain, pinned to the version its own base commit was built
# with. Installed HERE rather than as a Dockerfile RUN because R16 puts installs
# in prepare.sh -- build time, network available, output not parsed -- and
# because it keeps every per-PR Dockerfile to the one shape its siblings use:
# FROM, ARG BASE_COMMIT, COPY, prepare, harden, with no era-specific layer
# wedged in between.
#
# The version test is a SHELL conditional, so no PR-number logic reaches the
# Dockerfile at all. When the carrier image's own Go already IS the pinned
# version -- true for PR 5221, whose golang:1.20 base is go1.20.14 -- this
# symlinks instead of re-fetching ~150 MB to arrive at the same compiler.
want="{go_version}"
have="$(/usr/local/go/bin/go env GOVERSION 2>/dev/null | sed 's/^go//')"
if [ "$want" = "$have" ]; then
  ln -sfn /usr/local/go {go_root}
else
  arch="$(dpkg --print-architecture)"
  curl -fsSL "https://go.dev/dl/go$want.linux-$arch.tar.gz" -o /tmp/go.tgz
  mkdir -p {go_root}
  tar -C {go_root} --strip-components=1 -xzf /tmp/go.tgz
  rm -f /tmp/go.tgz
fi

# GOROOT is set explicitly rather than inferred, so the carrier image's own
# /usr/local/go can never pair its binary with the pinned SDK's sources. The
# three eval scripts export the same fixed path, which is why they carry no era
# value of their own.
export GOROOT={go_root}
export PATH={go_root}/bin:$PATH
go version

# Image build is the only point where the network is legitimately available, so
# the module graph is warmed here; every eval stage then resolves from the cache.
export GOPROXY=https://proxy.golang.org,direct
export GOFLAGS=-mod=mod
# Pinned, not inherited. CGO_ENABLED is part of the build cache key, so it must
# be the SAME here as in the three eval stages or every stage recompiles from
# scratch and this warm is wasted. 1 is the golang image default these records
# were measured under; the packages under test are pure Go, so nothing links C.
export CGO_ENABLED=1

go mod download 2>&1 || true

# The SAME package expression the three eval stages use, so the warm covers
# exactly what they compile. `|| true` because a resolution failure here must
# not abort the build -- the eval stage is where it should surface.
pkgs=$(go list ./... 2>/dev/null | grep -v /test/ | grep -v /contrib/) || true

go build ./... 2>&1 || true

# `-run '^$'` compiles and links every test binary without executing a single
# test: it warms the TEST build cache so the three eval stages only recompile
# what the patches change, and it does it without running anything whose result
# could be mistaken for a baseline.
go test -count=1 -run '^$' $pkgs 2>&1 || true

# `go mod download`/`go build` may rewrite go.mod or go.sum under -mod=mod;
# restore the tracked tree so the eval stages start from a pristine base and
# `git apply` cannot conflict.
git checkout -- . 2>&1 || true
git clean -fd 2>&1 || true

bash /home/check_git_changes.sh

""".format(pr=self.pr, go_root=go_root, go_version=self.go_version()),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Same toolchain prepare.sh warmed the caches with. A different SDK here would
# invalidate the whole build cache and change nothing else.
export GOROOT={go_root}
export PATH={go_root}/bin:$PATH

# Resolve only from the module cache prepare.sh warmed: no network at eval time.
export GOPROXY=file://$(go env GOMODCACHE)/cache/download
export GOSUMDB=off  # already verified against sum.golang.org during prepare.sh
export GOFLAGS=-mod=mod
export CGO_ENABLED=1  # must match prepare.sh: it is part of the build cache key
export CI=true

# Upstream's own unit scope, copied verbatim from the Makefile's
#     pkgs = $(shell go list ./... | grep -v /test/ | grep -v /contrib/)
# which is byte-identical at all five base commits. Excluding /test/ is what
# keeps ./test/e2e out: its TestMain takes --kubeconfig and drives a live kind
# cluster (Makefile `test-e2e`), so it can neither build a verdict nor fail
# honestly inside this container. Every gold unit test in this dataset lives
# under pkg/, so nothing creditable is lost.
#
# `-count=1` disables the result cache; `-v` is what emits the `--- PASS:` /
# `--- FAIL:` lines parse_log reads. No `-short` (that is upstream `test-unit`,
# which skips cases) and no `-race` (upstream `test-long` omits it too).
pkgs=$(go list ./... | grep -v /test/ | grep -v /contrib/) || true
if [ -z "$pkgs" ]; then
  echo "run: go list produced no packages" >&2
  exit 1
fi

go test -count=1 -v $pkgs

""".format(pr=self.pr, go_root=go_root),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export GOROOT={go_root}
export PATH={go_root}/bin:$PATH
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

pkgs=$(go list ./... | grep -v /test/ | grep -v /contrib/) || true
if [ -z "$pkgs" ]; then
  echo "test-run: go list produced no packages" >&2
  exit 1
fi

# Several of these gold tests call an API the fix patch introduces, so the
# package fails to COMPILE at this stage and emits `FAIL <pkg> [build failed]`
# with no per-test lines. That is the compile-coupled shape of §14.4: those
# tests grade NONE->PASS (n2p) rather than FAIL->PASS (f2p), and the instance is
# still valid. Measured on PR 5221: pkg/operator build-fails here and is `ok` at
# the fix stage.
go test -count=1 -v $pkgs

""".format(pr=self.pr, go_root=go_root),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export GOROOT={go_root}
export PATH={go_root}/bin:$PATH

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

pkgs=$(go list ./... | grep -v /test/ | grep -v /contrib/) || true
if [ -z "$pkgs" ]; then
  echo "fix-run: go list produced no packages" >&2
  exit 1
fi

go test -count=1 -v $pkgs

""".format(pr=self.pr, go_root=go_root),
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

        # This image chains to an Image OBJECT, so DockerfileEnhancer.enhance()
        # returns the text verbatim (image.py:315) and injects nothing -- the
        # clone already happened in the shared base, and whatever isolation this
        # instance gets, it gets from the hardening block below (R9).
        #
        # No WORKDIR line here: the shared base ends on
        # `WORKDIR /home/<repo>` and a derived image inherits it, so repeating it
        # would only restate what the base already guarantees. The hardening
        # block below runs its git commands relative to that inherited
        # directory -- which is why the base, not this file, is the right place
        # for it to be declared.
        #
        # Hardening is deliberate (§4.2): without it an agent solving this
        # instance can read the gold fix out of a future commit with
        # `git log --all` / `git cat-file --batch-all-objects`. It is doubly
        # load-bearing here, because the shared base clones the FULL default
        # branch -- every commit after this PR's base is present in the image
        # until this block prunes it. No safe.directory line is needed (R13)
        # because prepare.sh never chowns the repo: every stage runs as root, the
        # same uid that cloned it.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


# Every row in this dataset leaves `number_interval` empty, so Instance.create()
# (instance.py:41-49) computes the PLAIN key `prometheus-operator/prometheus-operator`.
# Under R26/§17.4 that is exactly the case where a `<repo>_<hi>_to_<lo>` range
# file would be unreachable, so this repo ships as a single `<repo>.py`
# registered under the plain key. The PR range still names the shared base image
# (`base-5221-to-3810`), which is where the range label belongs.
@Instance.register("prometheus-operator", "prometheus-operator")
class PrometheusOperator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PrometheusOperatorImageDefault(self.pr, self._config)

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
        # colourize its own output -- the captured stage logs contain zero escape
        # bytes -- but a wrapper or a future toolchain that did would defeat
        # every anchored pattern below, and the resulting empty sets are reported
        # by the harness as "no test results were captured" rather than as the
        # parse failure they actually are.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Only the per-test result lines are authoritative:
        #     --- PASS: TestFoo (0.00s)
        #     --- FAIL: TestFoo/subcase (0.01s)
        #     --- SKIP: TestFoo (0.00s)
        #
        # Do NOT add a broad `FAIL:?\s?(.+?)\s` pattern: it also matches go's
        # PACKAGE summary lines, e.g.
        #     FAIL	github.com/prometheus-operator/prometheus-operator/pkg/operator	0.051s
        # recording the import path as a phantom failed TEST and corrupting
        # failed_count / the f2p/p2p sets.
        re_pass_tests = [re.compile(r"^--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"^--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"^--- SKIP: (\S+)")]

        # The line that TERMINATES one package's output block:
        #     ok  	github.com/prometheus-operator/prometheus-operator/pkg/k8sutil	0.796s
        #     FAIL	github.com/prometheus-operator/prometheus-operator/pkg/operator [build failed]
        #     ?   	github.com/prometheus-operator/prometheus-operator/pkg/api	[no test files]
        # `go test <pkgs>` buffers each package's output and emits it
        # contiguously, so every result line still pending when a terminator
        # appears belongs to the package that terminator names. Matched against
        # the RAW line, since a real terminator always starts at column 0 --
        # stripping first would let an indented `t.Log("ok  something")`
        # masquerade as one. The bare trailing `FAIL` go prints as its final line
        # has no second field and so cannot match.
        re_package_end = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

        def qualified(package: str, test_name: str) -> str:
            # `go test -v` prints a BARE `TestFoo`, which is not unique across
            # packages. Measured on PR 5221's baseline log: of 757 result lines,
            # 33 bare names are emitted by more than one package --
            # TestPodLabelsAnnotations by four (pkg/alertmanager, pkg/k8sutil,
            # pkg/prometheus/server, pkg/thanos), TestStatefulSetPVC and
            # TestPodTemplateConfig by three each. Unqualified, those merge into
            # one entry -- and `passed_tests -= failed_tests` below would then
            # let either one erase the other, inventing or destroying an f2p
            # transition with nothing in the log to show for it.
            #
            # The subtest suffix is deliberately NOT collapsed with rfind("/"):
            # six of PR 5221's seven creditable transitions ARE subtests of
            # TestMakeRulesConfigMaps, and collapsing would throw them away.
            #
            # `<package>::<test>` is also the shape report.py can classify:
            # _candidate_identifiers (report.py:517) splits on "::" and recovers
            # the bare `TestFoo`, so _authored_via_diff ties a top-level test
            # back to the gold patch's added `func TestFoo(` line (R20; verified
            # for TestMakeRulesConfigMaps). A SUBTEST name keeps its "/" and
            # matches nothing in the diff, so _touched_by_test_patch falls
            # through to its fail-open return (report.py:168) -- which is the
            # correct verdict here anyway, and the cheating guard stays armed
            # because it is _touched_by_fix_patch, not this path, that gates it.
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
                # all, only `FAIL\t<pkg> [build failed]`, so an unrecorded
                # compile knockout is indistinguishable from a clean run
                # (failed_count == 0). That is the normal test-stage shape here
                # (§14.4), so recording it is what makes the stage legible.
                # Record the BARE import path -- never qualified, so it can never
                # collide with a real test name. It is not creditable: a package
                # summary cannot reach PASS, so it stays out of f2p/n2p/p2p and
                # only makes the failure visible. Do NOT also record `ok <pkg>`
                # as a pass: that would pair with this entry to mint a
                # package-level FAIL->PASS for every compile-coupled instance,
                # routing around the CBC demotion report.py:290-294 performs on
                # purpose.
                if verdict == "FAIL":
                    failed_tests.add(package)

        # Result lines with no terminator mean the log was truncated mid-package
        # (an OOM kill; `docker_util.run` has no timeout to hit). Their package
        # is unknowable, so they CANNOT be named consistently with the other
        # stages -- inventing a placeholder would manufacture transitions. Drop
        # them and record one fixed marker instead: it never reaches PASS in any
        # stage, so it credits nothing, and it makes a truncated stage visible as
        # a failure rather than as a suspiciously short clean run.
        if pending:
            failed_tests.add("[truncated: package block without terminator]")

        # `go test` reports subtests separately, and a re-listed name can appear
        # under more than one status. Reconcile with failure winning, then skip,
        # so the three sets stay disjoint -- otherwise TestResult.__post_init__
        # raises and the instance run dies (R2).
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
