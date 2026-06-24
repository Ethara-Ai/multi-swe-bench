import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# bufbuild/buf — the Protobuf CLI / linter / breaking-change detector (Go).
#
# Discovery (dataset analysis):
#  - 134-PR range #1..#4492. Single Go module. Test files moved across eras
#    (internal/ -> private/ -> cmd/) but it stays one `go test` build; a recent
#    Go toolchain with GOTOOLCHAIN=auto covers the whole go.mod version span.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with a `### BUFPKG ###`
#    marker so test ids stay unique across packages. buf's unit tests are
#    self-contained Go tests — no cloud credentials needed.
#
# Build structure (shared base + single-FROM hardened PR layer):
#  - BufImageBase -> tag ":base". Built once and reused by every PR. Installs
#    the common apt packages and does the single `git clone` of the whole repo
#    (full history, so any PR's BASE_COMMIT is reachable). Deliberately NOT
#    hardened — it must retain every commit to serve all PRs. Its Dockerfile
#    carries the BuildKit `# syntax=` directive so DockerfileEnhancer leaves it
#    verbatim (else the enhancer would inject a BASE_COMMIT-dependent hardening
#    pass into the base, which has no BASE_COMMIT and would fail to build).
#  - BufImageDefault -> tag ":pr-<n>". Single-FROM layered build off the shared
#    ":base": pin/checkout BASE_COMMIT -> COPY per-PR patches/scripts -> prep ->
#    Image._HARDENING_BLOCK (prunes every other ref/remote/commit) -> CMD.
#
#  Reward-hacking note: hardening sanitizes the *runtime* filesystem, but with a
#  single FROM the :base layer's full-history packfile sits below the hardening
#  layer, so a layered `docker save` can be raw `tar -x`'d to recover the fix.
#  Distribute FLATTENED images (`docker export`, or build with `--squash`) and
#  never ship the standalone ":base" image to an evaluated model.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class BufImageBase(Image):
    """Shared base image (tag ':base'): apt deps + a single full-history clone.

    Built once and reused by every PR. Deliberately un-hardened (it must keep
    all commits so any PR's BASE_COMMIT is reachable). The leading `# syntax=`
    directive makes DockerfileEnhancer return this Dockerfile verbatim, so it
    does not get a BASE_COMMIT-dependent hardening pass injected (which would
    fail to build here — BASE_COMMIT only exists in the PR layer).
    """

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
        return "golang:1-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM golang:1-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV GOTOOLCHAIN=auto
ENV GOFLAGS=-mod=mod

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

CMD ["/bin/bash"]
"""


class BufImageDefault(Image):
    """Per-PR image (tag ':pr-<n>'): single-FROM layered build off the shared
    :base. Pins/checks out BASE_COMMIT, copies patches+scripts, preps, then runs
    Image._HARDENING_BLOCK in this layer.

    NOTE: single-FROM means the :base layer's full-history clone sits below the
    hardening layer, so a layered `docker save` can still be `tar -x`'d to
    recover the fix. Distribute FLATTENED images (`docker export` / `--squash`).
    """

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
        # Chained Image -> the per-PR layer builds FROM the shared ":base".
        # (DockerfileEnhancer returns dockerfile() verbatim for a chained
        # dependency, so the layout below is exactly what ships.)
        return BufImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_files = " ".join(f.name for f in self.files())

        # Industry-standard single-FROM layered pattern. Image._HARDENING_BLOCK
        # is concatenated (not f-stringed) so its ${BASE_COMMIT}/%(refname)
        # braces stay literal and byte-identical to image.py.
        head = f"""FROM {base_name}

# 1. Build-time args first (overridable via --build-arg)
ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

# 2. WORKDIR before any RUN/COPY that depends on it
WORKDIR /home/{repo}

# 3. Git checkout BEFORE copying patches (clean, known state)
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

# 4. COPY scripts/patches late so earlier expensive layers stay cached
COPY {copy_files} /home/

# 5. Install / prep
RUN bash /home/prepare.sh

# 6. Repo cleanup (hardening) — kept as-is, synced with image.py
"""
        return head + self._HARDENING_BLOCK + '\nCMD ["/bin/bash"]\n'

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        check_git = """#!/bin/bash
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
"""

        # The repo is already checked out at ${BASE_COMMIT} and hardened by the
        # shared Image.dockerfile(), so prepare.sh no longer performs any git
        # checkout itself — it only warms the module cache. The download is
        # allowed to fail (|| true) because its only purpose here is caching;
        # the real pass/fail signal comes from the run/test-run/fix-run scripts.
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
go mod download 2>/dev/null || true
# Drop any go.sum churn from the cache warm-up so the tree is clean before the
# hardening pass detaches onto ${BASE_COMMIT}.
git reset --hard 2>/dev/null || true
""".replace("__REPO__", repo)

        # Per-package `go test`. -vet=off avoids vet-only failures masking the
        # real test outcome.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### BUFPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("bufbuild", "buf")
class Buf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BufImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestLint (0.01s)
        #   --- FAIL: TestBreaking (0.02s)
        #   --- SKIP: TestRegistry (0.00s)
        # Fenced by `### BUFPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### BUFPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
