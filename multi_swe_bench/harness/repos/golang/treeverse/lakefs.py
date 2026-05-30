from __future__ import annotations

import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".pdf", ".zip", ".jar", ".class", ".tar", ".gz", ".tgz", ".bz2", ".7z",
    ".parquet", ".avro", ".orc",
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".ogg",
}


def _is_binary_path(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    if ext in _BINARY_EXTENSIONS:
        return True
    # lakeFS test fixtures with no extension but binary content
    if "nessie/files/ro_" in path or "/test_files/" in path:
        return True
    return False


def _strip_binary_hunks(patch_text: str) -> str:
    """Remove binary-file hunks from a unified diff so `git apply` won't reject
    the whole patch when only PNG/JPG/JAR-style hunks are problematic.
    Binary content is irrelevant to Go test outcomes in this repo."""
    if not patch_text:
        return patch_text
    out = []
    blocks = re.split(r"(?m)^(?=diff --git )", patch_text)
    for block in blocks:
        if not block.strip():
            continue
        first_line = block.splitlines()[0]
        m = re.match(r"diff --git a/(.*) b/(.*)$", first_line)
        if m and (_is_binary_path(m.group(1)) or _is_binary_path(m.group(2))):
            continue
        if "GIT binary patch" in block or re.search(
            r"^Binary files .* differ", block, re.MULTILINE
        ):
            continue
        out.append(block)
    return "".join(out)


class LakeFSImageBase(Image):
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
        return "golang:1.25"

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

        body = f"""FROM {image_name}

{self.global_env}

ENV GOTOOLCHAIN=auto
ENV PATH=/go/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

# Code generators used across lakeFS eras (all Go-based, no Docker / Java needed):
#   - oapi-codegen: generates pkg/api/apigen and *.gen.go from api/swagger.yml
#   - statik:       embeds SQL migrations (pkg/ddl) and UI assets (pkg/webui) for the older era
#   - mockgen:      generates pkg/*/mock packages via //go:generate
#   - goimports:    used by some wrapgen //go:generate directives
RUN go install github.com/deepmap/oapi-codegen/cmd/oapi-codegen@v1.5.6 \\
 && go install github.com/golang/mock/mockgen@v1.6.0 \\
 && go install github.com/rakyll/statik@latest \\
 && go install golang.org/x/tools/cmd/goimports@latest

WORKDIR /home/

{code}

{self.clear_env}
"""
        # Collapse 2+ consecutive blank lines into 1 (the f-string template +
        # DockerfileEnhancer cert-symlink injection can leave 3-4 blank lines
        # between sections, which trips the validate_dockerfiles.py lint).
        return re.sub(r"\n{3,}", "\n\n", body)


class LakeFSImageDefault(Image):
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
        return LakeFSImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_hunks(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_hunks(self.pr.test_patch),
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
                "gen.sh",
                """#!/bin/bash
# Shared code-generation script. Invoked from prepare.sh, test-run.sh, and fix-run.sh
# AFTER the source tree is at its target state (base, base+test, or base+test+fix).
# Generated outputs (pkg/*/mock/*.go, lakefs.gen.go, statik.go, ...) must match the
# source they are generated from, so this MUST run after any patch is applied.
set +e
export PATH=/go/bin:$PATH

if [ -d tools/wrapgen ]; then
  go install ./tools/wrapgen 2>/dev/null
fi

# Earlier era (<= v0.x): statik embeds SQL migrations into pkg/ddl.
if [ -d pkg/ddl ] && ls pkg/ddl/*.sql >/dev/null 2>&1; then
  statik -ns ddl -m -f -p ddl \\
    -c "auto-generated SQL files for data migrations" \\
    -dest pkg -src pkg/ddl -include '*.sql' 2>/dev/null
fi

# Earlier era: oapi-codegen for pkg/api via //go:generate in pkg/api/serve.go.
if [ -f pkg/api/serve.go ] && grep -q "^//go:generate" pkg/api/serve.go; then
  go generate ./pkg/api 2>/dev/null
fi

# Later era: apigen package with its own //go:generate directive.
if [ -f pkg/api/apigen/doc.go ] && grep -q "^//go:generate" pkg/api/apigen/doc.go; then
  go generate ./pkg/api/apigen 2>/dev/null
fi
for p in pkg/auth pkg/authentication; do
  if [ -f "$p/service.go" ] && grep -q "^//go:generate.*oapi-codegen" "$p/service.go"; then
    go generate ./$p 2>/dev/null
  fi
done

# Stub pkg/webui for the older era so pkg/api/ui_handler.go compiles without npm.
if grep -rq '"github.com/treeverse/lakefs/pkg/webui"' pkg/ cmd/ 2>/dev/null; then
  if [ ! -f pkg/webui/statik.go ] && [ ! -f pkg/webui/embedded.go ]; then
    mkdir -p pkg/webui/dist
    echo '<!doctype html><html></html>' > pkg/webui/dist/index.html
    statik -src=pkg/webui/dist -dest=pkg -p=webui -ns=webui -f 2>/dev/null
  fi
fi

# mockgen directives across many packages.
for tgt in pkg/graveler pkg/graveler/committed pkg/graveler/sstable pkg/graveler/staging \\
           pkg/pyramid pkg/actions pkg/kv pkg/onboard; do
  [ -d "$tgt" ] && go generate ./$tgt 2>/dev/null
done

exit 0
""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export PATH=/go/bin:$PATH
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true

# Generate code so the codebase compiles and the test cache warms up.
bash /home/gen.sh

# Commit the generated files so the working tree is "clean" and the run/test/fix
# scripts can `git reset --hard {pr.base.sha}` back to a pristine state before
# applying patches. This is critical for PRs whose fix.patch ADDS mock files
# (PR 4900, etc.) — without the reset, `git apply` aborts on those.
git add -A
git -c user.email=harness@local -c user.name=harness commit -m "harness: generated files" --no-verify || true

# Pre-compile (no test execution) so amd64-via-QEMU doesn't redo it 3x at eval.
# `go test -c` compiles each package's test binary but does NOT run it — saves
# ~80% vs full `go test ./...` warm-up while still seeding the build cache.
go test -count=1 -run='^$' ./... > /dev/null 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

export PATH=/go/bin:$PATH
cd /home/{pr.repo}
# Reset to pristine base.sha (drops the generated-files commit from prepare.sh)
# so `git apply` of patches that add mock_*.go won't conflict with pre-gen files.
git reset --hard {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch
# Regenerate code that matches the post-test-patch source state.
bash /home/gen.sh
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

export PATH=/go/bin:$PATH
cd /home/{pr.repo}
git reset --hard {pr.base.sha}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# Regenerate code that matches the post-fix-patch source state.
bash /home/gen.sh
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


@Instance.register("treeverse", "lakeFS")
class LakeFS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LakeFSImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # Dedup: when a parent test's parametrized subtests have mixed
        # PASS/FAIL results, the parent name (base_name) can end up in both
        # passed and failed sets. Likewise for skipped. TestResult rejects
        # overlapping sets in __post_init__, so resolve conflicts here:
        #   any FAIL on a name wins over PASS/SKIP for that name.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
