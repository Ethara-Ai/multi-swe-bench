"""goadesign/goa — v2 era (`base.ref == "v2"`, 14 instances, tags v2.0.0 .. v2.2.5).

v2 predates Go modules: there is NO go.mod in the tree and the self-import path
is `goa.design/goa` (vanity import, no /vN suffix). The original CI ran in
GOPATH mode with `GO111MODULE=off` + `go get ./...`. That is unreproducible
today: unpinned `go get` now resolves moved/renamed HEADs (verified failures:
`go.yaml.in/yaml/v4` 404, `oasdiff`->`invopop/yaml` import rename) so the build
breaks.

Robust approach (same philosophy as the pulumi old-era config): synthesize a
go.mod with the correct module path and let `go mod tidy` (under
GOTOOLCHAIN=auto, so a Go new enough for the resolved deps is fetched) pick an
MVS-consistent dependency set.

Verified interactively in Docker (golang:1.24-bookworm, linux/arm64) on the
latest v2 base 6566ea1 (v2.2.4..v2.2.5):
  go mod init goa.design/goa && go mod edit -go=1.22 && go mod tidy   => RC 0
  go build ./...                                                       => RC 0
  go test ./...                                                        => runs;
    many packages `ok` (goa.design/goa, codegen, dsl, eval, expr, http, ...),
    a few baseline FAILs (grpc/codegen, http/codegen, openapi) — expected; the
    harness scores fix-induced transitions, not absolute pass.

Cross-stage determinism: `go mod tidy` on a synthesized go.mod is inherently
unpinned, so the resolved dependency set is not bit-for-bit reproducible across
time. This is mitigated structurally: all three stages (run/test/fix) undo the
prepare-time go.mod commit and re-run `synth_gomod.sh` at scored-run time, so
within a single instance the baseline and patched stages are built the SAME way
(differing only by the dataset patch). Any residual environmental flake yields
an invalid Report that Report.check() drops safely — it cannot silently corrupt
f2p. v2 (14 instances) is a lower-confidence era than v3; this is a documented
property of pre-modules dependency rot, not a config defect.

Routing: dataset records for this era carry number_interval == "goa_v2".
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GoaV2ImageBase(Image):
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
        return "base-v2"

    def workdir(self) -> str:
        return "base-v2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Canonical, enhancer-aware repo fetch (see goa.py for rationale):
        # ${{REPO_URL}} is injected by DockerfileEnhancer and skipped by
        # _standardize_repo_fetch() — no hardcoded URL, multi-arch infra intact.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

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

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates protobuf-compiler && rm -rf /var/lib/apt/lists/*

RUN GOTOOLCHAIN=auto go install google.golang.org/protobuf/cmd/protoc-gen-go@latest \\
 && GOTOOLCHAIN=auto go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest \\
 || true
ENV PATH="/go/bin:${{PATH}}"

WORKDIR /home/

{code}

{self.clear_env}

"""


class GoaV2ImageDefault(Image):
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
        return GoaV2ImageBase(self.pr, self.config)

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
# Synthesize a go.mod for the pre-modules v2 tree (module path goa.design/goa),
# unless the checked-out/patched tree already provides one. Idempotent.
#
# MULTIARCH-CRITICAL (same rationale as goa_v1.py): pin the go directive to
# 1.24 with GOTOOLCHAIN=local. An unpinned `go mod tidy` pulls the LATEST of
# every dep, and the 2025 releases of x/*, grpc, protobuf and kin-openapi all
# bumped their go directive to >=1.25 -> Go would fetch the go1.25 toolchain,
# whose amd64 binary SIGSEGVs under the buildx QEMU emulator (breaks the
# linux/amd64 multiarch build). It also caused the test stage of patches that
# import x/tools/go/ast/astutil or kin-openapi/openapi3 (e.g. pr-2571) to
# produce zero tests -> invalid report. Pinning the go1.25-requiring modules
# to their last go<=1.24 tags keeps v2 on go1.24 (emulates cleanly on both
# arches) and makes the test stage deterministic. Verified in Docker on the
# v2 bases: pr-2571 test stage 552 PASS / 27 FAIL (was 0,0,0), pr-2262 1490
# PASS. The lone `tidy` warning ("no matching versions for .../testdata/dsls")
# is a self-referential internal test package the patch adds; `go test`
# resolves it locally, hence `|| true`.
set -e
cd /home/{pr.repo}
export GOTOOLCHAIN=local GOFLAGS=-mod=mod
if [ ! -f go.mod ]; then
    go mod init goa.design/goa
