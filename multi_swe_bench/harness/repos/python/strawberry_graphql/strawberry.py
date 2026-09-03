"""strawberry-graphql/strawberry config.

A pure-python GraphQL library laid out as a single top-level `strawberry`
package with its suite in `tests/`, plus per-integration subdirectories
(tests/asgi, tests/django, tests/flask, tests/sanic, tests/mypy).

Covers the five PRs in input/strawberry-graphql__strawberry_raw_dataset.jsonl
-- 289, 555, 659, 712 and 900. None of them carries a `tag` or a
`number_interval`, so Instance.create() looks every one of them up under the
single key "strawberry-graphql/strawberry" registered at the bottom of this
file.

The repo is poetry-managed, and its build-system at these commits asks for
`poetry>=0.12` with the `poetry.masonry.api` backend, so `pip install -e .`
would first have to install poetry itself. Nothing here needs the package
installed: the checkout is put on the interpreter path with a .pth file (see
prepare.sh) and the dependencies are pip-installed from the pinned lists below.
A .pth rather than PYTHONPATH because pytest-mypy-plugins shells mypy out with
its own environment -- under PYTHONPATH alone every tests/mypy/*.yml case fails
with "Error importing plugin 'strawberry.ext.mypy_plugin'".

The pins are per-era. These five PRs span a year of the project (v0.21.1 in
April 2020 to v0.57.4 in May 2021) and the dependency set moves underneath
them: pytest 5 -> 6, starlette 0.13.0 -> 0.14.2, and pydantic, cached-property,
python-multipart and sanic each arrive partway through. Each list below is the
era's own pyproject.toml resolved to concrete versions, and each was verified
to give a clean baseline -- 0 failed, 0 collection errors -- at that PR's base
commit.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_COMMON = [
    "click==7.1.2",
    "hupper==1.10.3",
    "pygments==2.7.4",
    "itsdangerous==1.1.0",
    "Jinja2==2.11.3",
    "MarkupSafe==1.1.1",
    "Werkzeug==1.0.1",
    "requests==2.25.1",
]

_ERA_0_21 = [
    "graphql-core==3.1.0",
    "starlette==0.13.0",
    "uvicorn==0.11.2",
    "django==3.0.14",
    "asgiref==3.2.10",
    "flask==1.1.4",
    "six==1.16.0",
    "pytest==5.4.3",
    "pytest-asyncio==0.10.0",
    "pytest-mock==2.0.0",
    "pytest-cov==2.10.1",
    "pytest-emoji==0.2.0",
    "pytest-django==3.10.0",
    "pytest-flask==0.15.1",
    "pytest-mypy-plugins==1.2.1",
    "mypy==0.761",
] + _COMMON

_ERA_0_40 = [
    "graphql-core==3.1.3",
    "starlette==0.13.8",
    "uvicorn==0.13.4",
    "django==3.1.14",
    "asgiref==3.3.4",
    "flask==1.1.4",
    "typing_extensions==3.7.4.3",
    "python-dateutil==2.8.2",
    "opentelemetry-api==0.13b0",
    "opentelemetry-sdk==0.13b0",
    "pytest==6.1.2",
    "pytest-asyncio==0.14.0",
    "pytest-mock==3.3.1",
    "pytest-cov==2.10.1",
    "pytest-emoji==0.2.0",
    "pytest-django==4.1.0",
    "pytest-flask==1.1.0",
    "pytest-benchmark==3.2.3",
    "pytest-mypy-plugins==1.6.1",
    "mypy==0.790",
    "freezegun==1.0.0",
] + _COMMON

_ERA_0_44 = [
    "graphql-core==3.1.3",
    "starlette==0.14.1",
    "uvicorn==0.13.3",
    "django==3.1.14",
    "asgiref==3.3.4",
    "flask==1.1.4",
    "typing_extensions==3.7.4.3",
    "python-dateutil==2.8.2",
    "cached-property==1.5.2",
    "pydantic==1.7.4",
    "email-validator==1.1.2",
    "python-multipart==0.0.5",
    "opentelemetry-api==0.13b0",
    "opentelemetry-sdk==0.13b0",
    "pytest==6.2.5",
    "pytest-asyncio==0.14.0",
    "pytest-mock==3.5.1",
    "pytest-cov==2.11.1",
    "pytest-emoji==0.2.0",
    "pytest-django==4.1.0",
    "pytest-flask==1.1.0",
    "pytest-benchmark==3.2.3",
    "pytest-mypy-plugins==1.6.1",
    "mypy==0.790",
    "freezegun==1.0.0",
] + _COMMON

_ERA_0_45 = [
    "graphql-core==3.1.3",
    "starlette==0.14.2",
    "uvicorn==0.13.4",
    "django==3.1.14",
    "asgiref==3.3.4",
    "flask==1.1.4",
    "typing_extensions==3.7.4.3",
    "python-dateutil==2.8.2",
    "cached-property==1.5.2",
    "pydantic==1.8.2",
    "email-validator==1.1.3",
    "python-multipart==0.0.5",
    "opentelemetry-api==0.17b0",
    "opentelemetry-sdk==0.17b0",
    "pytest==6.2.5",
    "pytest-asyncio==0.14.0",
    "pytest-mock==3.5.1",
    "pytest-cov==2.11.1",
    "pytest-emoji==0.2.0",
    "pytest-django==4.1.0",
    "pytest-flask==1.1.0",
    "pytest-benchmark==3.2.3",
    "pytest-mypy-plugins==1.6.1",
    "mypy==0.812",
    "freezegun==1.1.0",
] + _COMMON

_ERA_0_57 = [
    "graphql-core==3.1.3",
    "starlette==0.14.2",
    "uvicorn==0.13.4",
    "django==3.1.14",
    "asgiref==3.3.4",
    "flask==1.1.4",
    "typing_extensions==3.7.4.3",
    "python-dateutil==2.8.2",
    "cached-property==1.5.2",
    "pydantic==1.8.2",
    "email-validator==1.1.3",
    "python-multipart==0.0.5",
    "opentelemetry-api==0.17b0",
    "opentelemetry-sdk==0.17b0",
    "sanic==20.12.7",
    "pytest==6.2.5",
    "pytest-asyncio==0.15.1",
    "pytest-mock==3.5.1",
    "pytest-cov==2.11.1",
    "pytest-emoji==0.2.0",
    "pytest-django==4.2.0",
    "pytest-flask==1.2.0",
    "pytest-benchmark==3.4.1",
    "pytest-mypy-plugins==1.6.1",
    "mypy==0.812",
    "freezegun==1.1.0",
] + _COMMON


def _pins(number: int) -> list[str]:
    """The dependency set for the era a PR number falls in.

    Thresholds sit between the five PRs in the dataset rather than on them, so
    a neighbouring PR pulled in later lands on the era whose pyproject.toml it
    actually shares.
    """
    if number <= 400:
        return _ERA_0_21
    if number <= 600:
        return _ERA_0_40
    if number <= 680:
        return _ERA_0_44
    if number <= 800:
        return _ERA_0_45
    return _ERA_0_57


_RUN_TESTS = """
ADDOPTS=""
for ini in mypy_tests.ini tests/mypy/mypy.ini tests/mypy/config.cfg; do
    if [ -f "$ini" ]; then
        ADDOPTS="--mypy-ini-file=$ini"
        break
    fi
done
if [ -d tests/benchmarks ]; then
    ADDOPTS="$ADDOPTS --benchmark-disable"
fi

python -m pytest tests/ -o addopts="$ADDOPTS" -v -rA --tb=no \\
    -p no:cacheprovider --continue-on-collection-errors
"""


class StrawberryImageBase(Image):
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
        return "python:3.8-slim-bookworm"

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

{self.clear_env}

"""


class StrawberryImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return StrawberryImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pins = " \\\n    ".join(f'"{pin}"' for pin in _pins(self.pr.number))

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# In dataset mode the base image is already detached at the base commit with
# its history pruned and its remote removed, so cat-file short-circuits and
# nothing is fetched. Outside that mode the clone sits on the default branch
# and the base commit has to be pulled down before it can be checked out.
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --upgrade pip setuptools wheel

python -m pip install --no-cache-dir \\
    {pins}

# Put the checkout on the path for every interpreter in the image, including
# the one pytest-mypy-plugins spawns for mypy. Written after the checkout so
# a broken clone fails earlier, at the git step, than at import time.
python -c "import site; f = open(site.getsitepackages()[0] + '/_strawberry_src.pth', 'w'); f.write('/home/{pr.repo}')"

python --version
python -c "import strawberry, graphql; print(strawberry.__file__); print(graphql.version)"

""".format(pr=self.pr, pins=pins),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{run_tests}
""".format(pr=self.pr, run_tests=_RUN_TESTS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
{run_tests}
""".format(pr=self.pr, run_tests=_RUN_TESTS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{run_tests}
""".format(pr=self.pr, run_tests=_RUN_TESTS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("StrawberryImageDefault dependency must be an Image")
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


@Instance.register("strawberry-graphql", "strawberry")
class Strawberry(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StrawberryImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        status = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        re_inline = re.compile(rf"^(tests/.+?)\s+({status})\b")
        re_summary = re.compile(rf"^({status})\s+(tests/\S+)")

        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        for line in clean_log.split("\n"):
            line = line.strip()

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
