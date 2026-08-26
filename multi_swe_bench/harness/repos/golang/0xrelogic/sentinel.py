import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# go.mod at base commit 9c0a2fdd declares `go 1.21`, and .github/workflows/go.yml
# pins actions/setup-go to `go-version: 1.21`. golang:1.21 is the Debian-bookworm
# variant, which already ships git/ca-certificates/curl, so the base image needs
# no apt layer at all.
BASE_IMAGE = "golang:1.21"

# `go test -json` reports a package by its full module import path; Go carries no
# notion of which *file* a test lives in, so a source path is not recoverable from
# the test output at all. Stripping the module prefix turns
# "github.com/0xReLogic/SENTINEL/checker::TestX" into the repo-relative
# "checker::TestX", which is the convention the other Go configs here follow
# (rook, talos, trivy, mvdan/sh). Uniqueness is unaffected -- the package segment
# is still what separates cmd::TestLoadConfig from config::TestLoadConfig.
_MODULE_PATH = "github.com/0xReLogic/SENTINEL"

# Exported by every stage script AND by prepare.sh, so the toolchain behaves
# identically at image-build time and at evaluation time.
#   CGO_ENABLED=0  -- the module has no cgo dependency (only cobra + yaml.v3);
#                     disabling it keeps the pure-Go resolver, which is what
#                     makes checker's "invalid-url-that-does-not-exist.example"
#                     case fail fast in a network-less sandbox.
#   GOTOOLCHAIN=local -- go.mod says `go 1.21` and the image is 1.21; this
#                     forbids an on-the-fly toolchain download at eval time.
#   GOFLAGS=-mod=mod -- neither the test patch nor the fix patch touches
#                     go.mod/go.sum, but this keeps a stale-checksum hiccup from
#                     aborting the whole stage before a single test runs.
_ENV_EXPORTS = "\n".join(
    [
        "export CI=true",
        "export CGO_ENABLED=0",
        "export GOFLAGS=-mod=mod",
        "export GOTOOLCHAIN=local",
    ]
)

# `-json` (not `-v`) because test2json tags every event with its Package, which
# is what makes the parsed names unique: Go permits the same TestX in several
# packages, and this repo genuinely has one -- TestLoadConfig exists in BOTH
# cmd/cmd_test.go and config/config_test.go. A `-v` parser keying on the bare
# function name would collapse the two into a single entry and silently corrupt
# the f2p comparison.
#
# `-count=1` disables the test result cache so all three stages really execute.
# `2>&1` folds the go command's own stderr (compiler diagnostics, the
# "FAIL <pkg> [build failed]" line) into the captured stream -- the test stage
# applies test.patch alone, which does not compile against the base sources, and
# that failure has to reach parse_log rather than vanish.
GO_TEST_CMD = "go test -json -count=1 -timeout 10m ./... 2>&1"


class ImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # This Dockerfile is written in the already-enhanced shape: it carries the
        # BuildKit syntax directive, the TARGETARCH/REPO_URL/BASE_COMMIT ARGs, the
        # proxy ARG/ENV pair, the OCI labels, the cert symlinks and the repo
        # hardening block. DockerfileEnhancer.enhance() sees the syntax directive
        # and returns the content verbatim (image.py:317-318), so what is written
        # to the build context is exactly what is below -- no injection, no
        # rewriting of the clone command, nothing to reconcile after the fact.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}


{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}
CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{env}

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Everything below runs during `docker build`, while the network is still
# reachable, so no evaluation stage ever has to fetch a module.
go mod download all || true

# Warm the cache against the PATCHED tree as well. The two patches do not touch
# go.mod/go.sum today, so this is belt-and-braces rather than load-bearing --
# but it costs one cached build and removes any dependency on the eval sandbox
# having network if that ever changes. `|| true` on the apply: a warm-up must
# never be able to fail the image build.
if git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download all || true
    go build ./... || true
fi

