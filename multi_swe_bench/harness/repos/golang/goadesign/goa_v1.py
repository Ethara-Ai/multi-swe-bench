"""goadesign/goa — v1 era (`base.ref == "v1"`, 5 instances, tags v1.1.0 .. v1.4.2).

The oldest era (≈2018). No go.mod; pre-modules GOPATH layout; self-import path
is `github.com/goadesign/goa`; tests use the ginkgo/gomega BDD framework. The
original `go get ./...` CI is unreproducible (unpinned HEADs of 2018-era deps
have since moved/renamed).

Same synthesize-go.mod strategy as the v2 config, with two v1-specific fixes
discovered interactively:
  * `github.com/armon/go-metrics` was renamed to `github.com/hashicorp/go-metrics`
    at v0.5+, so a bare `go mod tidy` fails ("module declares its path as ...").
    Pin it via -replace to the last pre-rename tag (v0.4.1).
  * go directive pinned to 1.24 with GOTOOLCHAIN=local (NOT 1.25/auto): the
    go1.25 amd64 toolchain SIGSEGVs under the buildx QEMU emulator, breaking
    multiarch (linux/amd64) builds on an arm64 host. go<=1.24 emulates fine.
  * x/net / x/tools pinned to go<=1.24 tags (v0.30.0); satori/go.uuid replaced
    with its maintained successor gofrs/uuid (2018 API rot). See synth_gomod.sh.

Verified interactively in Docker (golang:1.24-bookworm, linux/arm64) on the
latest v1 base d96a9e2 (v1.4.1..v1.4.2):
  go mod init github.com/goadesign/goa
  go mod edit -go=1.25
  go mod edit -replace=github.com/armon/go-metrics=github.com/armon/go-metrics@v0.4.1
  go mod tidy   => RC 0 ;  go build ./...  => RC 0
  go test ./... => runs (36 PASS / 6 FAIL; some 2018-era packages [build failed]
    against the modern dep graph). Baseline failures are expected — the harness
    scores fix-induced transitions, not absolute pass. v1 is the lowest-
    confidence era (only 5 instances, heaviest dependency rot).

Cross-stage determinism: identical to the v2 config — all three stages
(run/test/fix) re-run `synth_gomod.sh` at scored-run time so the baseline and
patched trees are constructed the same way (differing only by the dataset
patch). Unpinned `go mod tidy` is not bit-reproducible across time, but any
resulting flake produces an invalid Report that Report.check() drops safely
rather than mis-scoring f2p. Documented property of pre-modules dependency rot.

Routing: the raw dataset carries an empty number_interval for every era, so all
rows route to the single `goadesign/goa` Instance (goa.py), which dispatches
here by `base.ref == "v1"`. This module exports only the Image classes; it no
longer registers its own Instance.
"""

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.golang.goadesign.goa import _harden_block


class GoaV1ImageBase(Image):
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
        # Multi-arch manifest (linux/amd64 + linux/arm64).
        return "golang:1.24-bookworm"

    def image_tag(self) -> str:
        return "base-v1"

    def workdir(self) -> str:
        return "base-v1"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        # ${REPO_URL} is passed as a build-arg by build_dataset (dep is a str →
        # base image); declared as an ARG below so the clone resolves.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # See goa.py GoaImageBase for the full rationale: the leading
        # `# syntax=docker/dockerfile:1.6` directive makes DockerfileEnhancer
        # emit this Dockerfile VERBATIM, so the enhancer does NOT rewrite the
        # clone into a `git checkout ${BASE_COMMIT}` + history-strip block that
        # would pin this SHARED v1 base (tag `base-v1`) to one PR's commit.
        # Per-PR history hardening is applied in GoaV1ImageDefault.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=local
