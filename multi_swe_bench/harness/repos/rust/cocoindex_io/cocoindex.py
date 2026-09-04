import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
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
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential cmake curl pkg-config libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH
COPY --from={RUST_IMAGE} /usr/local/rustup /usr/local/rustup
COPY --from={RUST_IMAGE} /usr/local/cargo  /usr/local/cargo

{code}

{self.clear_env}

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
