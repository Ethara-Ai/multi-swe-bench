import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_PY_IMAGE = "python:3.6.15-bullseye"
_UV_VERSION = "0.13.8"
_VENV = "/opt/mswe/venv"
_UNDATED_CUTOFF = "2024-01-01T00:00:00Z"
_UNDATED_PACKAGES = ("setuptools", "wheel", "nose", "numpy", "decorator", "markupsafe")
_IPTEST_RUNNER = "nose-ignore-docstring requests mock"
_IPTEST = "python -m IPython.testing -- --verbosity=2 --with-ignore-docstrings"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NOSE_RESULT = re.compile(r"^(?!Failure: )(\S.*?) \.\.\. (?:(?!\S.* \.\.\. ).*\n)*?(ok|FAIL|ERROR|SKIP|KNOWNFAIL|skipped)\b", re.M)
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


def _python_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            f"export VIRTUAL_ENV={_VENV}",
            f"export PATH={_VENV}/bin:$PATH",
        )
    )


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _python_env(),
        "export UV_PYTHON_PREFERENCE=system",
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "uv --version\ngit --version",
        f"""UV_EXCLUDE_NEWER=$(git log -1 --format=%cd --date=iso-strict HEAD)
test -n "$UV_EXCLUDE_NEWER"
export UV_EXCLUDE_NEWER
undated=$(printf "%s\\n" "$UV_EXCLUDE_NEWER" "{_UNDATED_CUTOFF}" | sort | tail -n 1)
undated_flags=()
for package in {" ".join(_UNDATED_PACKAGES)}; do
    undated_flags+=(--exclude-newer-package "$package=$undated")
done
uv venv --python python3 {_VENV}
uv pip install "${{undated_flags[@]}}" setuptools wheel
python setup.py develop --no-deps
python -c "import pkg_resources; d = pkg_resources.get_distribution('ipython'); print('\\n'.join(map(str, d.requires(set(d.extras) - {{'all', 'doc'}}))))" > /tmp/requirements.txt
uv pip install "${{undated_flags[@]}}" -r /tmp/requirements.txt {_IPTEST_RUNNER}
python --version""",
        f"""cd /tmp
python -I -c "import IPython, nose; assert IPython.__file__.startswith('/home/{repo}/'), IPython.__file__\"""",
    )


def _run_script(repo: str, apply: str = "") -> str:
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _python_env(),
        "\n".join(filter(None, (f"cd /home/{repo}", apply))),
        f"cd /tmp\n{_IPTEST}",
    )


class Ipython9823To4536ImageBase(Image):
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

RUN pip install --no-cache-dir --ignore-requires-python --only-binary=:all: --platform "musllinux_1_1_$(uname -m)" --target /opt/uv "uv=={_UV_VERSION}" && \\
    ln -s /opt/uv/bin/uv /usr/local/bin/uv && \\
    git config --system --add safe.directory '*' && python3 --version && uv --version && git --version && gcc --version"""
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


class Ipython9823To4536ImageDefault(Image):
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
        return Ipython9823To4536ImageBase(self.pr, self._config)

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
class IPYTHON_9823_TO_4536(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Ipython9823To4536ImageDefault(self.pr, self._config)

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
        test_log = _ADDRESS.sub(" at 0x0", _ANSI.sub("", test_log)).replace("\r\n", "\n").replace("\r", "\n")
        results = _NOSE_RESULT.findall(test_log)

        def named(*statuses: str) -> set[str]:
            return {name for name, status in results if status in statuses}

        failed_tests = named("FAIL", "ERROR")
        passed_tests = named("ok") - failed_tests
        skipped_tests = named("SKIP", "KNOWNFAIL", "skipped") - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
