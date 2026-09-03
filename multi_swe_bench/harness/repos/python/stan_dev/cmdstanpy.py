import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

TEST_USER = "mswb"


class CmdStanPyImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.12-slim-bookworm"

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
ENV CMDSTAN_VERSION=2.36.0
ENV CMDSTAN=/opt/cmdstan/cmdstan-2.36.0
ENV TEST_USER={TEST_USER}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    make \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN python -m pip install --no-cache-dir \\
    "numpy>=1.21" \\
    "pandas" \\
    "tqdm" \\
    "stanio>=0.4.0,<2.0.0" \\
    "xarray" \\
    "polars>=1.8.2" \\
    "pytest" \\
    "pytest-order" \\
    "pytest-cov"

RUN useradd --create-home --shell /bin/bash --uid 1000 "${{TEST_USER}}"

RUN mkdir -p /opt/cmdstan \\
    && curl -fsSL -o /tmp/cmdstan.tgz \\
        "https://github.com/stan-dev/cmdstan/releases/download/v${{CMDSTAN_VERSION}}/cmdstan-${{CMDSTAN_VERSION}}.tar.gz" \\
    && tar -xzf /tmp/cmdstan.tgz -C /opt/cmdstan \\
    && rm -f /tmp/cmdstan.tgz \\
    && make -C "${{CMDSTAN}}" build -j$(nproc) \\
    && arch="${{TARGETARCH:-}}" \\
    && if [ -z "$arch" ]; then \\
        case "$(uname -m)" in aarch64|arm64) arch=arm64 ;; *) arch=amd64 ;; esac; \\
    fi \\
    && if [ "$arch" = "arm64" ]; then \\
        curl -fsSL -o "${{CMDSTAN}}/bin/stanc" \\
            "https://github.com/stan-dev/stanc3/releases/download/v${{CMDSTAN_VERSION}}/linux-arm64-stanc" \\
        && chmod +x "${{CMDSTAN}}/bin/stanc"; \\
    fi \\
    && expected=$(if [ "$arch" = "arm64" ]; then echo 183; else echo 62; fi) \\
    && actual=$(od -An -tu2 -j18 -N2 "${{CMDSTAN}}/bin/stanc" | tr -d " ") \\
    && if [ "$actual" != "$expected" ]; then \\
        echo "stanc ELF e_machine=$actual, expected $expected for $arch" >&2; exit 1; \\
    fi \\
    && chown -R "${{TEST_USER}}:${{TEST_USER}}" /opt/cmdstan

{code}

{self.clear_env}

"""


class CmdStanPyImageDefault(Image):
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
        return CmdStanPyImageBase(self.pr, self._config)

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
  echo "check_git_changes: Not inside a work tree"
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
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --no-deps -e . || true

python --version
python -c "import cmdstanpy; print(cmdstanpy.__version__, cmdstanpy.cmdstan_path())"

chown -R {test_user}:{test_user} /home/{pr.repo}

chmod 1777 /home

""".format(pr=self.pr, test_user=TEST_USER),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
runuser -u {test_user} -- env HOME=/home/{test_user} CI=true CMDSTAN="$CMDSTAN" \\
    python -m pytest test -v --no-header -rA --tb=no -p no:cacheprovider \\
    --continue-on-collection-errors \\
    --ignore=test/test_install_cmdstan.py

""".format(pr=self.pr, test_user=TEST_USER),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
if ! runuser -u {test_user} -- git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
runuser -u {test_user} -- env HOME=/home/{test_user} CI=true CMDSTAN="$CMDSTAN" \\
    python -m pytest test -v --no-header -rA --tb=no -p no:cacheprovider \\
    --continue-on-collection-errors \\
    --ignore=test/test_install_cmdstan.py

""".format(pr=self.pr, test_user=TEST_USER),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
if ! runuser -u {test_user} -- git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
runuser -u {test_user} -- env HOME=/home/{test_user} CI=true CMDSTAN="$CMDSTAN" \\
    python -m pytest test -v --no-header -rA --tb=no -p no:cacheprovider \\
    --continue-on-collection-errors \\
    --ignore=test/test_install_cmdstan.py

""".format(pr=self.pr, test_user=TEST_USER),
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


@Instance.register("stan-dev", "cmdstanpy")
class CmdStanPy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CmdStanPyImageDefault(self.pr, self._config)

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

        status = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        re_inline = re.compile(rf"^(test/.+?)\s+({status})\b")
        re_summary = re.compile(rf"^({status})\s+(test/.+?)(?:\s+-\s.*)?$")

        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        for line in test_log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line).strip()
            if not line:
                continue

            match = re_inline.match(line)
            if match:
                buckets[match.group(2)].add(match.group(1))
                continue

            match = re_summary.match(line)
            if match:
                buckets[match.group(1)].add(match.group(2))

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
