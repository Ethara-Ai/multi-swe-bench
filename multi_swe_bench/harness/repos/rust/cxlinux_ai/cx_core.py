import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# cxlinux-ai/cx-core ("cortex-linux") -- an AI-assisted apt/package manager for
# Debian/Ubuntu. GitHub classifies the repository as **Rust** (the JSONL carries
# `base.repo.language == "Rust"`, which is what the collection pipeline's
# `language:rust` search matched), so this config lives under `repos/rust/` per
# the directory convention. The code that the dataset's PRs touch, however, is
# pure Python (`cortex/*.py` + `tests/test_*.py`, pytest) -- exactly the
# artifacthub/hub situation, where the directory follows GitHub's classification
# and the toolchain follows the actual test suite. There is no Cargo.toml and no
# .rs file anywhere in the tree at the dataset's base commits.
#
# Toolchain (verified at base.sha eedd7ec, PR #598):
#   pyproject.toml -> requires-python = ">=3.10", setuptools backend, NO lockfile
#                     (no poetry.lock / Pipfile.lock / pdm.lock) -> plain pip
#   [project.optional-dependencies].dev -> pytest, pytest-cov, pytest-asyncio,
#                     pytest-mock, pytest-timeout (+ lint tools)
#   [tool.pytest.ini_options] -> testpaths=["tests"], addopts="-v --tb=short",
#                     asyncio_mode="auto"
#   .github/workflows/ci.yml -> `pip install -e ".[dev]"` then
#                     `pytest tests/ -v --tb=short ... --timeout=60
#                      --ignore=tests/integration` on Python 3.10/3.11/3.12,
#                     with dummy ANTHROPIC_API_KEY / OPENAI_API_KEY exported.
# The test command below mirrors CI (coverage flags dropped -- they add nothing
# to the f2p signal) with two additions, both required by the harness:
#   * --continue-on-collection-errors: the test patch adds a test module that
#     imports a `cortex.<feature>` module which only exists AFTER the fix patch.
#     Without this flag pytest aborts the whole session on that ImportError
#     ("Interrupted: 1 error during collection") and the test stage reports zero
#     tests, throwing away every p2p test in the run.
#   * --ignore=tests/langchain_py313: that directory imports `langchain`, which
#     is not in `dependencies` or any extra -> a permanent collection error that
#     is pure noise (tests/integration is ignored by CI itself; it needs Docker).
_TEST_CMD = (
    "python -m pytest tests/ -v -rA --tb=short "
    "-p no:cacheprovider --continue-on-collection-errors --timeout=300 "
    "--ignore=tests/integration --ignore=tests/langchain_py313"
)


class CxCoreImageBase(Image):
    """Level 1: toolchain-only base image.

    `dependency()` returns a *string*, so the DockerfileEnhancer engages and
    prepends the `# syntax` directive, the TARGETARCH/REPO_URL/BASE_COMMIT ARGs,
    the proxy/cert ENV block, the cert symlinks, the MITM CA mount and the OCI
    labels. None of those are written here.

    The clone lives here, in the shared `:base` layer, so the five PRs of this
    repo pay for it once. It is written directly in the `RUN git clone
    "${REPO_URL}" ...` form the enhancer would otherwise rewrite to, which makes
    `_standardize_repo_fetch` a no-op on it, and it is followed by a comment
    carrying the `git rev-list --all --count` marker so that
    `_inject_final_sanitize` does NOT append the hardening block here. Both
    matter because image dedup is keyed on `image_full_name()`: all five PRs
    collapse onto this one tag, so a `${BASE_COMMIT}` checkout or a
    history-strip at this level would be pinned to whichever PR won the build
    race and would break `git checkout <base.sha>` for the other four. The
    per-PR checkout and the hardening therefore live in CxCoreImageDefault (an
    Image dependency, left verbatim by the enhancer).

    python:3.11-bookworm: the repo requires Python >=3.10 and CI's coverage /
    lint / codecov jobs pin 3.11, so 3.11 is the reference interpreter for the
    whole PR range (#598-#603, a five-PR span of ~a week -- no era split). The
    full (non-slim) Debian bookworm variant is used because it already ships
    git + build-essential headers, and because `cryptography` / `pyyaml` fall
    back to a source build on any platform lacking a wheel.
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

    def dependency(self) -> str:
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # All from Debian's own repositories -> resolve identically on amd64 and
        # arm64. No third-party apt source, no `[arch=...]` pin, no direct
        # binary download, so nothing here is architecture-specific.
        return ["pkg-config", "libssl-dev", "libffi-dev"]

    def dockerfile(self) -> str:
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # No `# syntax` directive, so the enhancer injects its infra block (ARGs,
        # proxy/cert ENV, cert symlinks, OCI labels) right after the FROM. The
        # clone is already in the canonical `"${REPO_URL}"` form and is followed
        # by the rev-list marker comment, so neither _standardize_repo_fetch nor
        # _inject_final_sanitize touches this shared base (see the docstring).
        return f"""FROM {base_img}

