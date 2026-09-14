from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "7315_to_5900"
_CONDA_IMAGE = "continuumio/miniconda3:24.9.2-0"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NODE_ID_RE = re.compile(r"^[^\s:]+\.py(::\S*)?$")
_VERBOSE_RE = re.compile(r"^(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_SUMMARY_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)")

_DERIVE_IGNORES = """IGN="$(sed -n '/^test:/,/^$/p' Makefile \\
  | grep -oE '(exclude-dir|ignore)="[^"]+"' \\
  | sed -E 's/^[a-z-]+="//; s/"$//; s#/\\*$##; s#/$##; s#^#--ignore=#' \\
  | tr '\\n' ' ')"
test -n "$IGN" || { echo "could not derive the ignore list from the Makefile test target"; exit 1; }"""

_TEST_CMD = (
    "python -X faulthandler -m pytest -v -rA --color=no -p no:cacheprovider "
    "--continue-on-collection-errors --timeout=120 --timeout-method=signal $IGN test"
)


class HummingbotImageBase_HUMMINGBOT_7315_TO_5900(Image):
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
        return _CONDA_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential libusb-1.0-0 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class HummingbotImageDefault_HUMMINGBOT_7315_TO_5900(Image):
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
        return HummingbotImageBase_HUMMINGBOT_7315_TO_5900(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

source /opt/conda/etc/profile.d/conda.sh
conda --version
export PIP_DEFAULT_TIMEOUT=120
cp setup/environment.yml /tmp/environment.yml
if grep -qE '^\\s*-\\s*cython==3\\.0a7\\s*$' /tmp/environment.yml; then
  sed -i -E 's|^\\s*-\\s*cython==3\\.0a7\\s*$|    - cython==0.29.15|' /tmp/environment.yml
fi
if grep -qE '^\\s*-\\s*cython==3\\.0\\.0a10\\s*$' /tmp/environment.yml; then
  sed -i -E '/^\\s*-\\s*cython==3\\.0\\.0a10\\s*$/d; s|^dependencies:\\s*$|dependencies:\\n  - conda-forge/label/cython_dev::cython=3.0.0a10|' /tmp/environment.yml
fi
if grep -qE '^\\s*-\\s*pandas_ta==0\\.3\\.14b\\s*$' /tmp/environment.yml; then
  sed -i -E '/^\\s*-\\s*pandas_ta==0\\.3\\.14b\\s*$/d; s|^dependencies:\\s*$|dependencies:\\n  - pandas-ta=0.3.14b|' /tmp/environment.yml
fi
if grep -qE '^\\s*-\\s*cytoolz==0\\.11\\.0\\s*$' /tmp/environment.yml; then
  sed -i -E '/^\\s*-\\s*cytoolz==0\\.11\\.0\\s*$/d; s|^dependencies:\\s*$|dependencies:\\n  - cytoolz=0.11.0|' /tmp/environment.yml
fi
if grep -qE '^\\s*-\\s*aiohttp==3\\.\\*\\s*$' /tmp/environment.yml; then
  sed -i -E 's|^\\s*-\\s*aiohttp==3\\.\\*\\s*$|    - aiohttp>=3.8,<3.14|' /tmp/environment.yml
fi
sed -i -E 's|^dependencies:\\s*$|dependencies:\\n  - setuptools<81|' /tmp/environment.yml
conda env create -f /tmp/environment.yml || true
test -x /opt/conda/envs/hummingbot/bin/python || {{ echo "prepare.sh: conda env create produced no hummingbot env"; exit 1; }}
conda activate hummingbot
python --version
python -m pytest --version || {{ echo "prepare.sh: pytest is not installed in the hummingbot env"; exit 1; }}
python -c "import aiohttp, aioresponses" || {{ echo "prepare.sh: aiohttp or aioresponses is missing from the hummingbot env"; exit 1; }}
python -c "import pandas_ta" || {{ echo "prepare.sh: pandas_ta is missing from the hummingbot env"; exit 1; }}
PYTEST_MAJOR=$(python -c "import pytest; print(pytest.__version__.split('.')[0])")
if [ "$PYTEST_MAJOR" -lt 5 ]; then
  python -m pip install --no-cache-dir --no-deps "pytest-timeout==1.4.2" || true
else
  python -m pip install --no-cache-dir --no-deps "pytest-timeout==2.4.0" || true
fi
python -c "import pytest_timeout" || {{ echo "prepare.sh: pytest-timeout is missing from the hummingbot env"; exit 1; }}
python setup.py build_ext --inplace -j 8 || {{ echo "prepare.sh: Cython build_ext failed at the base commit"; exit 1; }}
python -c "import hummingbot.core.clock" || {{ echo "prepare.sh: compiled module hummingbot.core.clock does not import"; exit 1; }}
rm -rf build
conda clean -afy
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
{_DERIVE_IGNORES}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
git apply --whitespace=nowarn /home/test.patch
{_DERIVE_IGNORES}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_DERIVE_IGNORES}
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "HummingbotImageDefault_HUMMINGBOT_7315_TO_5900 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("hummingbot", "hummingbot_7315_to_5900")
class HUMMINGBOT_7315_TO_5900(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return HummingbotImageDefault_HUMMINGBOT_7315_TO_5900(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            name = status = None
            m = _VERBOSE_RE.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name or not _NODE_ID_RE.match(name):
                continue
            if status == "PASSED":
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
