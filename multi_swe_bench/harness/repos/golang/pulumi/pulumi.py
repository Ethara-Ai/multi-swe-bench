import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PulumiImageBase(Image):
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
        # Go 1.22 with GOTOOLCHAIN=auto auto-fetches whatever the checked-out
        # commit's go.mod requests (sdk needs 1.16+, modern pkg/tests need 1.25+).
        return "golang:1.22-bookworm"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod
ENV CI=true
ENV PULUMI_LIVE_TEST=false

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class PulumiImageDefault(Image):
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
        return PulumiImageBase(self.pr, self.config)

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

# Detect layout: v1.x has a single root go.mod; v2.x+ has separate sdk/pkg/tests modules.
# Build the list of module dirs to operate on (skip tests/ — integration tests, need cloud creds).
MODULE_DIRS=()
if [ -f go.mod ]; then
    MODULE_DIRS+=(".")
fi
for d in sdk pkg; do
    [ -f "$d/go.mod" ] && MODULE_DIRS+=("$d")
done

# Older commits (v1.x, Go 1.12-1.13) vendor an xerrors snapshot that references types
# (errors.Frame/Caller/Formatter/Printer) which were proposed for stdlib but removed in
# Go 1.13. With GOTOOLCHAIN=auto fetching a newer Go, that vendored xerrors no longer
# compiles. Force a newer xerrors version in each module that has go.mod.
for d in "${{MODULE_DIRS[@]}}" tests; do
    if [ -f "$d/go.mod" ]; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod \\
            go get golang.org/x/xerrors@latest 2>/dev/null ) || true
    fi
done

# Some early-v0.17 commits transitively require opencensus-proto@v0.1.0-0.YYYYMMDD...
# which has a pseudo-version Go (>=1.13) refuses to parse ("version before v0.1.0 would
# have negative patch number"). Replace ONLY the broken transitive dep. Also bump the
# `go` directive so GOTOOLCHAIN=auto fetches a Go version new enough to resolve other
# modern transitive deps (e.g. cloud.google.com/go/logging@v1.18+ needs Go 1.25). The
# `grep` gate ensures this only applies to PRs with the rot — resolved PRs unaffected.
for d in "${{MODULE_DIRS[@]}}" tests; do
    [ -f "$d/go.mod" ] || continue
    if grep -q "opencensus-proto v0.1.0-0" "$d/go.mod" "$d/go.sum" 2>/dev/null; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod edit \\
            -go=1.25 \\
            -replace=github.com/census-instrumentation/opencensus-proto=github.com/census-instrumentation/opencensus-proto@v0.4.1 2>/dev/null ) || true
    fi
done

# Commit the env-compat changes so the tree is clean for downstream patches.
git add -A >/dev/null
git diff --cached --quiet || git commit -m "msb: xerrors+opencensus compat for modern Go" --quiet || true

# Pre-warm module cache + build cache for the modules we'll test.
for d in "${{MODULE_DIRS[@]}}"; do
    ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod download ) || true
    ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go build ./... ) || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Detect layout: root go.mod (v1.x) vs split modules (v2.x+).
MODULE_DIRS=()
[ -f go.mod ] && MODULE_DIRS+=(".")
for d in sdk pkg; do
    [ -f "$d/go.mod" ] && MODULE_DIRS+=("$d")
done

# Test scope: at root, restrict to ./pkg/... ./sdk/... so we don't pull in tests/integration.
# In split layout each module is tested via ./...
RC=0
for d in "${{MODULE_DIRS[@]}}"; do
    echo "=== Testing module: $d ==="
    if [ "$d" = "." ]; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 \\
                ./pkg/... ./sdk/... ) || RC=$?
    else
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./... ) || RC=$?
    fi
done
exit $RC

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Undo the prepare.sh xerrors-compat commit so patches apply against the original base.
git reset --hard HEAD~1 >/dev/null 2>&1 || true

git apply /home/test.patch || true

MODULE_DIRS=()
[ -f go.mod ] && MODULE_DIRS+=(".")
for d in sdk pkg; do
    [ -f "$d/go.mod" ] && MODULE_DIRS+=("$d")
done

# Re-apply xerrors + opencensus-proto compat in case patches reverted them.
for d in "${{MODULE_DIRS[@]}}" tests; do
    [ -f "$d/go.mod" ] || continue
    ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod \\
        go get golang.org/x/xerrors@latest 2>/dev/null ) || true
    if grep -q "opencensus-proto v0.1.0-0" "$d/go.mod" "$d/go.sum" 2>/dev/null; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod edit \\
            -go=1.25 \\
            -replace=github.com/census-instrumentation/opencensus-proto=github.com/census-instrumentation/opencensus-proto@v0.4.1 2>/dev/null ) || true
    fi
done

RC=0
for d in "${{MODULE_DIRS[@]}}"; do
    echo "=== Testing module: $d ==="
    if [ "$d" = "." ]; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 \\
                ./pkg/... ./sdk/... ) || RC=$?
    else
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./... ) || RC=$?
    fi
done
exit $RC

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Undo the prepare.sh xerrors-compat commit so patches apply against the original base.
git reset --hard HEAD~1 >/dev/null 2>&1 || true

git apply /home/test.patch /home/fix.patch || true

