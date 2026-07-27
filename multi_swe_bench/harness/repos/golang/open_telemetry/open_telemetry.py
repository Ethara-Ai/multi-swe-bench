import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:latest"

# Archive-resilient apt, mirroring the XTLS/Xray-core registry: try the normal
# mirror first and fall back to archive.debian.org when it has been retired.
_APT_INSTALL = (
    "RUN { apt-get update 2>/dev/null || "
    "{ sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && "
    "sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && "
    "sed -i '/-updates/d' /etc/apt/sources.list && "
    "apt-get update; }; } && \\\n"
    "    apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    patch \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)


class OpenTelemetryGoImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR of this repo.

    ``dependency()`` returns a *string*, so DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository. A shared string-dependency image that clones
    gets force-pinned to a single ``${BASE_COMMIT}`` and history-stripped by the
    enhancer, which breaks ``git checkout`` for every other PR sharing the tag --
    that was exactly the defect in the previous two-stage layout here. The clone
    therefore lives in the Default image, per-PR. This layer provides only the Go
    toolchain, apt deps and Go env, so it is genuinely reusable: because no
    ``ARG BASE_COMMIT`` is declared before them, these layers keep a stable cache
    key across PRs instead of being rebuilt for each one.
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No repository fetch here on purpose -- see the class docstring. With
        # no fetch in this Dockerfile the enhancer's _standardize_repo_fetch and
        # _inject_final_sanitize passes are both no-ops, so nothing pins this
        # shared layer to one PR's commit.
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

{_APT_INSTALL}

ENV GOFLAGS=-buildvcs=false

CMD ["/bin/bash"]
"""


class OpenTelemetryGoImageDefault(Image):
    """Level 2: per-PR image, built on the shared toolchain base.

    ``dependency()`` returns an Image, so DockerfileEnhancer returns this
    Dockerfile verbatim -- it injects no clone and no hardening. Both therefore
    live here explicitly, per-PR: clone full history, check out
    ``${BASE_COMMIT}`` inline, stage the scripts, warm the caches, then run the
    verbatim ``Image._HARDENING_BLOCK`` to strip origin/refs/future history.
    Pinning is correct at this level because the tag is ``pr-<N>``.
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
        return OpenTelemetryGoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_files = " ".join(file.name for file in self.files())

        # BASE_COMMIT is defaulted to this PR's base.sha so the build works with
        # or without an explicit --build-arg. Declaring it here (level 2) rather
        # than in the shared base is what keeps the base's apt layer cacheable.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then the submodule
        # pass). Concatenated raw rather than through an f-string so its
        # ${BASE_COMMIT} and %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
                "restore_test_files.sh",
                """#!/bin/bash
# Anti-reward-hacking guard. Restores every file touched by the gold test
# patch to its pristine ${{BASE_COMMIT}} state before the patch is applied,
# so a candidate solution that weakened, deleted or pre-applied the tests
# cannot influence the graded result. HEAD is ${{BASE_COMMIT}} (and, after the
# hardening block, the only commit in the repo), so "git checkout HEAD --"
# is exactly a restore-to-base. Files that do not exist at base are ones the
# patch adds; those are removed so the patch applies cleanly.
set -eo pipefail

cd /home/{pr.repo}

TEST_FILES=$(grep '^diff --git' /home/test.patch 2>/dev/null \\
  | sed 's|diff --git a/||;s| b/.*||' | sort -u)

for f in $TEST_FILES; do
  if git cat-file -e "HEAD:$f" 2>/dev/null; then
    git checkout HEAD -- "$f"
  else
    rm -f "$f"
  fi
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "find_module_root.sh",
                """#!/bin/bash
# find_module_root.sh <pkg_dir>
# Walks up from pkg_dir to find the nearest go.mod, prints that directory.
# Falls back to repo root if no go.mod found.
DIR="$1"
REPO_ROOT="/home/{pr.repo}"
while [ "$DIR" != "." ] && [ "$DIR" != "/" ]; do
  if [ -f "$REPO_ROOT/$DIR/go.mod" ]; then
    echo "$DIR"
    exit 0
  fi
  DIR=$(dirname "$DIR")
done
echo "."
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_multimod_tests.sh",
                """#!/bin/bash
# run_multimod_tests.sh [patches...]
# Extracts affected Go packages from patches, groups by Go module root,
# and runs go test in each module directory.
set -e

REPO_ROOT="/home/{pr.repo}"
cd "$REPO_ROOT"

# Extract package dirs from patches
PKGS=$(cat "$@" 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '\\.go$' | xargs -I{{}} dirname {{}} | sort -u)

if [ -z "$PKGS" ]; then
  echo "No Go packages found in patches, testing root module..."
  go test -v -count=1 -timeout 15m ./...
  exit $?
fi

# Group packages by their Go module root
declare -A MODULE_PKGS
for pkg in $PKGS; do
  mod_root=$(bash /home/find_module_root.sh "$pkg")
  if [ "$mod_root" = "." ]; then
    rel_pkg="./$pkg"
  else
    # Make the package path relative to its module root
    rel_pkg="./${{pkg#$mod_root/}}"
    # If the package IS the module root, test ./...
    if [ "$rel_pkg" = "./" ] || [ "$rel_pkg" = "./$pkg" ] && [ "$pkg" = "$mod_root" ]; then
      rel_pkg="./..."
    fi
  fi
  if [ -z "${{MODULE_PKGS[$mod_root]+x}}" ]; then
    MODULE_PKGS[$mod_root]="$rel_pkg"
  else
    MODULE_PKGS[$mod_root]="${{MODULE_PKGS[$mod_root]}} $rel_pkg"
  fi
done

# Run tests per module
OVERALL_EXIT=0
for mod_root in "${{!MODULE_PKGS[@]}}"; do
  pkgs="${{MODULE_PKGS[$mod_root]}}"
  mod_dir="$REPO_ROOT/$mod_root"
  if [ "$mod_root" = "." ]; then
    mod_dir="$REPO_ROOT"
  fi
  echo "=== Testing module at $mod_root: $pkgs ==="
  cd "$mod_dir"
  go test -v -count=1 -timeout 15m $pkgs || OVERALL_EXIT=$?
  cd "$REPO_ROOT"
done

exit $OVERALL_EXIT
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Warms the Go module + build caches at image-build time so the eval runs do
# not need network. The per-PR Dockerfile has already cloned the repo and
# checked out ${{BASE_COMMIT}}, so this script performs no git checkout of its
# own. It runs *before* the hardening block, and restores a pristine worktree
# on exit so hardening sees the tree exactly as ${{BASE_COMMIT}} left it.
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# Warm up: run tests in all affected modules (ignore failures at this stage)
bash /home/run_multimod_tests.sh /home/test.patch /home/fix.patch || true

# Leave the worktree clean for the hardening pass.
git reset --hard
git clean -fdx -e vendor || true
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/run_multimod_tests.sh /home/test.patch /home/fix.patch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/restore_test_files.sh
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_multimod_tests.sh /home/test.patch /home/fix.patch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/restore_test_files.sh
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_multimod_tests.sh /home/test.patch /home/fix.patch

""".format(pr=self.pr),
            ),
        ]


@Instance.register("open-telemetry", "opentelemetry-go")
class OpenTelemetryGo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenTelemetryGoImageDefault(self.pr, self._config)

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
            return test_name

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
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
