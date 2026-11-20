import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "ray-project"
_INTERVAL = "ray_61483_to_60241"
_BASE_TAG = "base-61483_to_60241"
_PY_IMAGE = "python:3.10.19-bookworm"
_PIP_TOOLING = '"pip==25.2" "setuptools==80.9.0" "wheel==0.45.1"'
_WHEEL_ROOT = "https://s3-us-west-2.amazonaws.com/ray-wheels/master"
_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_EXTRA_PACKAGES = "torch polars"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_ID = r"(?:python|rllib)/[^\s:]+\.py::\S.*?"
_STATUS = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_VERBOSE = re.compile(
    rf"^({_TEST_ID})(?: <- \S+)? ({_STATUS})(?: \(.*\))?(?:\s+\[\s*\d+%\])?\s*$",
    re.M,
)
_SUMMARY = re.compile(rf"^({_STATUS}) ({_TEST_ID})(?: - .*)?\s*$", re.M)

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

_TEST_BODY = r"""TEST_FILES=$(grep -E '^\+\+\+ b/(python|rllib)/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)test_[^/]*\.py$' \
    | sort -u || true)

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch exists in this tree"
    exit 0
fi

echo "running pytest on:$TEST_TARGETS"
python -m pytest -v -rfEsxXp --tb=short --color=no \
    -p no:cacheprovider -p no:sugar \
    --import-mode=importlib \
    --continue-on-collection-errors \
    $TEST_TARGETS
"""


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd /home/{repo}", apply)))
    return f"""#!/bin/bash
set -eo pipefail

export CI=true

{setup}

{_TEST_BODY}"""


def _prepare_script(repo: str, sha: str) -> str:
    return rf"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

ray_version=$(sed -n 's/^version = "\(.*\)"$/\1/p' python/ray/_version.py)
test -n "$ray_version"
py_tag=$(python -c 'import sys; print(f"cp{{sys.version_info.major}}{{sys.version_info.minor}}")')
wheel="{_WHEEL_ROOT}/{sha}/ray-${{ray_version}}-${{py_tag}}-${{py_tag}}-manylinux2014_$(uname -m).whl"

python -m pip install --no-cache-dir -U \
    --extra-index-url {_TORCH_INDEX} \
    -c python/requirements.txt \
    -c python/requirements_compiled.txt \
    "ray[air,rllib] @ ${{wheel}}" \
    -r python/requirements/base-test-requirements.txt \
    -r python/requirements/test-requirements.txt \
    {_EXTRA_PACKAGES} || true

extras=$(git ls-tree HEAD python/ray/ \
    | awk '$2 == "tree" || $4 ~ /\.py$/ {{sub("^python/ray/", "", $4); print $4}}' \
    | grep -v -x -E '__init__\.py|_version\.py|setup-dev\.py|core|includes')
python python/ray/setup-dev.py -y --extras $extras

cd /
python - <<'PY'
import os

import numpy
import pandas
import pyarrow
import pytest
import pytest_lazy_fixtures
import pytest_timeout
import ray
import ray._raylet
import ray.data
import ray.job_config
import ray.runtime_env

assert ray.__commit__ == "{sha}", ray.__commit__
for module in (ray.data, ray.job_config, ray.runtime_env):
    path = os.path.realpath(module.__file__)
    assert path.startswith("/home/{repo}/python/ray/"), path
print("ray", ray.__version__, ray.__commit__, "pytest", pytest.__version__)
PY
echo "DEPS_OK"
"""


class Ray61483To60241ImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            clone = f"COPY {repo} /home/{repo}"
        return f"""# syntax=docker/dockerfile:1.6

FROM {_PY_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir {_PIP_TOOLING}

RUN git config --system --add safe.directory '*'

WORKDIR /home/

{clone}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Ray61483To60241ImageDefault(Image):
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
        return Ray61483To60241ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(repo, self.pr.base.sha)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        names = " ".join(file.name for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")
        return f"""FROM {self.dependency().image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

COPY {names} /home/

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}
"""


@Instance.register(_ORG, _INTERVAL)
class RAY_61483_TO_60241(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Ray61483To60241ImageDefault(self.pr, self._config)

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
        test_log = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        matches = _VERBOSE.findall(test_log)
        matches += [(name, status) for status, name in _SUMMARY.findall(test_log)]
        for name, status in matches:
            if status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
