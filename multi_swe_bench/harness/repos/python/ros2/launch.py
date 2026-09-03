import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Dockerfile layout contract
# ---------------------------------------------------------------------------
# The BASE Dockerfile stops at `git clone` and then `CMD ["/bin/bash"]`.
# Nothing else follows the clone -- no checkout, no history scrub.
# The PR Dockerfile owns the commit pin AND the git stripping/hardening.
# prepare.sh deliberately contains NO hardening.
#
# HOW THIS IS ENFORCED WITHOUT TOUCHING image.py
# ----------------------------------------------
# DockerfileEnhancer.enhance() would normally rewrite any `RUN git clone ...`
# line into clone + reset + `checkout ${BASE_COMMIT}` + _HARDENING_BLOCK + CMD
# (see DockerfileEnhancer._standardize_repo_fetch). That is exactly the layout
# we are moving away from. enhance() has two documented early-outs:
#
#     dep = image.dependency()
#     raw = image.dockerfile()
#     if not isinstance(dep, str):        # -> PR layer: returned verbatim
#         return raw
#     if cls.SYNTAX_DIRECTIVE in raw:     # -> base layer: returned verbatim
#         return raw
#
# So:
#   * The BASE emits `# syntax=docker/dockerfile:1.6` as its own first line.
#     enhance() then returns it byte-for-byte and injects nothing.
#   * The PR layer's dependency() is an Image, not a str, so it is returned
#     verbatim too.
# Because the enhancer no longer injects the infrastructure block, the BASE has
# to supply it itself. It does that by reusing the very same constants from
# image.py (_TARGETARCH_ARG / _PROXY_ARGS / _ENV_BLOCK / _CERT_SYMLINKS), so the
# proxy + MITM-CA wiring stays identical to every other repo and cannot drift.
#
# BUILD-ARG CONSEQUENCE
# ---------------------
# build_dataset.py only passes REPO_URL/BASE_COMMIT when dependency() is a str,
# i.e. only to the BASE image. The PR layer therefore receives no build args, so
# it declares `ARG BASE_COMMIT="<sha>"` with the SHA as a literal default. That
# is what lets the shared _HARDENING_BLOCK -- which references ${BASE_COMMIT} --
# be reused verbatim in the PR layer.

_PYTEST_FLAGS = (
    "--no-header -rA --tb=no -p no:cacheprovider -v "
    "-p no:launch_testing -p no:launch -p no:launch_pytest "
    "--timeout=60 "
    '-o "addopts=" '
    "-W ignore::pytest.PytestRemovedIn9Warning "
    # Without this, ONE unimportable test file aborts pytest's entire session
    # for that package. test.patch adds test_log.py, which imports an action
    # that only fix.patch creates, so in the test stage its import fails and
    # pytest reports "Interrupted: 1 error during collection" -- silently
    # discarding the other 260 collected tests in launch/, 21 in launch_xml and
    # 8 in launch_yaml. Measured effect on PR 858: test stage 68 -> 359 passing.
    # Those suppressed tests came back as TestStatus.NONE, producing the
    # PASS/NONE/FAIL pattern Report.check() rejects as anomalous.
    "--continue-on-collection-errors"
)

# The graded pytest invocation, defined once and reused verbatim by run.sh,
# test-run.sh and fix-run.sh, so the three stages differ ONLY by which patch was
# applied. If the command itself varied between stages, a FAIL->PASS transition
# could come from the command rather than from the fix, making f2p meaningless.
_PYTEST_LOOP = (
    "RESULT=0; "
    "for pkg in launch launch_testing launch_xml launch_yaml; do "
    'd="$pkg/test/$pkg"; '
    'if [ -d "$d" ]; then '
    'echo "=== Running tests in $d ==="; '
    'python3 -m pytest "$d" {flags} || RESULT=$?; '
    "fi; "
    "done; "
    "exit $RESULT"
).format(flags=_PYTEST_FLAGS)

# Editable installs of the in-tree subpackages. These depend on the CHECKED-OUT
# source, so they run in prepare.sh (at PR-image build time) and again in the
# graded scripts after a patch has changed the source.
_EDITABLE_INSTALL = (
    "pip3 install --break-system-packages -e launch/ -e launch_testing/ -e launch_xml/ -e launch_yaml/ "
    "2>/dev/null || true ; "
    "if [ -d launch_pytest ] && [ -f launch_pytest/setup.py ]; then "
    "pip3 install --break-system-packages -e launch_pytest/ 2>/dev/null || true; fi"
)

