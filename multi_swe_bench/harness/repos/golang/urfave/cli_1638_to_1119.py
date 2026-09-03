import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Covers urfave/cli PRs 1638, 1631, 1606, 1257 and 1119 (newest to oldest, the
# ordering the file name uses and the one existing interval configs follow, e.g.
# c_ares_1053_to_659 and notcurses_349_to_99999).
#
# Registered under "cli" rather than "cli_1638_to_1119" on purpose. Instance
# .create resolves `f"{pr.org}/{pr.number_interval}"` only when number_interval
# is set; this dataset leaves it unset, so the lookup is `urfave/cli`. Taking
# that key -- with the sibling cli import commented out in __init__.py -- makes
# this config authoritative without touching the JSONL, mirroring how
# repos/c/c_ares/__init__.py keeps exactly one interval config active.
_ORG = "urfave"
_REPO = "cli"

# The PR numbers this interval covers, newest to oldest.
#
# They are registered as additional keys because the two dataset files this
# config has to serve resolve differently:
#
#   output/urfave__cli_raw_dataset.jsonl   number_interval unset -> "urfave/cli"
#   data/dataset/urfave__cli_dataset.jsonl number_interval="1638" -> "urfave/1638"
#
# The generated dataset (written by the dataset run, and the natural input for a
# --mode image multi-arch build) stamps each record's own PR number into
# number_interval, and Instance.create prefers that field over org/repo. Without
# these keys every instance from that file dies with
# "Instance 'urfave/1638' is not registered."
_PR_NUMBERS = ("1638", "1631", "1606", "1257", "1119")

# go.mod declares `go 1.18` at both ends of the range (595cabc60c and
# 6686660662). 1.21 is pinned rather than floating `latest`: it satisfies that
# directive, is contemporary with the PRs, and keeps the image reproducible.
_GO_IMAGE = "golang:1.21"

# All five instances yield a base image with the same full name and the pipeline
# collects them into a set, so exactly one survives and its pr.base.sha becomes
# the BASE_COMMIT build-arg. Left to chance that is the first record in the
# JSONL, which matters because the scrub prunes the clone to that commit's
# ancestry -- a PR outside it could no longer be checked out.
#
# Seeding from the newest PR removes the problem: every base commit here is on
# `main` and strictly ordered, so each older one is an ancestor of the newest.
# Verified against GitHub's compare API: 595cabc60c, 94c9951e4a, 68a382fe39 and
# 87b48e2ddd are each "behind: 0" relative to 6686660662 (PR 1638).
_BASE_SEEDS: dict[str, PullRequest] = {}


def _register_base_seed(pr: PullRequest) -> None:
    key = f"{pr.org}/{pr.repo}"
    current = _BASE_SEEDS.get(key)
    if current is None or pr.number > current.number:
        _BASE_SEEDS[key] = pr


def _base_seed(pr: PullRequest) -> PullRequest:
    return _BASE_SEEDS.get(f"{pr.org}/{pr.repo}", pr)


# The history scrub lives in the PR layer, not the base image and not
# prepare.sh. That is possible because DockerfileEnhancer only rewrites *base*
# Dockerfiles -- it returns a PR Dockerfile untouched, since that image's
# dependency is an Image rather than a string. The literal SHA is interpolated
# for the same reason: an un-enhanced Dockerfile has no ${BASE_COMMIT} ARG.
_HARDENING = '''RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi'''


class UrfaveCli1638To1119ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        # Deliberately not self._pr: the base image is shared across the
        # interval, so it must describe the seed commit rather than whichever
        # instance constructed it. org/repo are identical across instances, so
        # only base.sha -- the BASE_COMMIT build-arg -- actually changes.
        return _base_seed(self._pr)

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _GO_IMAGE

    def image_tag(self) -> str:
        # Not plain "base": the image name derives from pr.org/pr.repo, so this
        # config and the sibling cli.py both resolve to mswebench/urfave_m_cli.
        # A distinct tag keeps their images apart if cli.py is ever re-enabled.
        return "base-1638-to-1119"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # `git -C /home clone <url> <dir>` rather than `git clone <url> <path>`.
        # Both do the same thing -- -C chdirs first, so the repo still lands in
        # /home/<repo> -- but the spelling is load-bearing, and removing it will
        # silently change the generated Dockerfile.
        #
        # DockerfileEnhancer touches a base Dockerfile in two ways:
        #   1. _standardize_repo_fetch replaces a line matching
        #      ^RUN\\s+git\\s+clone\\s+... with its own clone/checkout/scrub
        #      block terminated by CMD. `git -C /home clone` does not match that
        #      anchor, so the line is left alone.
        #   2. _inject_final_sanitize appends the history scrub before the last
        #      CMD, and its sole escape hatch is
        #          if not any(tok in content
        #                     for tok in ("git clone", "git fetch",
        #                                 "git remote add")):
        #              return content
        #      The string "git -C /home clone" contains none of those three
        #      tokens, so the base is returned untouched.
        #
        # Net effect: the base Dockerfile ends at the clone plus CMD, and the
        # scrub lives only in the PR layer, as specified. The tree is still
        # fully hardened -- just one layer later, pinned per-PR rather than to
        # the shared seed commit.
        #
        # Caveat worth knowing: this depends on a substring check in
        # multi_swe_bench/harness/image.py. If that guard is ever reworded, the
        # scrub silently reappears in the base. The durable fix is a real opt-out
        # in the enhancer.
        code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class UrfaveCli1638To1119ImageDefault(Image):
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
        return UrfaveCli1638To1119ImageBase(self.pr, self._config)

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
bash /home/check_git_changes.sh

go env -w GOFLAGS=-mod=mod
go mod download || true
go build ./... || true
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
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --3way..."; git apply --3way /home/test.patch 2>&1 || true; }}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --3way..."; git apply --3way /home/test.patch 2>&1 || true; git apply --3way /home/fix.patch 2>&1 || true; }}
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

        hardening = _HARDENING.format(sha=self.pr.base.sha)

        # Checkout and history scrub sit here rather than in the base image or
        # prepare.sh: the base stops at the clone, and prepare.sh only warms the
        # Go build cache against an already-pinned, already-scrubbed tree.
        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}

{hardening}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register(_ORG, _REPO)
class UrfaveCli1638To1119(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        # Every instance is constructed before the pipeline walks the dependency
        # graph, so the newest PR is known by the time any base image is built.
        _register_base_seed(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UrfaveCli1638To1119ImageDefault(self.pr, self._config)

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


# Same class, additional registry keys -- see _PR_NUMBERS above. Instance
# .register returns the class unchanged, so applying it again is just another
# dict entry pointing at the same implementation.
for _pr_number in _PR_NUMBERS:
    Instance.register(_ORG, _pr_number)(UrfaveCli1638To1119)
