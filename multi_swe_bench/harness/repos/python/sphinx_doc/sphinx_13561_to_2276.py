import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_PY_IMAGE = "python:3.12.7-bookworm"
_UV_VERSION = "0.13.8"
_VENV = "/opt/mswe/venv"
_MIN_PY = 8
_TESTS = "tests"
_EXTRAS = "test"
_UNDATED_CUTOFF = "2024-01-01T00:00:00Z"
_LEGACY_TOOLS = '"setuptools<46" "wheel" "pip<24"'
_WHEEL_DIR = "/tmp/mswe-wheel"
_APT_PACKAGES = ("git", "ca-certificates", "build-essential")
_PYPROJECT = "pyproject.toml"
_SETUP = "setup.py"
_NOSE_ERA = f"grep -qs nose {_TESTS}/run.py"
_PYTEST = f"python -m pytest -rA -v -p no:cacheprovider --continue-on-collection-errors {_TESTS}"
_NOSE = f"python {_TESTS}/run.py -v"
_PYTEST_COLLECT = f'{{ python -m pytest --collect-only -q -p no:cacheprovider --continue-on-collection-errors {_TESTS} || true; }} | grep -c "::"'
_NOSE_COLLECT = rf'python {_TESTS}/run.py --collect-only -v 2>&1 | grep -c " \.\.\. ok$"'
_PYTHON_MINOR = f"""python_minor() {{
    spec=$(cat {_PYPROJECT} {_SETUP} 2>/dev/null | grep -oE "(requires-python|python_requires) *= *[\\"'][^\\"']+" || true)
    minor=$(echo "$spec" | grep -oE ">= *3\\.[0-9]+" | grep -oE "[0-9]+$" | sort -n | tail -n 1 || true)
    minor=${{minor:-0}}
    echo $(( minor > {_MIN_PY} ? minor : {_MIN_PY} ))
}}"""
_FIX_PYTHON = r"""fix_py=$(grep -hE "^\+.*(requires-python|python_requires)" /home/fix.patch /home/test.patch | grep -oE ">=\s*3\.[0-9]+" | grep -oE "[0-9]+$" | sort -n | tail -n 1 || true)"""
_BINARY_EXCLUDES = r"""excludes=$(sed -nE 's#^Binary files (a/\S+|/dev/null) and b/(\S+) differ$#--exclude=\2#p; s#^Binary files a/(\S+) and /dev/null differ$#--exclude=\1#p' /home/test.patch /home/fix.patch)"""

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NODE = rf"({_TESTS}/[^\s\[]+(?:\[.*?\](?=\s|$))?)"
_PASSED = re.compile(rf"^(?:PASSED|XPASS) {_NODE}", re.M)
_FAILED = re.compile(rf"^(?:FAILED|ERROR) {_NODE}", re.M)
_SKIPPED = re.compile(rf"^XFAIL {_NODE}", re.M)
_VERBOSE = re.compile(rf"^({_TESTS}/\S.*?) (PASSED|XPASS|FAILED|ERROR|XFAIL)(?: +\[ *\d+%\])?$", re.M)
_NOSE_RESULT = re.compile(r"^(\S.*?) \.\.\. (ok|FAIL|ERROR|SKIP|skipped)\b", re.M)
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")

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


def _sections(*parts: str) -> str:
    return "\n\n".join(filter(None, parts)) + "\n"


_PYTHON_ENV = "\n".join(
    (
        "export CI=true",
        "export GIT_AUTHOR_NAME=mswe GIT_AUTHOR_EMAIL=mswe@example.com",
        "export GIT_COMMITTER_NAME=mswe GIT_COMMITTER_EMAIL=mswe@example.com",
        f"export VIRTUAL_ENV={_VENV}",
        f"export PATH={_VENV}/bin:$PATH",
    )
)


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _PYTHON_ENV,
        "export UV_PYTHON_PREFERENCE=only-managed",
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "uv --version\ngit --version",
        _PYTHON_MINOR,
        f"""export UV_EXCLUDE_NEWER=$(git log -1 --format=%cd --date=iso-strict HEAD)
base_py=$(python_minor)
{_FIX_PYTHON}
fix_py=${{fix_py:-0}}
py=$(( base_py > fix_py ? base_py : fix_py ))
uv venv --python "3.$py" {_VENV}
uv pip install -e ".[{_EXTRAS}]" || {{
    uv pip install --exclude-newer {_UNDATED_CUTOFF} {_LEGACY_TOOLS}
    python -m pip wheel --no-build-isolation --no-deps -w {_WHEEL_DIR} .
    python -m zipfile -e {_WHEEL_DIR}/*.whl {_WHEEL_DIR}/unpacked
    sed -n "s/^Requires-Dist: //p" {_WHEEL_DIR}/unpacked/*.dist-info/METADATA \\
        | sed -E "s/ *; *extra *== *[\\"']{_EXTRAS}[\\"'] *$//" | grep -v "extra *==" > {_WHEEL_DIR}/requirements.txt
    uv pip install --no-build-isolation -r {_WHEEL_DIR}/requirements.txt
    python -m pip install --no-deps {_WHEEL_DIR}/*.whl
    rm -rf {_WHEEL_DIR}
}}
python --version""",
        f"""collected=$(if {_NOSE_ERA}; then {_NOSE_COLLECT}; else {_PYTEST_COLLECT}; fi)
echo "prepare: $collected tests collected"
test "$collected" -gt 0
python -c "import sphinx; assert sphinx.__file__.startswith('/home/{repo}/'), sphinx.__file__\"""",
    )


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd /home/{repo}", apply)))
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _PYTHON_ENV,
        setup,
        f"""if {_NOSE_ERA}; then
    {_NOSE}
else
    {_PYTEST}
fi""",
    )


class Sphinx13561To2276ImageBase(Image):
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
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends {" ".join(_APT_PACKAGES)} && \\
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv=={_UV_VERSION}" && \\
    git config --system --add safe.directory '*' && python3 --version && uv --version && git --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class Sphinx13561To2276ImageDefault(Image):
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
        return Sphinx13561To2276ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = f"{_BINARY_EXCLUDES}\ngit apply --whitespace=nowarn $excludes /home/test.patch"
        fix_apply = f"{_BINARY_EXCLUDES}\ngit apply --whitespace=nowarn $excludes /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        names = " ".join(map(lambda file: file.name, self.files()))
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"COPY {names} /home/",
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class SPHINX_13561_TO_2276(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Sphinx13561To2276ImageDefault(self.pr, self._config)

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
        test_log = _ADDRESS.sub(" at 0x0", _ANSI.sub("", test_log))
        verbose = _VERBOSE.findall(test_log) + _NOSE_RESULT.findall(test_log)
        verbose_by = lambda statuses: set(map(lambda line: line[0], filter(lambda line: line[1] in statuses, verbose)))
        failed_tests = set(_FAILED.findall(test_log)) | verbose_by(("FAILED", "ERROR", "FAIL"))
        passed_tests = (set(_PASSED.findall(test_log)) | verbose_by(("PASSED", "XPASS", "ok"))) - failed_tests
        skipped_tests = (set(_SKIPPED.findall(test_log)) | verbose_by(("XFAIL", "SKIP", "skipped"))) - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