# External (non-repo) Python dependencies. These live in prepare.sh, NOT in the
# base Dockerfile, so the base stops at `git clone` + CMD as required.
#
# COST NOTE: because prepare.sh runs once per PR image, this dependency set --
# including seven `ament-*` packages fetched from git -- is installed 5 times
# for this dataset instead of once in the shared base. That is the deliberate
# trade for keeping the base minimal.
#
# The ament git refs are intentionally left unpinned here to match the config as
# supplied; be aware this resolves to whatever is on those default branches at
# build time (ament-lint 0.21.1 / ament-index-python 1.14.2 as of this writing),
# so the image is not byte-reproducible across rebuilds.
_EXTERNAL_DEPS = (
    'pip3 install --break-system-packages lark osrf-pycommon pyyaml "pytest<9" '
    "pytest-cov pytest-timeout typing_extensions "
    '"importlib-metadata<5.0" mock flake8 pydocstyle mypy '
    '"ament-copyright @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_copyright" '
    '"ament-index-python @ git+https://github.com/ament/ament_index.git#subdirectory=ament_index_python" '
    '"ament-mypy @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_mypy" '
    '"ament-flake8 @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_flake8" '
    '"ament-pep257 @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_pep257" '
    '"ament-lint @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_lint" '
    '"ament-xmllint @ git+https://github.com/ament/ament_lint.git#subdirectory=ament_xmllint"'
)

# ament index + frontend grammar wiring. Also moved out of the Dockerfiles.
# NOTE: the matching `ENV AMENT_PREFIX_PATH=/usr/local` CANNOT move here -- an
# `export` inside prepare.sh dies with that shell and would not be set when the
# graded run scripts execute later. It stays as a Dockerfile ENV in the base,
# where it is static, repo-independent environment configuration.
_AMENT_INDEX_SETUP = (
    "mkdir -p /usr/local/share/ament_index/resource_index/packages && "
    "touch /usr/local/share/ament_index/resource_index/packages/launch && "
    "touch /usr/local/share/ament_index/resource_index/packages/launch_testing && "
    "touch /usr/local/share/ament_index/resource_index/packages/launch_xml && "
    "touch /usr/local/share/ament_index/resource_index/packages/launch_yaml"
)

_GRAMMAR_SETUP = (
    "mkdir -p /usr/local/share/launch/frontend && "
    "if [ -f launch/share/launch/frontend/grammar.lark ]; then "
    "cp launch/share/launch/frontend/grammar.lark "
    "/usr/local/share/launch/frontend/grammar.lark; fi"
)


class ROS2LaunchImageBase(Image):
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
        # ubuntu:24.04 ships Python 3.12; ubuntu:22.04 ships 3.10.
        # PR 858's fix patch adds launch/launch/actions/log.py, which calls
        # logging.getLevelNamesMapping() -- an API added in Python 3.11. On 3.10
        # that raises AttributeError, deterministically failing
        # test_timer_action_launch_configurations (measured 0/8 passes on 3.10
        # with the fix applied, 8/8 without it). That is a PASS->FAIL transition
        # between the test and fix stages, which Report.check() Rule 2 forbids,
        # so PR 858 could never resolve on 22.04.
        # 22.04 only offers python3.11.0~rc1 (a release candidate), so 24.04 is
        # the correct move rather than backporting an interpreter.
        return "ubuntu:24.04"

    def image_tag(self) -> str:
        # ONE shared base for the whole repo config, not a per-PR tag.
        #
        # This is only safe because of the layout contract above: the base stops
        # at `git clone` and carries no checkout and no history scrub, so nothing
        # in it varies per PR -- every PR would otherwise render a byte-identical
        # image under a different tag. The commit pin and the scrub live in the
        # PR layer, which is what makes each PR distinct.
        #
        # (Under the OLD layout a shared base tag was genuinely unsafe: the
        # hardening block detached at one ${BASE_COMMIT}, deleted every other ref
        # and gc-pruned unreachable objects, so a shared base stayed pinned to
        # whichever PR built it FIRST and any second PR reusing it would find its
        # own base commit already pruned away. Moving the scrub out removes that
        # hazard entirely.)
        #
        # Image.__hash__/__eq__ key on image_full_name() and build_dataset.py
        # collects images into a set, so all 5 PRs collapse to this single base
        # and it is built exactly once.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        # The base stages nothing: no patches, no scripts. Those belong to the
        # PR layer. (The previous revision COPY'd fix.patch/test.patch into the
        # base, which put PR-specific content in the shared environment image.)
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"

        # Reuse image.py's own infrastructure constants so proxy/CA/locale
        # wiring is identical to every other repo and cannot drift out of sync.
        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )
        labels = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM ubuntu:24.04

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

# Static, repo-independent env. Must be a Dockerfile ENV rather than an export
# in prepare.sh, so it is still set when the graded run scripts execute.
ENV AMENT_PREFIX_PATH=/usr/local

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    python3 \\
    python3-pip \\
    python3-venv \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class ROS2LaunchImageDefault(Image):
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
        return ROS2LaunchImageBase(self.pr, self.config)

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

# NOTE: no git stripping/hardening here by design -- the scrub is the LAST thing
# the PR Dockerfile does, after this script has finished.
#
# This deliberately does NOT call check_git_changes.sh. That clean-tree assert
# runs in the PR Dockerfile right after the checkout, which is the only point
# where it is meaningful: the editable installs below write *.egg-info/ into the
# source tree, so from here on a porcelain check would always report dirty.
# What still matters is that the tree is parked on the right commit, so that is
# what gets asserted -- an invariant the egg-info noise cannot mask.
cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD pinned at {sha}"

# External (non-repo) Python dependencies.
{external}

# Editable installs of the in-tree subpackages.
{editable}

