import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_IMAGE = "golang:1.25-bookworm"


class _ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # SINGLE shared toolchain base for every era (tag "base"). The `# syntax`
        # directive makes DockerfileEnhancer.enhance() return this verbatim: no
        # proxy args / cert symlinks / MITM mount injected. It must NOT clone the
        # repo -- the tag is shared by all 65 dataset PRs across all three era
        # modules, so a clone here would be force-pinned by the hardening pass to
        # whichever PR built the base first, breaking the rest. The clone lives
        # per-PR in _ImageDefault.
        #
        # BOTH protobuf-codegen toolchains are installed side by side: protoc-gen-go
        # is a single-name binary that cannot hold two versions at once, and old
        # LocalAI (<= PR 4999) regenerates .pb.go with protoc-gen-go v1.31.0 /
        # grpc v1.3.0 while newer PRs use v1.34.2 / grpc HEAD. Each era's prepare.sh
        # symlinks the version its checkout expects to /go/bin/protoc-gen-go before
        # `make protogen-go`, so one base serves all eras without a codegen mismatch.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ENV GOTOOLCHAIN=auto
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    clang \\
    cmake \\
    curl \\
    git \\
    pkg-config \\
    protobuf-compiler \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.31.0 && mv /go/bin/protoc-gen-go /go/bin/protoc-gen-go-1.31.0 && \\
    go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2 && mv /go/bin/protoc-gen-go /go/bin/protoc-gen-go-1.34.2 && \\
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.3.0 && mv /go/bin/protoc-gen-go-grpc /go/bin/protoc-gen-go-grpc-1.3.0 && \\
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@1958fcbe2ca8bd93af633f11e97d44e567e945af && mv /go/bin/protoc-gen-go-grpc /go/bin/protoc-gen-go-grpc-new