WORKDIR /home/

{apt_command}

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# History hardening is deferred to the per-PR image, which checks out this PR's
# BASE_COMMIT and ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

CMD ["/bin/bash"]
"""


class CxCoreImageDefault(Image):
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
        return CxCoreImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Both guards run BEFORE the editable install on purpose: `pip install -e .`
# drops a *.egg-info / *.egg-link into the work tree, which would make
# `git status --porcelain` non-empty and fail the check for reasons that have
# nothing to do with a dirty checkout.

# No lockfile in this repo (setuptools + pyproject only), so pip is the package
# manager and `[dev]` is the extra that carries pytest + pytest-asyncio +
# pytest-mock + pytest-timeout -- exactly what .github/workflows/ci.yml installs.
# `|| true` is mandatory: a wheel that has to build from source (cryptography,
# pyyaml) can fail on one architecture, and that must not abort the image build.
python -m pip install --no-cache-dir -e ".[dev]" || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONUNBUFFERED=1
# CI exports the same dummy credentials; several modules refuse to import (or
# take a different code path) when no provider key is present at all.
export ANTHROPIC_API_KEY=test-key-for-ci
export OPENAI_API_KEY=test-key-for-ci

cd /home/{pr.repo}
{test_cmd} 2>&1

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONUNBUFFERED=1
export ANTHROPIC_API_KEY=test-key-for-ci
export OPENAI_API_KEY=test-key-for-ci

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd} 2>&1

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONUNBUFFERED=1
export ANTHROPIC_API_KEY=test-key-for-ci
export OPENAI_API_KEY=test-key-for-ci

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd} 2>&1

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # This image's dependency() is an Image, so DockerfileEnhancer.enhance()
        # returns the text below verbatim -- the checkout and the hardening block
        # are kept exactly as written. The repository itself is already cloned in
        # the `:base` layer; all this image adds is the per-PR checkout. Pinning
        # the base commit here is correct precisely because it is per-PR (tag
        # `pr-<number>`), unlike the shared `:base` image above.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening: the canonical Image._HARDENING_BLOCK
        # (detach at the base commit, drop origin, delete every ref, expire the
        # reflog, gc/repack, drop alternates, assert, then strip submodules).
        # `${BASE_COMMIT}` is left as-is: the ARG declared above carries this
        # PR's sha as its default value.
        hardening = Image._HARDENING_BLOCK

        # Deliberately no trailing `CMD` -- the `:base` image's CMD is inherited.
        tail = f"""
{self.clear_env}
"""
        return header + hardening + tail


@Instance.register("cxlinux-ai", "cx-core")
class CxCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return CxCoreImageDefault(self.pr, self._config)

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
        # ANSI first -- pytest colours PASSED/FAILED when it thinks it has a tty.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `-v` progress line:  tests/test_gpu_manager.py::TestGPUMode::test_x PASSED [ 12%]
        # The captured id is the full `path::Class::test[param]` node id: it carries
        # no timing, no counter and no percentage, so the SAME test yields the SAME
        # name in the run / test / fix stages (required by Report.__post_init__,
        # which unions test names across all three).
        re_progress = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # `-rA` short-summary line:  PASSED tests/test_gpu_manager.py::TestGPUMode::test_x
        # Collection errors are emitted as `ERROR tests/foo.py` (no `::`) and are
        # deliberately not matched -- a file is not a test.
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )

        def record(name: str, status: str) -> None:
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(name)

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_progress.match(line)
            if match:
                record(match.group(1), match.group(2))
                continue

            match = re_summary.match(line)
            if match:
                record(match.group(2), match.group(1))

        # Deduplicate -- worst result wins. A test can legitimately appear twice
        # (once in the progress stream, once in the -rA summary) and a rerun can
        # report it under two different statuses; TestResult.__post_init__ rejects
        # any overlap between the three sets.
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
