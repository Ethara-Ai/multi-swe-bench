import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class KorniaRsImageBase(Image):
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
        return "rust:1.98-bookworm"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CARGO_HOME=/usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup
ENV PATH=/usr/local/cargo/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    git \\
    libgstreamer1.0-dev \\
    libgstreamer-plugins-base1.0-dev \\
    libunwind-dev \\
    nasm \\
    pkg-config \\
    python3 \\
    python3-dev \\
    python3-pip \\
    python3-venv \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class KorniaRsImageDefault(Image):
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
        return KorniaRsImageBase(self.pr, self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --upgrade pip || true
.venv/bin/python -m pip install --no-cache-dir -r kornia-py/requirements-dev.txt || true
.venv/bin/python -m pip install --no-cache-dir "maturin[patchelf]==1.5.1" || true
source /home/{pr.repo}/.venv/bin/activate && maturin develop -m kornia-py/Cargo.toml || true
source /home/{pr.repo}/.venv/bin/activate && python -m pytest kornia-py/tests --collect-only -q -p no:cacheprovider || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
source /home/{pr.repo}/.venv/bin/activate
maturin develop -m kornia-py/Cargo.toml
python -m pytest kornia-py/tests -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
source /home/{pr.repo}/.venv/bin/activate
maturin develop -m kornia-py/Cargo.toml
python -m pytest kornia-py/tests -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
source /home/{pr.repo}/.venv/bin/activate
maturin develop -m kornia-py/Cargo.toml
python -m pytest kornia-py/tests -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("kornia", "kornia-rs")
class KorniaRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KorniaRsImageDefault(self.pr, self._config)

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

        # pytest -v --no-header -rA emits each test twice:
        #   kornia-py/tests/test_io.py::test_decompress PASSED        [ 44%]
        #   PASSED kornia-py/tests/test_io.py::test_decompress
        # Both shapes are matched so a truncated log still parses.
        re_pass = [
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+PASSED"),
            re.compile(r"^PASSED\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+XPASS"),
            re.compile(r"^XPASS\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
        ]
        re_fail = [
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+FAILED"),
            re.compile(r"^FAILED\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+ERROR"),
            re.compile(r"^ERROR\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
        ]
        re_skip = [
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+SKIPPED"),
            re.compile(r"^SKIPPED\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
            re.compile(r"^(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)\s+XFAIL"),
            re.compile(r"^XFAIL\s+(kornia-py/tests/[^\s:]+(?:::[^\s:]+)+)"),
        ]

        for line in test_log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if not line:
                continue

            for re_p in re_pass:
                match = re_p.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_f in re_fail:
                match = re_f.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_s in re_skip:
                match = re_s.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        # R2: the three sets must be disjoint. Failure wins.
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
