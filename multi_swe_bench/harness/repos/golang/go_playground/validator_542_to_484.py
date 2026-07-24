import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.golang.go_playground.validator import (
    ValidatorImageBase,
)


class ValidatorV9ImageDefault(Image):
    """v9-era image (PRs 484 / 496 / 542): the commits that predate Go modules.

    There is deliberately no v9-specific base image class. The previous version
    of this file had one (``ValidatorV9ImageBase``, tag ``base``, a string
    dependency of ``golang:latest``) that cloned the repo in its own overridden
    ``dockerfile()``. That was unsafe for two independent reasons, both fixed
    here:

    1. ``DockerfileEnhancer._standardize_repo_fetch`` rewrites a hardcoded
       ``git clone ... /home/<repo>`` in any string-dependency Dockerfile into
       ``git clone "${REPO_URL}"`` + ``git checkout ${BASE_COMMIT}`` + the
       history-stripping hardening block. ``build_dataset`` passes BASE_COMMIT
       only for string dependencies -- so the ONE shared ``base`` tag got pinned
       to whichever v9 PR happened to build first, and its history gc-pruned.
       The other two v9 PRs' ``git checkout <their sha>`` then had nothing to
       check out.
    2. The per-PR image's ``dockerfile()`` was overridden and its dependency was
       an ``Image``, so the enhancer returned it verbatim and the canonical
       ``Image._HARDENING_BLOCK`` never ran on it at all -- the upstream fix
       commit and the ``origin`` remote stayed reachable inside the eval
       container.

    This class instead shares ``ValidatorImageBase`` with the v10 era (both need
    the identical Go toolchain) and writes the clone, the per-PR checkout and the
    hardening block out explicitly.
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

    def dependency(self) -> Image:
        return ValidatorImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Staged into /home/ after the checkout and before the hardening block.
        # These files live outside /home/<repo>, so the hardening pass (which
        # only operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY bootstrap-module.sh /home/bootstrap-module.sh\n"
            "COPY drop-bootstrap-if-patched.sh /home/drop-bootstrap-if-patched.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "bootstrap-module.sh",
                """#!/bin/bash
# v9 era: these commits predate Go modules -- there is no go.mod in the tree, and
# modern Go has no GOPATH fallback, so a module has to be synthesised before
# anything can build. Idempotent: a real go.mod supplied by a gold patch wins and
# this becomes a no-op.
set -e

cd /home/{pr.repo}

if [ -f go.mod ]; then
    exit 0
fi

# The module path must match the import path the v9 sources use internally
# (translations/*/*.go import "gopkg.in/go-playground/validator.v9"); using
# anything else makes the subpackages fail to resolve the parent.
go mod init gopkg.in/go-playground/validator.v9

# Pin era-appropriate dependency versions instead of letting `go mod tidy` float
# to latest. Plain `tidy` resolves locales v0.14.1, whose French number
# formatting switched to a narrow no-break space -- that makes
# translations/fr TestTranslations FAIL at the *base* commit, an
# environment-induced baseline failure unrelated to any PR under test. These are
# the versions the project's own go.mod pinned in v10.0, immediately after this
# era, and they give a fully green baseline.
go mod edit -require=github.com/go-playground/locales@v0.13.0
go mod edit -require=github.com/go-playground/universal-translator@v0.17.0
go mod edit -require=github.com/leodido/go-urn@v1.2.0
go mod edit -require=gopkg.in/go-playground/assert.v1@v1.2.1
go mod tidy

""".format(pr=self.pr),
            ),
            File(
                ".",
                "drop-bootstrap-if-patched.sh",
                """#!/bin/bash
# PR 542 is the Go-modules migration itself: its gold fix_patch CREATES go.mod
# and go.sum. prepare.sh has already left UNTRACKED bootstrap copies of exactly
# those paths, so `git apply` aborts with
#   error: go.mod: already exists in working directory
# and the whole stage captures zero test results (report invalid, check #1).
#
# Remove the synthesised copies ONLY when an incoming patch supplies its own, so
# the gold module definition wins. PRs 484/496 never touch go.mod, so this is a
# no-op for them and their (already green) behaviour is unchanged.
set -e

cd /home/{pr.repo}

for patch in "$@"; do
    [ -f "$patch" ] || continue
    if grep -qE '^\\+\\+\\+ b/go\\.(mod|sum)$' "$patch"; then
        rm -f go.mod go.sum
        break
    fi
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# The repo is already cloned and checked out at the base commit by dockerfile(),
# so this script performs no git checkout of its own.
set -e

cd /home/{pr.repo}

bash /home/bootstrap-module.sh

# go.mod/go.sum are deliberately left UNTRACKED and are never committed. The
# hardening block appended after this script re-checks out the base commit
# detached; a commit created here would be discarded by that checkout and then
# pruned by the gc, taking go.mod with it and breaking every test run. Untracked
# files survive it. Assert the tracked tree is still pristine before hardening.
test -z "$(git status --porcelain -uno)"

# Warm the module + build caches so the eval runs work without network.
go mod download
go test -v -count=1 ./... || true

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

cd /home/{pr.repo}
bash /home/drop-bootstrap-if-patched.sh /home/test.patch
git apply /home/test.patch
bash /home/bootstrap-module.sh
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/drop-bootstrap-if-patched.sh /home/test.patch /home/fix.patch
git apply /home/test.patch /home/fix.patch
bash /home/bootstrap-module.sh
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # ${BASE_COMMIT} must be defined before the checkout and the hardening
        # block reference it; build_dataset only injects that buildarg for
        # *string* dependencies, so it is baked in from the PR's base.sha.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{self.extra_setup()}

"""

        # Anti-reward-hacking hardening, concatenated raw so its ${BASE_COMMIT}
        # and %(refname) tokens stay literal. Runs after prepare.sh (which
        # bootstraps the Go module and warms the caches) and before CMD.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("go-playground", "validator_542_to_484")
class VALIDATOR_542_TO_484(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ValidatorV9ImageDefault(self.pr, self._config)

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Instance.create() routes on f"{org}/{number_interval}" whenever the delivered
# PR carries a number_interval, so the v9-era intervals must be registered here
# too or create() raises "Instance ... is not registered" before any image is
# built. These three bundles are exactly the commits with no go.mod in the tree;
# see _V9_ERA_PRS in validator.py, which routes the same PRs when the JSONL
# carries no number_interval at all.
_V9_NUMBER_INTERVALS = [
    "484-489",
    "496-529-530-535",
    "542-543",
]
for _interval in _V9_NUMBER_INTERVALS:
    Instance.register("go-playground", _interval)(VALIDATOR_542_TO_484)