{self.clear_env}

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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
                "extract_packages.sh",
                """#!/bin/bash
# Patch-parsing helpers. Callers pass the patch files explicitly; nothing here
# reads a patch on its own. Only prepare.sh (build time, gold patches) calls
# _gold_pkg_dirs / _patch_files -- see gold_guard.sh for why that matters.

# Directories of every non-vendored .go file touched by the given patches.
# Deliberately NO existence filter: a package the gold test patch *creates* does
# not exist at the base commit and must still be tested once the patch applies.
# The existence filter is applied per stage by gold_guard.sh:gold_test_pkgs.
_gold_pkg_dirs() {
    grep -h '^diff --git a/' "$@" 2>/dev/null \\
        | sed 's|diff --git a/||;s| b/.*||' \\
        | grep '\\.go$' \\
        | grep -v '^vendor/' \\
        | xargs -r -I{} dirname {} \\
        | sort -u
}

# Every file touched by the given patches. Matches the framework's
# get_modified_files(test_patch), which backs fix_patch_tampers_with_tests.
_patch_files() {
    grep -h '^diff --git a/' "$@" 2>/dev/null \\
        | sed 's|diff --git a/||;s| b/.*||' \\
        | sort -u
}

_extract_test_names_in_pkg() {
    local pkg_path="$1"
    shift
    for patch in "$@"; do
        awk -v pkg="$pkg_path" '
            /^diff --git / {
                in_pkg = ($3 ~ ("^a/" pkg "/[^/]+_test\\.go$"))
            }
            in_pkg && /^\\+func +Test[A-Z]/ {
                if (match($0, /Test[A-Z][A-Za-z0-9_]*/)) {
                    print substr($0, RSTART, RLENGTH)
                }
            }
        ' "$patch" 2>/dev/null
    done | sort -u
}
""",
            ),
            File(
                ".",
                "gold_guard.sh",
                """#!/bin/bash
# Reward-integrity helpers (MSB-REWARD-003), sourced by run.sh / test-run.sh /
# fix-run.sh.
#
# At evaluation time run_evaluation bind-mounts the AGENT's patch over
# /home/fix.patch (Image.fix_patch_path()), so that file is agent-controlled
# while /home/test.patch stays gold. Everything scoring-relevant here therefore
# reads only the build-time-frozen /home/gold_pkgs.txt and
# /home/gold_test_files.txt, never /home/fix.patch. That keeps the set of tested
# packages, their order, and the set of protected test files identical across
# the run / test / fix stages regardless of what the agent submits.

: "${BASE_COMMIT:?BASE_COMMIT must be set (ENV BASE_COMMIT in the Dockerfile)}"

# Restore every file the GOLD test patch touches back to ${BASE_COMMIT}, undoing
# any edit an agent fix patch made to a scoring test. This is the per-image half
# of the tamper defence that run_evaluation's fix_patch_tampers_with_tests()
# enforces at the grader; it also catches hunks that landed via a partial apply
# and that a patch parser therefore never saw. Files absent at the base commit
# were created by the patch: remove them rather than checking them out.
restore_gold_test_files() {
    local f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        if git cat-file -e "${BASE_COMMIT}:$f" 2>/dev/null; then
            git checkout "${BASE_COMMIT}" -- "$f" 2>/dev/null || true
        else
            rm -f -- "$f"
        fi
    done < /home/gold_test_files.txt
}

# Strict apply, then 3-way. Never --reject: a partially applied GOLD test patch
# silently corrupts the F2P signal (the scoring test may simply be missing), and
# a partially applied FIX patch can land test-file hunks the parser-based tamper
# guard never saw. All-or-nothing keeps both failure modes visible.
apply_patch_strict() {
    local patch="$1" label="$2"
    if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
        echo "patch-apply: ${label} applied cleanly"
        return 0
    fi
    if git apply --3way --whitespace=nowarn "$patch" 2>/dev/null; then
        echo "patch-apply: ${label} applied via 3-way merge"
        return 0
    fi
    echo "patch-apply: ${label} FAILED TO APPLY (no partial application attempted)"
    return 1
}

# Regenerate the gRPC/protobuf bindings when a patch changes a .proto file.
# prepare.sh ran `make protogen-go` at ${BASE_COMMIT}, so the checked-in .pb.go
# only knows the messages that existed then. A patch that edits backend/*.proto
# adds new messages, and without a regen the package fails to compile with
# "undefined: pb.<NewMessage>" -- the whole package is then [build failed] and
# every test in it reads as NONE, which sinks an otherwise valid instance.
# Runs AFTER the patches are applied, unlike the build-time codegen.
regen_proto_if_patched() {
    grep -qE '^diff --git a/.*\\.proto' /home/test.patch /home/fix.patch 2>/dev/null || return 0
    echo "proto-regen: a patch touches .proto -- regenerating bindings"
    timeout 300 make protogen-go 2>&1 | tail -5 || true
}

# Frozen package list + a per-stage existence filter.
gold_test_pkgs() {
    local pkg result=""
    while IFS= read -r pkg; do
        [ -n "$pkg" ] || continue
        if [ -d "$pkg" ] && compgen -G "$pkg/*.go" > /dev/null 2>&1; then
            result="$result ./$pkg"
        fi
    done < /home/gold_pkgs.txt
    echo "$result"
}

# Run `go test` over the frozen package list. $1 is the patch list used to
# synthesise "--- FAIL:" lines for tests that cannot even compile; it is always
# the GOLD test patch, never the agent's fix patch, so an agent cannot inject or
# suppress synthetic verdicts. Packages past the time budget are listed by name
# instead of being silently dropped.
run_gold_pkgs() {
    local synth_patches="$1"
    shift
    local pkgs="$*"
    local BUDGET=450
    local skipped="" pkg pkg_out pkg_path tn
    echo "Testing packages:$pkgs"
    SECONDS=0
    for pkg in $pkgs; do
        if [ "$SECONDS" -gt "$BUDGET" ]; then
            skipped="$skipped $pkg"
            continue
        fi
        echo "==> $pkg (elapsed=${SECONDS}s)"
        pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
        echo "$pkg_out"
        if [ -n "$synth_patches" ] && echo "$pkg_out" | grep -q '\\[build failed\\]'; then
            pkg_path="${pkg#./}"
            for tn in $(_extract_test_names_in_pkg "$pkg_path" $synth_patches); do
                echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
            done
        fi
    done
    if [ -n "$skipped" ]; then
        echo "BUDGET-EXCEEDED: ${BUDGET}s budget hit; packages NOT RUN:$skipped"
    fi
    echo "Total elapsed: ${SECONDS}s"
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Repo is already cloned and checked out at ${{BASE_COMMIT}} by the Dockerfile;
# no git checkout here (it would fight the hardening pass that follows).
git reset --hard
bash /home/check_git_changes.sh

# Freeze the scoring inputs while BOTH patches are still gold. At evaluation
# time /home/fix.patch is replaced by the agent's patch (bind-mounted), so any
# list derived from it at that point would be agent-controlled: it could add
# packages that push the real test package past the time budget, or reorder the
# run. Deriving them once here, at build time, makes the tested package set and
# the protected gold-test file set identical in every stage.
source /home/extract_packages.sh
_gold_pkg_dirs /home/test.patch /home/fix.patch > /home/gold_pkgs.txt
_patch_files /home/test.patch > /home/gold_test_files.txt
chmod 0444 /home/gold_pkgs.txt /home/gold_test_files.txt
echo "frozen gold packages: $(wc -l < /home/gold_pkgs.txt)"
echo "frozen gold test files: $(wc -l < /home/gold_test_files.txt)"

export PATH="$PATH:/go/bin"
# Select the protobuf codegen version this era's checkout expects
# (both are pre-installed in the shared base).
ln -sf /go/bin/protoc-gen-go-1.31.0 /go/bin/protoc-gen-go
ln -sf /go/bin/protoc-gen-go-grpc-1.3.0 /go/bin/protoc-gen-go-grpc
timeout 300 make prepare-sources 2>&1 | tail -20 || true
timeout 900 make get-sources 2>&1 | tail -20 || true
timeout 300 make protogen-go 2>&1 | tail -30 || true
timeout 600 make assets 2>&1 | tail -10 || true

# Pre-resolve every Go module this PR's go.mod declares, at ${{BASE_COMMIT}}.
# Runs at BUILD time so the test stages never need the network and a
# "missing go.sum entry for module ..." can no longer fail a package build.
# `download all` walks the full module graph (test deps included) and writes the
# missing go.sum hashes; both calls are best-effort so a flaky mirror or an old
# go.mod that cannot resolve never aborts the image.
export GOFLAGS=-mod=mod
timeout 900 go mod download all 2>&1 | tail -5 || true
timeout 300 go mod download     2>&1 | tail -5 || true
go mod verify 2>&1 | tail -2 || true

go test -v -count=1 -timeout 3m ./pkg/utils/... 2>&1 | tail -5 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

# Baseline stage: no patches applied. run_result is the temporal proof of what
# existed and passed before the gold test patch, so nothing may be applied here.
cd /home/{pr.repo}
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh
source /home/gold_guard.sh

restore_gold_test_files
GO_TEST_PKGS=$(gold_test_pkgs)
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from the gold patches; exiting cleanly."
    exit 0
fi
# No synthetic verdicts at baseline: a test the gold patch has not introduced yet
# must read as NONE, not FAIL, or the baseline-first classifier misreads it.
run_gold_pkgs "" $GO_TEST_PKGS
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

# Gold test patch only. The credited tests must FAIL here and PASS in fix-run.
cd /home/{pr.repo}
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh
source /home/gold_guard.sh

# prepare.sh ran `go mod download all`, which rewrites the tracked go.sum inside
# the image. A fix patch that also edits go.mod/go.sum then cannot apply against
# the rewritten file (git apply and its --3way fallback both refuse), so restore
# both to ${{BASE_COMMIT}} before patching. The module cache stays warm, so the
# `go mod tidy` below refills any missing hashes offline.
git checkout "${{BASE_COMMIT}}" -- go.mod go.sum 2>/dev/null || true

restore_gold_test_files
if ! apply_patch_strict /home/test.patch "gold test patch"; then
    # Fail closed: running the unpatched tree here would record baseline passes
    # as the post-test-patch result and fabricate a resolved instance.
    echo "GOLD-TEST-PATCH-UNAPPLIED: no results captured for this stage."
    exit 0
fi
regen_proto_if_patched
go mod tidy 2>/dev/null || true

GO_TEST_PKGS=$(gold_test_pkgs)
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from the gold patches; exiting cleanly."
    exit 0
fi
run_gold_pkgs "/home/test.patch" $GO_TEST_PKGS
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

# Fix stage. /home/fix.patch is the AGENT's patch at evaluation time (gold only
# during dataset generation), so the order below matters: apply the fix first,
# then restore every gold test file to ${{BASE_COMMIT}}, then apply the gold test
# patch. Any edit the fix made to a scoring test is discarded before the tests
# that decide the score are laid down.
cd /home/{pr.repo}
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh
source /home/gold_guard.sh

# prepare.sh ran `go mod download all`, which rewrites the tracked go.sum inside
# the image. A fix patch that also edits go.mod/go.sum then cannot apply against
# the rewritten file (git apply and its --3way fallback both refuse), so restore
# both to ${{BASE_COMMIT}} before patching. The module cache stays warm, so the
# `go mod tidy` below refills any missing hashes offline.
git checkout "${{BASE_COMMIT}}" -- go.mod go.sum 2>/dev/null || true

if ! apply_patch_strict /home/fix.patch "fix patch"; then
    echo "FIX-PATCH-UNAPPLIED: continuing so the stage still records verdicts."
fi

restore_gold_test_files
if ! apply_patch_strict /home/test.patch "gold test patch"; then
    echo "GOLD-TEST-PATCH-UNAPPLIED: no results captured for this stage."
    exit 0
fi
regen_proto_if_patched
go mod tidy 2>/dev/null || true

GO_TEST_PKGS=$(gold_test_pkgs)
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from the gold patches; exiting cleanly."
    exit 0
fi
run_gold_pkgs "/home/test.patch" $GO_TEST_PKGS
restore_gold_test_files
exit 0
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-level per-PR image FROM the shared era base. dependency() is an
        # *Image*, so DockerfileEnhancer returns this verbatim -- the clone,
        # checkout and Image._HARDENING_BLOCK below are kept exactly as written.
        # BASE_COMMIT is defaulted to this PR's sha because build_dataset only
        # passes REPO_URL/BASE_COMMIT build args for string-dependency images,
        # and is re-exported as an ENV so it survives into the run stages, where
        # gold_guard.sh restores the gold test files from it.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        org, repo = self.pr.org, self.pr.repo

        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"

        header = f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [re.compile(r"--- FAIL: (\S+)")]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    def base_name(name: str) -> str:
        i = name.rfind("/")
        return name if i == -1 else name[:i]

    for raw in test_log.splitlines():
        line = raw.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(base_name(name))

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(base_name(name))

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(base_name(name))

    passed_tests -= failed_tests
    skipped_tests -= passed_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("mudler", "LocalAI_4999_to_1000")
class LocalAI_4999_to_1000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)
