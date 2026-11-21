from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.11.7-slim-bookworm"
_UV_VERSION = "0.8.22"

_ENVIRONMENTS = [
    (
        8627,
        {
            "exclude_newer": "2024-02-14T00:00:00Z",
            "requirements": [
                "numpy",
                "pandas",
                "setuptools",
                "wheel",
                "setuptools-scm",
                "packaging",
                "pytest",
                "dask[array,dataframe]",
                "toolz",
                "scipy",
                "cftime",
                "bottleneck",
                "matplotlib",
                "pint",
                "flox",
                "numexpr",
                "pyarrow",
                "hypothesis",
                "sparse",
                "numbagg",
            ],
            "pins": {
                "setuptools": "69.1.0",
                "wheel": "0.42.0",
                "setuptools-scm": "8.0.4",
                "packaging": "23.2",
                "numpy": "1.26.4",
                "pandas": "2.2.0",
                "pytest": "8.0.0",
                "dask": "2024.2.0",
                "toolz": "0.12.1",
                "scipy": "1.12.0",
                "cftime": "1.6.3",
                "bottleneck": "1.3.7",
                "matplotlib": "3.8.2",
                "pint": "0.23",
                "flox": "0.9.2",
                "numexpr": "2.9.0",
                "pyarrow": "15.0.0",
                "hypothesis": "6.98.4",
                "sparse": "0.15.1",
                "numba": "0.59.0",
                "numbagg": "0.8.0",
            },
            "imports": [
                "dask.array",
                "dask.dataframe",
                "scipy",
                "cftime",
                "bottleneck",
                "matplotlib",
                "pint",
                "flox",
                "numexpr",
                "pyarrow",
                "hypothesis",
                "sparse",
                "numbagg",
            ],
        },
    ),
    (
        None,
        {
            "exclude_newer": "2024-04-17T00:00:00Z",
            "requirements": [
                "numpy",
                "pandas",
                "setuptools",
                "wheel",
                "setuptools-scm",
                "packaging",
                "pytest",
                "dask[array,dataframe]",
                "toolz",
                "scipy",
                "cftime",
                "bottleneck",
                "matplotlib",
                "pint",
                "flox",
                "numexpr",
                "pyarrow",
                "hypothesis",
                "sparse",
                "numbagg",
            ],
            "pins": {
                "setuptools": "69.5.1",
                "wheel": "0.42.0",
                "setuptools-scm": "8.0.4",
                "packaging": "24.0",
                "numpy": "1.26.4",
                "pandas": "2.2.2",
                "pytest": "8.1.1",
                "dask": "2024.4.1",
                "toolz": "0.12.1",
                "scipy": "1.13.0",
                "cftime": "1.6.3",
                "bottleneck": "1.3.8",
                "matplotlib": "3.8.4",
                "pint": "0.23",
                "flox": "0.9.6",
                "numexpr": "2.10.0",
                "pyarrow": "15.0.2",
                "hypothesis": "6.100.1",
                "sparse": "0.15.1",
                "numba": "0.59.1",
                "numbagg": "0.8.1",
            },
            "imports": [
                "dask.array",
                "dask.dataframe",
                "scipy",
                "cftime",
                "bottleneck",
                "matplotlib",
                "pint",
                "flox",
                "numexpr",
                "pyarrow",
                "hypothesis",
                "sparse",
                "numbagg",
            ],
        },
    ),
]

_INSTALL_SH = """python -m venv /home/tools
/home/tools/bin/pip install --no-cache-dir "uv==__UV_VERSION__"
python -m venv /home/venv

uv_install=(/home/tools/bin/uv pip install --no-cache --python /home/venv/bin/python \\
    --exclude-newer "__EXCLUDE_NEWER__" -c /home/constraints.txt)
"${uv_install[@]}" -r /home/requirements-test.txt
"${uv_install[@]}" --no-deps --no-build-isolation -e .
"""

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_TEST_PREFIX = r"(?:xarray|properties)/"
_TRAILING_RE = re.compile(
    r"^(?P<name>" + _TEST_PREFIX + r"\S+?)\s+(?P<status>" + "|".join(_STATUSES) + r")\b"
)
_LEADING_RE = re.compile(
    r"^(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?(?P<status>"
    + "|".join(_STATUSES)
    + r")\s+(?P<name>"
    + _TEST_PREFIX
    + r"\S+)"
)
_SKIP_SUMMARY_RE = re.compile(
    r"^SKIPPED \[\d+\] (?P<name>" + _TEST_PREFIX + r"[^:\s]+:\d+)"
)


