import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "solana-py"


class SolanaPyImageBase(Image):

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
        if self._pr.number <= 232:
            return "python:3.8-slim"
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def _era(self) -> str:
        if self._pr.number <= 114:
            return "py38-era1"
        if self._pr.number <= 232:
            return "py38-era2"
        return "py39-era3"

    def image_tag(self) -> str:
        return f"base-{self._era()}"

    def workdir(self) -> str:
        return f"base-{self._era()}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        extra_apt = SolanaPyImageDefault(self.pr, self.config)._extra_apt_packages()
        default_packages = "ca-certificates curl git gnupg make python3 sudo wget"
        packages_str = (
            f"{default_packages} {extra_apt}" if extra_apt else default_packages
        )

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{REPO_DIR}'
        else:
            code = f"COPY {repo} /home/{REPO_DIR}"

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {packages_str} \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{REPO_DIR}

CMD ["/bin/bash"]
"""


class SolanaPyImageDefault(Image):
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
        return SolanaPyImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _extra_apt_packages(self) -> str:
        if self._pr.number <= 114:
            return ""
        elif self._pr.number <= 232:
            return "build-essential libffi-dev libsodium-dev"
        else:
            return "build-essential"

    def _install_commands(self) -> str:
        if self._pr.number <= 114:
            cmd = 'pip install --upgrade pip setuptools wheel\npip install -e ".[dev]"'
            if self._pr.number == 105:
                cmd += "\npip install cachetools"
            return cmd
        else:
            return (
                "pip install --upgrade pip setuptools wheel\n"
                "pip install poetry\n"
                "poetry config virtualenvs.create false\n"
                "poetry lock\n"
                "poetry install"
            )

    def _test_command(self) -> str:
        if self._pr.number <= 114:
            return (
                'PYTHONPATH=./solana pytest -v -m "not integration" '
                '--no-header -rA --tb=no -p no:cacheprovider -o addopts=""'
            )
        elif self._pr.number <= 232:
            return (
                'pytest -v -m "not integration" '
                "--no-header -rA --tb=no -p no:cacheprovider "
                "--ignore=src --ignore=tests/integration"
            )
        else:
            return (
                'pytest -v -m "not integration" '
                "--no-header -rA --tb=no -p no:cacheprovider "
                "-p no:anyio --ignore=src --ignore=tests/integration"
            )

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}


RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}
"""

    def files(self) -> list[File]:
        test_cmd = self._test_command()
        install_sh = self._install_commands()
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
                f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
{install_sh}
{test_cmd}
echo '{test_cmd}' > test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_cmd}
""",
            ),
        ]


@Instance.register("michaelhly", "solana-py")
class SolanaPy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SolanaPyImageDefault(self.pr, self._config)

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
        test_results = {}

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        passed_pattern = re.compile(r"^PASSED\s+(\S+)")
        failed_pattern = re.compile(r"^FAILED\s+(\S+)")
        error_pattern = re.compile(r"^ERROR\s+(\S+)")
        skipped_pattern = re.compile(r"^SKIPPED\s+(\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            match = failed_pattern.match(line) or error_pattern.match(line)
            if match:
                test_results[match.group(1)] = "failed"
                continue

            match = skipped_pattern.match(line)
            if match:
                test_name = match.group(1)
                if test_results.get(test_name) != "failed":
                    test_results[test_name] = "skipped"
                continue

            match = passed_pattern.match(line)
            if match:
                test_name = match.group(1)
                if test_results.get(test_name) not in ("failed", "skipped"):
                    test_results[test_name] = "passed"
                continue

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