# Back to the pristine base tree. `git checkout -- .` alone is not enough: the
# test patch adds new test functions to existing files and `go mod download`
# can append checksums to go.sum, either of which would leave the tree dirty and
# make the evaluation-time `git apply` fail on an already-applied hunk.
git checkout -- . || true
git reset --hard
git clean -fd
bash /home/check_git_changes.sh

# Compile the packages and the test binaries once so the build cache is hot.
# `-run XXX_NO_SUCH_TEST` links every *_test.go without executing a single test,
# which keeps prepare.sh from recording results that the run stage must produce.
go build ./... || true
go test -count=1 -run XXX_NO_SUCH_TEST ./... || true

git reset --hard
git clean -fd
bash /home/check_git_changes.sh
""".format(env=_ENV_EXPORTS, pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}

{gotest}
""".format(env=_ENV_EXPORTS, pr=self.pr, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{gotest}
""".format(env=_ENV_EXPORTS, pr=self.pr, gotest=GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{gotest}
""".format(env=_ENV_EXPORTS, pr=self.pr, gotest=GO_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() returns an Image, so DockerfileEnhancer.enhance() returns
        # this content unchanged (image.py:315-316). The base layer already did
        # the clone, the checkout and the git hardening; this layer only ships
        # the patches and the stage scripts.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{self.clear_env}
RUN bash /home/prepare.sh
"""


@Instance.register("0xReLogic", "SENTINEL")
class SENTINEL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _short_pkg(pkg: str) -> str:
        # "github.com/0xReLogic/SENTINEL/checker" -> "checker".
        # The root package (main.go) collapses to "", handled by _qualify below.
        if pkg == _MODULE_PATH:
            return ""
        if pkg.startswith(_MODULE_PATH + "/"):
            return pkg[len(_MODULE_PATH) + 1 :]
        return pkg

    @classmethod
    def _qualify(cls, pkg: str, test: str) -> str:
        short = cls._short_pkg(pkg)
        return f"{short}::{test}" if short else test

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # `go test -json` emits one JSON object per line. Test-level events carry
        # both Package and Test, so "<import path>::<TestName>" is unique across
        # packages -- required here, because TestLoadConfig is defined in both
        # cmd/cmd_test.go and config/config_test.go. Subtests keep their full
        # "TestX/sub" path, so t.Run cases stay distinct too. Package-level
        # events have no Test field and are tracked separately below.
        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        # A package that fails to COMPILE produces no test event at all -- go
        # writes the compiler diagnostics as plain text and closes the package
        # with "FAIL <pkg> [build failed]". This is not hypothetical for this
        # instance: the test stage applies test.patch alone, which calls
        # CheckService with a third argument and references config.DefaultInterval,
        # neither of which exists at the base commit, so all three packages fail
        # to build. Without this the whole stage would parse as 0/0/0 and the
        # report would be rejected for having no results instead of showing the
        # expected NONE -> PASS transitions.
        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        for raw in log.split("\n"):
            line = raw.strip()
            if not line.startswith("{"):
                m = build_failed_re.match(line)
                if m:
                    failed_tests.add(self._qualify(m.group(1), "[build]"))
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue

            action = ev.get("Action")
            pkg = ev.get("Package")
            test = ev.get("Test")

            # Depending on the Go release the "[build failed]" line arrives either
            # as plain stderr text (handled above) or wrapped in an output event.
            # Catch both; the name is identical, and the collection is a set.
            if action == "output" and pkg and not test:
                m = build_failed_re.match(str(ev.get("Output", "")).strip())
                if m:
                    failed_tests.add(self._qualify(m.group(1), "[build]"))
                continue

            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = self._qualify(pkg, test)
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A package that failed without producing any test event did not build
        # (or died in TestMain). Surface it as a failure so the stage is not
        # silently credited with zero tests.
        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(self._qualify(pkg, "[build]"))

        # TestResult.__post_init__ rejects overlapping sets, and a flaky-retry or
        # a subtest that both skips and fails can legitimately land in two of
        # them. Failure wins, then pass, so the counts stay consistent.
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
