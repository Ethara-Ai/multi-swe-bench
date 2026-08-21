import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MODULE_PATH = "mvdan.cc/sh/v3"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Names are kept package-qualified
    (`pkg/path::TestName`); subtests appear as `TestName/sub`.

    mvdan/sh's table-driven suites call `t.Run("", ...)`, so the runtime names
    those subtests `#0000`, `#0001`, ... in table order. Inserting a case in the
    middle of a table therefore renumbers every later subtest. That is harmless
    for classification: the test-patch and fix-patch stages share the same
    numbering, so F2P/P2P are compared like for like, and only the pre-patch
    baseline stage is offset."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if pkg == _MODULE_PATH:
            pkg = ""
        elif pkg.startswith(_MODULE_PATH + "/"):
            pkg = pkg[len(_MODULE_PATH) + 1:]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


class ShImageBase(Image):
    """Per-PR base for mvdan/sh, go.mod `go 1.21` era (PR #1066, 2024-03).

    This base is pinned to the PR's own BASE_COMMIT and carries the COMPLETE
    history scrub - gc, repack and all four integrity asserts. Nothing is left
    for the PR layer to finish, which is why `pr-<N>` has no scrub block at all.

    That is only possible because the tag is `base-pr-<N>`. A shared era tag
    cannot do this: the prune needs a pinned HEAD, and pinning a shared image
    would fix it to whichever PR built it first, so every other PR in the era
    would die on `fatal: unable to read tree`. One PR, one base, one prune.

    `dependency()` returns a str, so build_dataset.py:625-629 passes REPO_URL
    and BASE_COMMIT as build args - that is what makes `${BASE_COMMIT}` below
    resolve. An Image-dependency image gets no build args at all.

    golang:1.21 is bookworm, whose bash is 5.2. That matters: `checkBash()` in
    interp_test.go only sets `hasBash50` when `$BASH_VERSION` starts with "5.1",
    so on bookworm `TestRunnerRunConfirm` skips instead of shelling out to real
    bash ~1100 times. A bullseye base would silently turn a 35s suite into a
    very slow one. Keep this base on the default (bookworm) tag."""

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
        return "golang:1.21"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # The COMPLETE scrub - gc, repack and all four integrity asserts - lives
        # here and only here. `Image._HARDENING_BLOCK` is used verbatim rather
        # than a hand-rolled variant so the asserts can never quietly diverge
        # from the harness's own definition; it already carries the submodule
        # pass as its second RUN.
        #
        # It opens with `git checkout --detach "${BASE_COMMIT}"`, so it can only
        # run somewhere BASE_COMMIT is a real value. That is true here because
        # dependency() is a str (see the class docstring), and it is why the
        # prune belongs in this file instead of the pr-<N> layer.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere and cannot drift.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file:
        #
        #     if cls.SYNTAX_DIRECTIVE in raw: return raw     (image.py:316-317)
        #
        # and the directive has to stay. Dropping it to re-enable the enhancer
        # would let _inject_final_sanitize() append the FULL hardening block --
        # `git checkout --detach "${BASE_COMMIT}"` against an empty BASE_COMMIT
        # plus a `gc --prune=now` -- which would either fail the build or prune
        # the shared base down to one commit and break every other PR in the era.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            "ENV CGO_ENABLED=0 \\\n    GOTOOLCHAIN=local",
            # Quoted deliberately. Docker's `ENV key=value` form takes MULTIPLE
            # space-separated pairs per line, so unquoted this renders as two
            # variables - GOFLAGS=-buildvcs=false plus a junk var named `-mod` -
            # and `-mod=mod` never reaches GOFLAGS. Verified in the built image.
            'ENV GOFLAGS="-buildvcs=false -mod=mod"',
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class ShImageDefault(Image):
    """Per-PR image: stage the patches and run-scripts, warm the module cache
    for both the pre-fix and post-fix `go.mod`, and run the Go packages the test
    patch touches.

    Carries no history scrub - `base-pr-<N>` already ran the complete one."""

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
        return ShImageBase(self.pr, self._config)

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
cd /home/{pr.repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
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
timeout 600 go mod download || true
# A fix patch may ADD a module requirement (PR #1066 adds
# github.com/muesli/cancelreader), which the line above cannot see. Warm the
# cache for the post-fix module graph too, at build time while the network is
# still available, then put go.mod/go.sum back so the tree stays clean for the
# hardening asserts.
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/fix.patch 2>/dev/null; then
    git apply --whitespace=nowarn --include=go.mod --include=go.sum /home/fix.patch 2>/dev/null || true
    timeout 600 go mod download || true
    git checkout -- go.mod go.sum
fi
# This block edits two tracked files and puts them back. Verify the revert
# actually happened -- `set -e` turns a failure here into a failed build rather
# than an image that silently ships a modified go.mod as its baseline.
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Go package dirs the PR's test patch touches. cmd/ and the top-level
# testdata-driven scripts need a built binary and a real shell sandbox, so keep
# to the unit-test packages.
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | grep -vE '(^|/)(integration|e2e)/' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | grep -vE '(^|/)(integration|e2e)/' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn /home/fix.patch 2>/dev/null || git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch | sed -E 's#^diff --git a/(.+) b/.*#\\1#' | grep -vE '(^|/)(integration|e2e)/' | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- {tag} is pinned to this PR's base commit and has already run
        # the full scrub (gc, repack, all four asserts), so there is nothing left
        # to prune here. Repeating it would only re-run an expensive no-op.
        #
        # prepare.sh does the reset/checkout and asserts a clean tree, so this
        # file does not repeat those either.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


@Instance.register("mvdan", "sh")
class SH(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ShImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_go_test_log(log)