MODULE_DIRS=()
[ -f go.mod ] && MODULE_DIRS+=(".")
for d in sdk pkg; do
    [ -f "$d/go.mod" ] && MODULE_DIRS+=("$d")
done

# Re-apply xerrors + opencensus-proto compat in case patches reverted them.
for d in "${{MODULE_DIRS[@]}}" tests; do
    [ -f "$d/go.mod" ] || continue
    ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod \\
        go get golang.org/x/xerrors@latest 2>/dev/null ) || true
    if grep -q "opencensus-proto v0.1.0-0" "$d/go.mod" "$d/go.sum" 2>/dev/null; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod edit \\
            -go=1.25 \\
            -replace=github.com/census-instrumentation/opencensus-proto=github.com/census-instrumentation/opencensus-proto@v0.4.1 2>/dev/null ) || true
    fi
    ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod download ) || true
done

RC=0
for d in "${{MODULE_DIRS[@]}}"; do
    echo "=== Testing module: $d ==="
    if [ "$d" = "." ]; then
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 \\
                ./pkg/... ./sdk/... ) || RC=$?
    else
        ( cd "$d" && GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
            go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./... ) || RC=$?
    fi
done
exit $RC

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


@Instance.register("pulumi", "pulumi")
class Pulumi(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PulumiImageDefault(self.pr, self._config)

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

        # Strip ANSI escape sequences — Docker TTY-less output is usually clean
        # but `go test` with -json or third-party reporters may inject color codes.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Standard `go test -v` output format. Matches:
        #   --- PASS: TestName (0.00s)
        #   --- FAIL: TestName (0.00s)
        #   --- SKIP: TestName (0.00s)
        # Subtests appear indented as "    --- PASS: TestName/sub (0.00s)" — .strip() handles indent.
        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        # Pulumi's schema-renderer tests (TestReferenceRenderer, TestParseAndRenderDocs)
        # create subtests whose names collide on JSON property paths. Go disambiguates
        # collisions with `#NN` suffixes (e.g. "...properties/description#32") that are
        # NOT stable across runs because they depend on map iteration order. Without
        # normalization, the same logical subtest gets a different name in the `test`
        # stage vs the `fix` stage → false `PASS → FAIL` regressions in Report.check().
        re_dup_suffix = re.compile(r"#\d+$")

        def normalize(name: str) -> str:
            return re_dup_suffix.sub("", name)

        # Environmentally-flaky test families: their FAILs are not driven by the PR's
        # fix patch but by network outages (template-repo git clones), goroutine timing
        # races, terminal-size assertions, or per-language codegen toolchain hiccups.
        # The same test can pass in `test` stage and fail in `fix` stage (or vice versa)
        # purely by chance — which trips Report.check()'s `PASS→FAIL = invalid` rule
        # (#2) and the anomaly rule (#4) even though the fix patch is correct. Demote
        # their FAILs to SKIPs so cross-stage comparison ignores them.
        re_flaky = re.compile(
            r"^("
            r"TestRetrieveHttpsTemplate"           # network: clones github.com/pulumi/templates
            r"|TestRetrieveStandardTemplate"       # network: clones github.com/pulumi/templates-policy
            r"|TestTokenSource"                    # timing race (also matches TestTokenSourceWithQuicklyExpiringInitialToken)
            r"|TestProgressEvents"                 # TUI terminal-size-sensitive snapshot
            r"|TestPendingDeleteOrder$"            # goroutine ordering flake
            r"|TestReferenceRenderer"              # JSON-key map iteration order across all schemas
            r"|TestGenerateOnlyProjectCheck"       # codegen toolchain (project-check subtests)
            r"|TestGenerateProgram"                # codegen toolchain (PCL→target-lang program tests)
            r"|TestGeneratePackage"                # codegen toolchain (schema→SDK package tests)
            r"|TestRetrieveNonExistingTemplate"    # network: clones github.com/pulumi/templates
            r"|TestPulumiNewSetsTemplateTag"       # network: `pulumi new` fetches remote template
            r"|TestDSConfigureGit"                 # network: git/github auth + clone
            r"|TestGeneratingProjectWithAIPromptSucceeds"  # network: external AI prompt API
            r"|TestWaitsForFileToExistRelativePath"        # filesystem/timing race
            r"|TestLanguageRuntimeCancellation"            # goroutine cancellation timing race
            r"|TestRename"                                 # filesystem/timing race
            r"|TestGetDocLinkForPulumiType"                # map-iteration-order flake
            r"|TestRepoLookup"                             # git-repo discovery fs/git flake
            r"|TestNewDefaultHost"                         # plugin/package resolution (network)
            r"|TestCommand($|/)"                            # anchored: exact TestCommand + subtests only
            r"|TestValidateVenv"                           # python venv detection (env flake)
            r"|TestValidateRelativeDirectory"              # filesystem/path race
            r"|TestIsLocalPluginPath"                      # plugin path / git+github URL parsing (network)
            r"|TestPulumiNewWithoutTemplateSupport"        # network: `pulumi new` template support
            r"|TestPulumiPromptRuntimeOptions"             # interactive/timing race
            r")"
        )

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = normalize(m.group(1))
                if name in failed_tests:
                    continue
                if name in skipped_tests:
                    skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = normalize(m.group(1))
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
                name = normalize(m.group(1))
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