ENV GOFLAGS=-mod=mod
ENV CI=true
# Go's signal-based async preemption (Go 1.14+) crashes under QEMU user-mode
# emulation (SIGSEGV/SIGILL with a register dump) when building linux/amd64 on
# an arm64 host. Disabling it makes cross-arch (QEMU) multiarch builds robust;
# native-arch builds are unaffected. Inherited by prepare/run/test/fix stages.
ENV GODEBUG=asyncpreemptoff=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates protobuf-compiler && rm -rf /var/lib/apt/lists/*

RUN GOTOOLCHAIN=auto go install google.golang.org/protobuf/cmd/protoc-gen-go@latest \\
 && GOTOOLCHAIN=auto go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest \\
 || true
ENV PATH="/go/bin:${{PATH}}"

{code}

CMD ["/bin/bash"]
"""


class GoaV1ImageDefault(Image):
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
        return GoaV1ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "synth_gomod.sh",
                """#!/bin/bash
# Synthesize a go.mod for the pre-modules v1 tree (module path
# github.com/goadesign/goa), unless one already exists. Idempotent.
#
# MULTIARCH-CRITICAL: pin the go directive to 1.24 and force GOTOOLCHAIN=local.
# A go directive of 1.25 makes Go fetch the go1.25 toolchain, whose amd64
# binary SIGSEGVs under the buildx QEMU emulator when building linux/amd64 on
# an arm64 host (verified: arm64 native OK, amd64-QEMU exit 2). go<=1.24 amd64
# emulates cleanly (v2/v3 prove it), so pinning to 1.24 makes v1 build on BOTH
# arches identically. The replaces below let the dep graph resolve under 1.24:
#   * armon/go-metrics: renamed to hashicorp/go-metrics at v0.5+ -> pin v0.4.1
#   * x/net / x/tools: latest tags require go>=1.25 -> pin to go<=1.24 tags
#   * satori/go.uuid: 2018 API rot (uuid.Must/NewV4 signature); the maintained
#     drop-in successor gofrs/uuid builds every v1 base (1497 v1.3.x .. 2000
#     v1.4.x). Verified: both bases tidy=0 build=0, 13+ pkgs `ok`. Remaining
#     [build failed] pkgs (apidsl "Default redeclared", flaky sampler_test)
#     are pre-existing v1-era source rot, identical on both arches.
set -e
cd /home/{pr.repo}
export GOTOOLCHAIN=local GOFLAGS=-mod=mod
if [ ! -f go.mod ]; then
    go mod init github.com/goadesign/goa
fi
go mod edit -go=1.24
go mod edit -replace=github.com/armon/go-metrics=github.com/armon/go-metrics@v0.4.1
go mod edit -replace=golang.org/x/net=golang.org/x/net@v0.30.0
go mod edit -replace=golang.org/x/tools=golang.org/x/tools@v0.30.0
go mod edit -replace=github.com/satori/go.uuid=github.com/gofrs/uuid@v4.0.0+incompatible
# Sirupsen/logrus was rename-deprecated to lowercase sirupsen/logrus (~2017).
# Modern Go strictly enforces module-path case, so old v1 commits that still
# import the capital-S path (e.g. goadesign/goa/logging/logrus) fail tidy with
# "module declares its path as: sirupsen/logrus but was required as: Sirupsen/logrus".
# Pin via -replace to the canonical lowercase path so the import resolves.
go mod edit -replace=github.com/Sirupsen/logrus=github.com/sirupsen/logrus@v1.9.4
# 2025 dep-rot: onsi/gomega (v1.42.1+) and go-openapi/loads (v0.24.0+, a
# transitive dep of the swagger codegen) both bumped their go directive to
# >=1.25, so an UNPINNED `go mod tidy` under GOTOOLCHAIN=local+go1.24 fails
# ("toolchain upgrade needed to resolve ...") and yields ZERO tests. Pin both
# to their last go<=1.24 tags (gomega v1.38.0 = go1.23; loads v0.22.0 = go1.20,
# which drags its go-openapi siblings to pre-go1.25 versions via MVS). Verified:
# tidy RC 0, `go build ./...` clean. Classic ginkgo v1 (github.com/onsi/ginkgo,
# unmaintained at v1.16.5 / go1.16) needs no pin.
go mod edit -replace=github.com/onsi/gomega=github.com/onsi/gomega@v1.38.0
go mod edit -replace=github.com/go-openapi/loads=github.com/go-openapi/loads@v0.22.0
GOTOOLCHAIN=local go mod tidy || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo} 2>/dev/null || true
git config user.email "msb@build" >/dev/null
git config user.name "msb-build" >/dev/null

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/synth_gomod.sh
export GOTOOLCHAIN=local GOFLAGS=-mod=mod
go build ./... || true
git add -A >/dev/null
git diff --cached --quiet || git commit -m "msb: synthesize go.mod for pre-modules v1" --quiet || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=local GOFLAGS=-mod=mod CI=true

# Construct the baseline IDENTICALLY to test-run/fix-run (minus patches): undo
# the prepare.sh go.mod commit back to the pristine base tree, then re-synth a
# fresh module graph at scored-run time. This keeps the dependency set
# consistent across all three stages (run/test/fix), so cross-stage test-name
# transitions reflect the patch — not `go mod tidy` resolving different
# unpinned dep versions at image-build time vs scored-run time.
git reset --hard HEAD~1 >/dev/null 2>&1 || true

bash /home/synth_gomod.sh
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=local GOFLAGS=-mod=mod CI=true

git reset --hard HEAD~1 >/dev/null 2>&1 || true

git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn -3 /home/test.patch || true

bash /home/synth_gomod.sh
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export GOTOOLCHAIN=local GOFLAGS=-mod=mod CI=true

git reset --hard HEAD~1 >/dev/null 2>&1 || true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || ( git apply --whitespace=nowarn /home/fix.patch && git apply --whitespace=nowarn /home/test.patch ) \\
  || git apply --whitespace=nowarn -3 /home/test.patch /home/fix.patch || true

bash /home/synth_gomod.sh
go test -mod=mod -vet=off -short -timeout 900s -v -count=1 ./...

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

        # Per-PR git-history hardening, applied AFTER prepare.sh checks out this
        # PR's base commit and commits the synthesized go.mod (so HEAD is the
        # synth commit and base.sha is its kept parent — see _harden_block).
        harden = _harden_block(self.pr.repo)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{harden}

{self.clear_env}
"""


# NOTE: this module intentionally does NOT register an Instance. All rows route
# to the single `goadesign/goa` Instance (goa.py), which dispatches here by
# base.ref. v1's log parsing is identical to the default (no flaky demotion), so
# goa.parse_goa_log(log) covers it.
