import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "python:3.10-slim-bullseye"
# The Rust toolchain is lifted out of the official image rather than fetched
# with `curl | sh`: the version is pinned by the tag, nothing is piped into a
# shell, and it is not a network RUN. rust:1.65 is contemporary with this PR's
# 2022-10 base commit (Cargo.toml declares rust-version = "1.57" as the floor).
# The tag is multi-arch, so the builder pulls the layer matching TARGETARCH.
RUST_IMAGE = "rust:1.65-slim-bullseye"
VENV = "/home/venv"
PYTEST_CMD = (
    "RUST_BACKTRACE=1 python -m pytest -v -rA --color=no -p no:randomly"
    " --continue-on-collection-errors datafusion/tests"
)


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
        return f"""FROM {self.dependency()}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential cmake curl pkg-config libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH
COPY --from={RUST_IMAGE} /usr/local/rustup /usr/local/rustup
COPY --from={RUST_IMAGE} /usr/local/cargo  /usr/local/cargo

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

git submodule update --init --recursive || true
git submodule foreach --recursive '
    git checkout --detach HEAD 2>/dev/null || true
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d || true
    git reflog expire --expire=now --all || true
    git reflog expire --expire-unreachable=now --all || true
    git gc --prune=now --aggressive || true
    rm -f .git/objects/info/alternates || true
' || true

python -m venv {venv}
. {venv}/bin/activate
pip install --no-cache-dir -U pip || true
pip install --no-cache-dir -r requirements-310.txt || true

cargo fetch --locked || true

if git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    cargo fetch --locked || true
fi
git checkout -- . || true
git reset --hard
git clean -fd
bash /home/check_git_changes.sh

maturin develop --locked || true
""".format(pr=self.pr, venv=VENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

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

cd /home/{pr.repo}
. {venv}/bin/activate

if ! git apply --whitespace=nowarn /home/test.patch; then
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

cd /home/{pr.repo}
. {venv}/bin/activate

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
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


@Instance.register("apache", "datafusion-python")
class DATAFUSION_PYTHON(Instance):
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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # pytest -rA emits skip/xfail summaries as "SKIPPED [1] file.py:76: reason".
        # The "[1]" is a COUNT, not a test id -- the real test is captured from its
        # verbose line -- so reject a name that starts with "[".
        summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?!\[)(\S+)"
        )
        verbose_re = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        for raw in log.split("\n"):
            line = raw.strip()

            m = summary_re.match(line)
            if m:
                status, name = m.group(1), m.group(2)
            else:
                m = verbose_re.match(line)
                if not m:
                    continue
                name, status = m.group(1), m.group(2)

            name = name.rstrip(":")
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
