import copy
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


# =============================================================================
# !!! SHARED BASE IMAGE -- KNOWN DATASET-LEAKAGE TRADE-OFF, CHOSEN DELIBERATELY
# =============================================================================
# This config builds ONE base image shared by all 5 PRs (6 images total instead
# of 10). That is an explicit operator decision, taken with the consequence
# below understood. Read this before changing or copying this file.
#
# WHY IT LEAKS. A single base image can only be pinned to ONE commit. It has to
# be the NEWEST base commit in the dataset (004ed749, PR 241): every other PR's
# base commit is an ancestor of it and therefore still present after the
# history scrub, whereas pinning to an older commit would leave the newer PRs'
# commits absent entirely and their prepare.sh checkout would fail.
#
# But pinning to the newest also retains every commit made AFTER each older
# PR's base. Measured with `git rev-list --count <base>..004ed749`:
#     PR 105 -> 490 future commits present
#     PR  76 -> 257
#     PR 187 -> 194
#     PR 185 -> 117
# and `git merge-base --is-ancestor <merge_commit> 004ed749` confirms that EACH
# PR's own merge commit -- its ground-truth fix -- is reachable:
#     PR 76 429574e8, PR 105 e869503e, PR 185 47d914a5, PR 187 59422ae2
#
# WHAT THAT MEANS IN PRACTICE. prepare.sh still checks out the correct per-PR
# SHA, so the WORKING TREE is right and every graded run is valid. But .git
# retains the full 799-commit history, so anything with shell access inside the
# container can run `git log --all` or `git show <merge sha>` and read the exact
# patch it is supposed to produce. The harness's four scrub assertions still
# pass (HEAD == BASE_COMMIT, no refs, no remotes, rev-list --all == rev-list
# HEAD) because the future commits are legitimately reachable from HEAD --
# so neither dataset validation nor the Dockerfile QC will flag this.
#
# IF YOU NEED LEAK-FREE IMAGES, revert to per-PR base tags: delete
# _SHARED_BASE_COMMIT / _SHARED_BASE_TAG, restore
#     def image_tag(self):  return f"base-pr-{self.pr.number}"
#     def workdir(self):    return f"base-pr-{self.pr.number}"
# and drop the __init__ override that rewrites base.sha. That yields 10 images
# and no instance can see past its own base commit.
# =============================================================================

# The newest base commit in this dataset (PR 241, 2020-08-12). Every other base
# commit in the 5-PR set is an ancestor of it, so this is the only value that
# lets a single shared base serve all five.
_SHARED_BASE_COMMIT = "004ed7492ef45b9cb26f7711981c2a657004ef04"

# One constant tag for every PR. Image defines __hash__/__eq__ on the image name
# (image.py:89,92), so all five base Image objects collapse to a single entry in
# build_dataset's `images: dict[str, set[Image]]` and only one image is built.
_SHARED_BASE_TAG = "base"


# --- The graded pytest invocation ------------------------------------------
# Defined once and reused verbatim by run.sh, test-run.sh and fix-run.sh, so the
# three graded stages differ ONLY by which patch was applied. If the command
# varied between stages, a FAIL->PASS transition could come from the command
# rather than from the fix, and the f2p/n2p signal would be meaningless.
#
# --continue-on-collection-errors is LOAD-BEARING, not cosmetic. The repo ships
# `level_file_templates/`, an authoring scaffold whose test_levels.py contains
# literal `{}` placeholders rather than real code. At several of this dataset's
# base commits it fails to import at all:
#     d3843307 -> ImportError: cannot import name 'BasicLevel' from gitgud.skills.util
#     2d1ada13 -> KeyError: '{}'  (skill['{}'] is a template placeholder)
# pytest treats a collection error as fatal ("Interrupted: 1 error during
# collection") and runs ZERO tests, which would have produced empty f2p/n2p for
# 2 of the 5 PRs. Measured in Docker across all five base commits:
#     without the flag:  1 error / 20 / 67 / 1 error / 119
#     with    the flag:  21 / 20 / 67 / 59 / 119
#
# Deliberately NOT `--ignore=level_file_templates`. PR 187's test patch modifies
# that very file, and its fix patch repairs it -- verified in Docker, the file
# goes from "1 error" at base to "1 skipped" once test+fix are applied. Ignoring
# the directory would have silently discarded part of that PR's own change.
_PYTEST_CMD = (
    "python -m pytest --no-header -rA --tb=no -p no:cacheprovider "
    '--continue-on-collection-errors -o addopts=""'
)