fi
go mod edit -go=1.24
go mod edit \\
    -replace=golang.org/x/net=golang.org/x/net@v0.30.0 \\
    -replace=golang.org/x/tools=golang.org/x/tools@v0.30.0 \\
    -replace=golang.org/x/sys=golang.org/x/sys@v0.26.0 \\
    -replace=golang.org/x/text=golang.org/x/text@v0.19.0 \\
    -replace=golang.org/x/crypto=golang.org/x/crypto@v0.28.0 \\
    -replace=golang.org/x/mod=golang.org/x/mod@v0.21.0 \\
    -replace=golang.org/x/sync=golang.org/x/sync@v0.8.0 \\
    -replace=google.golang.org/grpc=google.golang.org/grpc@v1.68.0 \\
    -replace=google.golang.org/protobuf=google.golang.org/protobuf@v1.35.1 \\
    -replace=github.com/getkin/kin-openapi=github.com/getkin/kin-openapi@v0.127.0
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

# Build a working module graph and commit it so the tree is clean for the
# downstream dataset patches (test-run/fix-run undo this commit first).
bash /home/synth_gomod.sh
export GOTOOLCHAIN=local GOFLAGS=-mod=mod
go build ./... || true
git add -A >/dev/null
git diff --cached --quiet || git commit -m "msb: synthesize go.mod for pre-modules v2" --quiet || true

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

# Undo the prepare.sh go.mod commit so the dataset patch applies against the
# pristine (no go.mod) base tree it was generated from.
git reset --hard HEAD~1 >/dev/null 2>&1 || true

git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn -3 /home/test.patch || true

# Re-synthesize the module graph on top of the patched tree.
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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("goadesign", "goa_v2")
class GOA_V2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GoaV2ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        # Environmentally non-deterministic tests: goa's adaptive/fixed traffic
        # samplers are probabilistic — `sampler_test.go` asserts tight bounds on
        # a random trueCount, so the SAME test flips PASS/FAIL across the
        # run/test/fix stages independently of the patch (verified for pr-2262:
        # run=FAIL, test=PASS, fix=FAIL with differing random counts each stage).
        # That trips Report.check()'s "test=PASS -> fix=FAIL = invalid" rule and
        # wrongly invalidates an otherwise-good instance. Demote these to SKIP so
        # cross-stage comparison ignores them (same technique as pulumi.py's
        # re_flaky). Matches the test func and any of its subtests.
        # Also demote TestHeader: goa v2's codegen header golden checks iterate
        # Go maps, so the header order is nondeterministic — TestHeader passes
        # or fails across stages independently of the patch. Verified flaky:
        # chunk2 pr-2402 shows run=FAIL/test=PASS/fix=FAIL while pr-2326 shows
        # PASS/PASS/FAIL (different outcomes in `run` with no patch applied yet).
        re_flaky = re.compile(r"^(TestAdaptiveSampler|TestFixedSampler|TestHeader)(/|$)")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if re_flaky.match(name):
                    passed_tests.discard(name)
                    failed_tests.discard(name)
                    skipped_tests.add(name)
                    continue
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                if re_flaky.match(name):
                    passed_tests.discard(name)
                    failed_tests.discard(name)
                    skipped_tests.add(name)
                    continue
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if re_flaky.match(name):
                    skipped_tests.add(name)
                    continue
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