def _environment(number: int) -> dict:
    for max_number, env in _ENVIRONMENTS:
        if max_number is None or number <= max_number:
            return env
    raise ValueError(f"no environment for PR {number}")


def _constraints(env: dict) -> str:
    return "".join(f"{name}=={version}\n" for name, version in env["pins"].items())


def _requirements(names: list[str]) -> str:
    return "".join(f"{name}\n" for name in names)


def _gate(env: dict) -> str:
    modules = ["xarray", "numpy", "pandas", "pytest"] + list(env["imports"])
    imports = "; ".join(f"import {module}" for module in modules)
    numpy_pin = env["pins"]["numpy"]
    pandas_pin = env["pins"]["pandas"]
    checks = (
        f"assert numpy.__version__ == {numpy_pin!r}, numpy.__version__; "
        f"assert pandas.__version__ == {pandas_pin!r}, pandas.__version__; "
        "assert xarray.__file__.startswith('/home/xarray/'), xarray.__file__; "
        "print(xarray.__version__, numpy.__version__, pandas.__version__)"
    )
    return f'/home/venv/bin/python -c "{imports}; {checks}"\n/home/venv/bin/python -m pytest --version\n'


def _test_files(test_patch: str) -> list[str]:
    files = []
    for path in _DIFF_HEADER_RE.findall(test_patch):
        name = path.rsplit("/", 1)[-1]
        if re.match(_TEST_PREFIX, path) and name.startswith("test_") and name.endswith(".py"):
            if path not in files:
                files.append(path)
    return files


def _test_command(test_patch: str) -> str:
    modules = " ".join(shlex.quote(path) for path in _test_files(test_patch))
    return f"""files=()
for module in {modules}; do
    if [ -f "$module" ]; then
        files+=("$module")
    fi
done
if [ ${{#files[@]}} -eq 0 ]; then
    echo "no test modules to run"
    exit 0
fi
python -m pytest -p no:cacheprovider -rA -v "${{files[@]}}"
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=dirty) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__INSTALL__
__GATE__"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export MPLBACKEND=Agg
export PYTHONDONTWRITEBYTECODE=1
export PATH="/home/venv/bin:$PATH"

cd /home/__REPO__
"""


class Xarray8723To8627ImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        interval = self.pr.number_interval.removeprefix(f"{self.pr.repo}_")
        return f"base-{interval}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {self.pr.number_interval} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential pkg-config ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Xarray8723To8627ImageDefault(Image):
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
        return Xarray8723To8627ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        env = _environment(self.pr.number)
        return (
            template.replace("__INSTALL__", _INSTALL_SH)
            .replace("__GATE__", _gate(env))
            .replace("__UV_VERSION__", _UV_VERSION)
            .replace("__EXCLUDE_NEWER__", env["exclude_newer"])
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        env = _environment(self.pr.number)
        header = self._script(_HEADER_SH)
        test_cmd = _test_command(self.pr.test_patch)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "constraints.txt", _constraints(env)),
            File(".", "requirements-test.txt", _requirements(env["requirements"])),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{test_cmd}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{test_cmd}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{test_cmd}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    test_log = _ANSI_RE.sub("", test_log)
    for raw in test_log.splitlines():
        line = raw.strip()
        status = None
        name = None
        match = _TRAILING_RE.match(line) or _LEADING_RE.match(line)
        if match:
            status = match.group("status")
            name = match.group("name")
        else:
            skip = _SKIP_SUMMARY_RE.match(line)
            if skip:
                status = "SKIPPED"
                name = skip.group("name")
        if not name or ("::" not in name and status != "SKIPPED"):
            continue
        if status in ("PASSED", "XFAIL", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
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


@Instance.register("pydata", "xarray_8723_to_8627")
class XARRAY_8723_TO_8627(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Xarray8723To8627ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)