# pytest is NOT in this project's install_requires (setup.py declares only
# gitpython, importlib_resources and, at later commits, pyyaml), so it has to be
# installed explicitly. Pinned for reproducibility: verified in Docker that
# 7.4.4 produces results identical to the then-latest 8.4.2 on all five base
# commits, and that it supports --continue-on-collection-errors.
_PIP_INSTALL = "pip install -e . && pip install pytest==7.4.4"

# git-gud is a tool for LEARNING git: its tests drive GitPython to init real
# repositories and create real commits. Without a global identity, git refuses
# to commit ("Please tell me who you are") and the level tests fail. This is set
# in the BASE image so it lands in /root/.gitconfig and survives prepare.sh's
# `git reset --hard` (which only touches the repo working tree, not ~/.gitconfig).
# init.defaultBranch is pinned so newly-created test repos get a deterministic
# branch name instead of git's version-dependent default plus a warning.
_GIT_IDENTITY = (
    'git config --global user.email "test@example.com" && '
    'git config --global user.name "Test User" && '
    "git config --global init.defaultBranch master"
)


class GitGudImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        # Rewrite base.sha to the shared newest commit. build_dataset passes
        # `buildargs["BASE_COMMIT"] = image.pr.base.sha` (build_dataset.py:629),
        # so this is what actually pins the single shared base image. Without
        # it the surviving image would be pinned to whichever PR won an
        # unordered set iteration -- and if that were not the newest commit,
        # the newer PRs' base commits would be absent from the clone and their
        # prepare.sh checkout would fail. Deep-copied so the PR layer, which
        # must keep each PR's REAL base.sha for its own checkout, is unaffected.
        shared_pr = copy.deepcopy(pr)
        shared_pr.base.sha = _SHARED_BASE_COMMIT
        self._pr = shared_pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        # setup.py declares python_requires>=3.6 (>=3.5 at the oldest commit in
        # this dataset), so 3.9 is comfortably inside the supported range while
        # still being a maintained image. Chosen by measurement, not by guess:
        # on python:3.11-bookworm this 2020-era code produces 16
        # `argparse.ArgumentError` collection errors because argparse got
        # stricter about conflicting subparsers; on 3.9 the same commit is
        # 119 passed / 1 skipped / 0 errors. 3.8 also passes but runs ~2.5x
        # slower (21s vs 8s). Pinned, and published for both linux/amd64 and
        # linux/arm64.
        return "python:3.9-bookworm"

    def image_tag(self) -> str:
        # Constant on purpose -- this is what collapses five base images into
        # one. See the SHARED BASE IMAGE banner at the top of this file for the
        # leakage trade-off this buys.
        return _SHARED_BASE_TAG

    def workdir(self) -> str:
        # Must also be constant: a per-PR workdir would give the single shared
        # image five different build contexts and defeat the deduplication.
        return _SHARED_BASE_TAG

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Rendered after `git checkout ${BASE_COMMIT}`, with WORKDIR already at
        # /home/git-gud. Baking the dependency tree into the base image means the
        # graded runs do not each pay a cold install.
        return f"RUN {_GIT_IDENTITY}\nRUN {_PIP_INSTALL}"

    def dockerfile(self) -> str:
        # Reimplements Image.dockerfile() rather than calling super(), for one
        # reason only: the base class hardcodes its own
        # "ENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8" here, and
        # DockerfileEnhancer._ENV_BLOCK (injected into every rendered Dockerfile)
        # already sets both -- plus TZ and the proxy/CA vars -- earlier in the
        # same file. The duplicate is a harmless no-op but this project's
        # Dockerfile QC flags it, and patching the shared base class would touch
        # every other default-template repo. Everything below is otherwise
        # byte-for-byte Image.dockerfile() with that one ENV pair dropped.
        base_img = self.dependency()
        if isinstance(base_img, Image):
            raise NotImplementedError(
                "Subclass must override dockerfile() or return a string from dependency()"
            )

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]

        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        clone_section = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")

        sections.append(apt_command)
        sections.append(clone_section)
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class GitGudImageDefault(Image):
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
        return GitGudImageBase(self.pr, self.config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Re-install so the editable install points at the checked-out tree. The
# `git clean -fd` above removes git_gud.egg-info/, so this has to run after it.
# (That directory is listed in the repo's own .gitignore, so it never dirties
# `git status --porcelain` and never trips check_git_changes.sh -- verified in
# Docker.) `|| true` because a warm-up hiccup must not fail the image build:
# the graded runs decide pass/fail, not this.
{pip_install} || true

""".format(repo=self.pr.repo, sha=self.pr.base.sha, pip_install=_PIP_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{pytest_cmd}

""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{pytest_cmd}

""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{pytest_cmd}

""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("benthayer", "git-gud")
class GitGud(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GitGudImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI first. pytest emits colour whenever it believes it has a
        # TTY, and a stray escape sequence silently breaks every pattern below --
        # producing an empty TestResult that looks like "no tests ran" rather
        # than like a parsing bug.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Patterns match pytest's `-rA` short-summary block, captured verbatim
        # from this repo at 004ed749 with the pinned pytest 7.4.4:
        #   PASSED gitgud/test_commands.py::test_load
        #   PASSED gitgud/skills/test_levels.py::test_skill_types[skill0]
        #   FAILED gitgud/test_operator.py::test_x - AssertionError: ...
        #   ERROR  gitgud/skills/basics/test_levels.py::test_level[level0-commands0]
        #   SKIPPED [1] level_file_templates/test_levels.py:13: got empty parameter set
        # Node IDs are file::function[param], contain no spaces, and carry no
        # timing or count metadata -- so they stay identical across the
        # run/test/fix stages, which is what Report's cross-stage union needs.
        re_passed = re.compile(r"^PASSED\s+(\S+)", re.MULTILINE)
        re_failed = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
        # ERROR covers both per-test errors and whole-module collection errors,
        # which this repo genuinely produces from level_file_templates/ (see the
        # note on --continue-on-collection-errors above). They fail identically
        # in every stage, so they are excluded from f2p/p2p automatically.
        re_error = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
        # XFAIL/XPASS are reported as expected/unexpected outcomes; an XPASS is a
        # genuine surprise pass and is counted as failing, matching pytest's own
        # non-zero exit behaviour under strict xfail.
        re_xpass = re.compile(r"^XPASS\s+(\S+)", re.MULTILINE)
        # SKIPPED in the -rA summary is reported as `[count] file:line: reason`,
        # not as a node ID -- that is pytest's format, not a parsing choice. The
        # shape is stable across stages, which is what matters for the union.
        re_skipped = re.compile(r"^SKIPPED\s+\[\d+\]\s+(\S+?):\s", re.MULTILINE)

        passed_tests.update(re_passed.findall(test_log))
        failed_tests.update(re_failed.findall(test_log))
        failed_tests.update(re_error.findall(test_log))
        failed_tests.update(re_xpass.findall(test_log))
        skipped_tests.update(re_skipped.findall(test_log))

        # Enforce TestResult's disjointness invariants explicitly.
        # TestResult.__post_init__ raises ValueError if any two sets intersect,
        # which would crash the run. Precedence: failure wins, then skip, then
        # pass -- so a test that is retried and fails is never also counted as
        # passing. This matters here specifically because a module can appear as
        # a collection ERROR while individual tests from a sibling module pass.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )