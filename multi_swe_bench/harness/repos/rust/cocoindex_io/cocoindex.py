import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "python:3.11-slim-bookworm"
RUST_IMAGE = "rust:1.90-slim-bookworm"
VENV = "/home/venv"
MATURIN_SPEC = "maturin==1.15.0"
TEST_DEPS = "pytest pytest-asyncio pydantic numpy"
PYTEST_CMD = (
    "python -m pytest python/cocoindex/tests"
    " -v --no-header -rA --tb=short --color=no"
    " -p no:cacheprovider --continue-on-collection-errors"
)

_STATUS = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_VERBOSE_RE = re.compile(rf"^(?P<name>\S.*?)\s+(?P<status>{_STATUS})(?:\s+\[\s*\d+%\])?$")
_SUMMARY_RE = re.compile(rf"^(?P<status>{_STATUS})\s+(?P<name>\S.*)$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _is_test_name(name: str) -> bool:
    return "::" in name or name.endswith(".py")


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

    def dependency(self) -> Union[str, "Image"]:
        return BASE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Level 1: toolchain + source base image, SHARED by every PR.

        This Dockerfile is written out IN FULL, starting with the
        `# syntax=docker/dockerfile:1.6` directive. That directive is the
        documented enhancer opt-out: DockerfileEnhancer.enhance() returns the
        content verbatim the moment it sees it (image.py: `if
        cls.SYNTAX_DIRECTIVE in raw: return raw`).

        Taking the opt-out is what lets a SHARED base clone the repository at
        all. Without it, _standardize_repo_fetch() rewrites the plain `git
        clone ... /home/<repo>` line into the `${REPO_URL}` / `${BASE_COMMIT}`
        form followed by the full Image._HARDENING_BLOCK -- which force-pins
        this one shared image to a single commit and deletes every other ref.
        That is exactly what used to happen here: the base landed on whichever
        PR happened to build first, and the other four then had to `git fetch`
        their own base commit back in prepare.sh to make `git checkout` work.

        So: clone here (once, full history, light hardening only); pin to the
        PR's commit and run the canonical hardening per-PR in ImageDefault's
        prepare.sh.

        The infrastructure block (ARG TARGETARCH / REPO_URL / BASE_COMMIT, the
        proxy ARGs, the ENV block, the ethara LABEL and the cert symlinks) is
        taken straight from DockerfileEnhancer._infrastructure_block rather
        than hand-copied, so it cannot drift out of sync with the reference
        format the enhancer emits for every other image.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            # Keep the bare `git clone "${REPO_URL}" /home/<repo>` form so the
            # reference-format marker still matches. Full history is retained
            # deliberately -- every PR checks out its own commit from it.
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        infra = DockerfileEnhancer._infrastructure_block(self, image_name).rstrip("\n")

        # Light base hardening ONLY: drop the origin remote so the image
        # carries no upstream to re-fetch from, and stop submodule recursion.
        # The canonical Image._HARDENING_BLOCK (detach at ${BASE_COMMIT},
        # delete every ref, expire reflog, gc) deliberately does NOT run here
        # -- this image is shared, so it must retain full history for every
        # PR's checkout.
        light_hardening = (
            "RUN git remote remove origin 2>/dev/null || true; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            "    git config --local gc.auto 0"
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            infra,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates build-essential cmake curl pkg-config libssl-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        sections.append(
            "ENV RUSTUP_HOME=/usr/local/rustup \\\n"
            "    CARGO_HOME=/usr/local/cargo \\\n"
            "    PATH=/usr/local/cargo/bin:$PATH\n"
            f"COPY --from={RUST_IMAGE} /usr/local/rustup /usr/local/rustup\n"
            f"COPY --from={RUST_IMAGE} /usr/local/cargo  /usr/local/cargo"
        )
        sections.append(code)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(light_hardening)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


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

export CI=true
export RUST_BACKTRACE=1
unset COCOINDEX_DATABASE_URL

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

if ! git cat-file -e {pr.base.sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

python -m venv {venv}
. {venv}/bin/activate
pip install --no-cache-dir -U pip || true
pip install --no-cache-dir "{maturin}" || true

cargo fetch --locked || true
git update-index -q --refresh || true
if git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    cargo fetch --locked || true
fi
git checkout -- . || true
git reset --hard
git clean -fd
bash /home/check_git_changes.sh

pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch || true

maturin develop --locked -E dev \\
    || maturin develop --locked -E test \\
    || maturin develop --locked \\
    || true
for attempt in 1 2 3 4 5; do
    pip install --no-cache-dir {test_deps} && break
    echo "prepare: test dependency install failed (attempt $attempt), retrying" >&2
    sleep 10
done

python -c "import pytest, pytest_asyncio, pydantic, numpy"
python -c "import cocoindex"
""".format(
                    pr=self.pr,
                    venv=VENV,
                    maturin=MATURIN_SPEC,
                    test_deps=TEST_DEPS,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
unset COCOINDEX_DATABASE_URL

cd /home/{pr.repo}
. {venv}/bin/activate

maturin develop --locked
{pytest}
""".format(pr=self.pr, venv=VENV, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
unset COCOINDEX_DATABASE_URL

cd /home/{pr.repo}
. {venv}/bin/activate

git update-index -q --refresh || true
if ! git apply --3way --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

maturin develop --locked
{pytest}
""".format(pr=self.pr, venv=VENV, pytest=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
unset COCOINDEX_DATABASE_URL

cd /home/{pr.repo}
. {venv}/bin/activate

git update-index -q --refresh || true
if ! git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

maturin develop --locked
{pytest}
""".format(pr=self.pr, venv=VENV, pytest=PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("cocoindex-io", "cocoindex")
class COCOINDEX(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = _ANSI_RE.sub("", log)

        for raw in log.split("\n"):
            line = raw.strip()
            if not line:
                continue

            match = _SUMMARY_RE.match(line)
            if match:
                name = match.group("name").split(" - ", 1)[0].strip()
            else:
                match = _VERBOSE_RE.match(line)
                if not match:
                    continue
                name = match.group("name").strip()

            if not _is_test_name(name):
                continue

            status = match.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