# ament index entries for the four subpackages.
{ament}

# Frontend grammar.
{grammar}

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    external=_EXTERNAL_DEPS,
                    editable=_EDITABLE_INSTALL,
                    ament=_AMENT_INDEX_SETUP,
                    grammar=_GRAMMAR_SETUP,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
export PYTHONPATH=/home/{repo}/launch/test:$PYTHONPATH
{pytest}

""".format(repo=self.pr.repo, pytest=_PYTEST_LOOP),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}

# A patch that fails to apply must ABORT, not fall through. The previous
# revision ended this chain with `|| true`, so an unappliable test.patch left
# the tree at baseline and the "test" stage silently re-ran the "run" stage --
# producing an empty or bogus f2p set that looks like a legitimate result.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "test.patch did not apply cleanly, retrying with --3way" >&2
    if ! git apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply of test.patch failed" >&2
        exit 1
    fi
fi
git add -A

{editable}

export PYTHONPATH=/home/{repo}/launch/test:$PYTHONPATH
{pytest}

""".format(repo=self.pr.repo, editable=_EDITABLE_INSTALL, pytest=_PYTEST_LOOP),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "test.patch did not apply cleanly, retrying with --3way" >&2
    if ! git apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply of test.patch failed" >&2
        exit 1
    fi
fi
git add -A

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "fix.patch did not apply cleanly, retrying with --3way" >&2
    if ! git apply --whitespace=nowarn --3way /home/fix.patch; then
        echo "Error: git apply of fix.patch failed" >&2
        exit 1
    fi
fi

{editable}

export PYTHONPATH=/home/{repo}/launch/test:$PYTHONPATH
{pytest}

""".format(repo=self.pr.repo, editable=_EDITABLE_INSTALL, pytest=_PYTEST_LOOP),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        # check_git_changes.sh is COPY'd early and run right after the scrub,
        # so it is excluded from the bulk COPY below to avoid a duplicate layer.
        copy_commands = "".join(
            f"COPY {f.name} /home/\n"
            for f in self.files()
            if f.name != "check_git_changes.sh"
        )

        # ARG default carries the SHA because build_dataset.py passes build args
        # only to the base image (dependency() is a str there, an Image here).
        # _HARDENING_BLOCK references ${BASE_COMMIT}, so it needs this to resolve.
        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

# Clean-tree assert, immediately after the checkout: this is the last moment the
# working tree is still pristine. prepare.sh below writes *.egg-info/ into the
# source tree via the editable installs, so running this any later would report
# "Uncommitted changes" and fail the build.
COPY check_git_changes.sh /home/
RUN bash /home/check_git_changes.sh

WORKDIR /home/

{copy_commands}
RUN bash /home/prepare.sh

# Git stripping/hardening LAST, after prepare.sh.
#
# WORKDIR must be restored to the repo first: the block above left it at /home/,
# and every command in the scrub is a git operation that has to run inside the
# work tree.
#
# Running the scrub after prepare.sh is safe: none of its four assertions look at
# the working tree, only at git state (HEAD == BASE_COMMIT, no refs, no remotes,
# reachable-object count). The *.egg-info/ files prepare.sh leaves behind are
# untracked and cannot affect any of them, nor block `git checkout --detach`.
WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("ros2", "launch")
class ROS2_LAUNCH(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ROS2LaunchImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI. The previous revision only stripped colour SGR sequences
        # (\x1b[...m); this covers every CSI final byte, so a cursor-movement or
        # erase sequence cannot silently break the anchored patterns below.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # `-v` per-test lines: "path/to/test.py::test_name PASSED [ 42%]"
        verbose_passed = re.compile(r"^(.*?)\s+PASSED\s+\[\s*\d+%\]$")
        verbose_failed = re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$")
        verbose_error = re.compile(r"^(.*?)\s+ERROR\s+\[\s*\d+%\]$")

        # `-rA` short-summary lines: "PASSED path::test_name"
        summary_passed = re.compile(r"^\s*PASSED\s+(\S+)")
        summary_failed = re.compile(r"^\s*FAILED\s+(\S+)")
        summary_error = re.compile(r"^\s*ERROR\s+(\S+)")
        skipped_pattern = re.compile(r"^\s*SKIPPED\s+\[\d+\]\s+(\S+)", re.IGNORECASE)

        for line in clean_log.splitlines():
            stripped = line.strip()

            for pat, bucket in (
                (verbose_passed, passed_tests),
                (verbose_failed, failed_tests),
                (verbose_error, failed_tests),
                (summary_passed, passed_tests),
                (summary_failed, failed_tests),
                (summary_error, failed_tests),
                (skipped_pattern, skipped_tests),
            ):
                match = pat.match(stripped)
                if match:
                    bucket.add(match.group(1).strip())
                    break

        # Enforce TestResult's disjointness invariants explicitly.
        # TestResult.__post_init__ raises ValueError if any two sets intersect,
        # which would crash the run. Precedence: failure wins, then skip, then
        # pass. The previous revision omitted this entirely, so a test that both
        # errored and passed (e.g. a retry, or the same name from two of the
        # four package loops) could abort the whole instance.
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