import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


# The exact pytest invocation, defined once and reused verbatim by run.sh,
# test-run.sh and fix-run.sh. Sharing one constant is what guarantees the three
# graded stages differ ONLY by which patch was applied -- if the command itself
# varied between stages, a FAIL->PASS transition could come from the command
# rather than from the fix, and the f2p/n2p signal would be meaningless.
_PYTEST_CMD = (
    'python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""'
)

# Installed identically on both architectures. An earlier revision of this
# config branched on `uname -m`, using `uv sync` on aarch64 and `pip install -e`
# on x86_64. That is a real correctness hazard for a multi-arch image: the two
# installers resolve dependencies differently, so the same PR could legitimately
# produce different test results per architecture. Verified on a real
# linux/arm64 container that plain pip works here (exit 0, 1429 tests
# collected), which it should -- pyproject.toml declares the pure-Python
# hatchling backend and the test extra is only pytest + pytest-cov, with no
# native/C-extension dependency to fall over on arm64.
_PIP_INSTALL = "pip install -e '.[test]'"


class SpecKitImageBase(Image):
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
        # pyproject.toml at this base commit declares requires-python = ">=3.11",
        # so 3.11 is the correct floor. Pinned (not :latest) and published for
        # both linux/amd64 and linux/arm64.
        return "python:3.11-slim"

    def image_tag(self) -> str:
        # Per-PR, NOT a shared "base" tag. The hardening block injected into the
        # rendered base Dockerfile detaches at one ${BASE_COMMIT}, deletes every
        # other ref and gc-prunes unreachable objects, then asserts
        # rev-list --all == rev-list HEAD. A shared tag would stay permanently
        # pinned to whichever PR built it FIRST, and a second PR reusing it
        # would find its own base commit already pruned away.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Rendered after `git checkout ${BASE_COMMIT}`, with WORKDIR already at
        # /home/spec-kit. Installing here bakes the dependency tree into the base
        # image so the graded runs do not each pay a cold install.
        return f"RUN {_PIP_INSTALL}"

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


class SpecKitImageDefault(Image):
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
        return SpecKitImageBase(self.pr, self.config)

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

# Re-install so the editable install points at the checked-out tree. `|| true`
# because a warm-up hiccup must not fail the image build -- the graded runs
# decide pass/fail, not this.
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


@Instance.register("github", "spec-kit")
class SpecKit(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpecKitImageDefault(self.pr, self._config)

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
        # from this repo at 2f5417f0:
        #   PASSED tests/test_presets.py::TestPresetManifest::test_valid_manifest
        #   FAILED tests/test_x.py::test_y - AssertionError: ...
        #   ERROR  tests/test_x.py::test_y
        #   SKIPPED [1] tests/test_x.py:23: reason
        # Node IDs are file::class::method, so names are inherently unique and
        # carry no timing or count metadata -- they stay identical across the
        # run/test/fix stages, which is what Report's cross-stage union needs.
        re_passed = re.compile(r"^PASSED\s+(\S+)", re.MULTILINE)
        re_failed = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
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
        # passing. (The previous revision omitted this entirely.)
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