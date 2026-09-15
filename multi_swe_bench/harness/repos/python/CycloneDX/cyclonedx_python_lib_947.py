import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.CycloneDX.cyclonedx_python_lib_906_to_145 import (
    CycloneDxPythonLib as _OldCycloneDxPythonLib,
)

REPO_DIR = "cyclonedx-python-lib"

_CHECK_GIT_CHANGES_SH = f"""\
#!/bin/bash
set -e
cd /home/{REPO_DIR}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: uncommitted changes"
    git status --porcelain
    exit 1
fi
echo "check_git_changes: No uncommitted changes"
"""


def _base_dockerfile(from_image: str, org: str) -> str:
    return f"""# syntax=docker/dockerfile:1.6
FROM {from_image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{REPO_DIR}.git"
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    POETRY_VIRTUALENVS_CREATE=false \\
    CI=true \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{REPO_DIR}" \\
      org.opencontainers.image.description="{org}/{REPO_DIR} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{REPO_DIR}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        ca-certificates git curl gnupg sudo build-essential libffi-dev libssl-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN git -C /home clone "${{REPO_URL}}" {REPO_DIR}

CMD ["/bin/bash"]
"""


def _pr_dockerfile(name: str, tag: str, sha: str, copy_commands: str) -> str:
    return f"""FROM {name}:{tag}

WORKDIR /home/{REPO_DIR}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh
"""


def _prepare_sh(sha: str) -> str:
    return f"""#!/bin/bash
set -e

cd /home/{REPO_DIR}
bash /home/check_git_changes.sh

pip install --upgrade pip wheel || true
pip install --upgrade 'setuptools>=68,<81' || true
pip install --upgrade 'poetry>=1.8,<2.0' || true

poetry config virtualenvs.create false || true
poetry install --no-interaction --no-ansi --with dev || poetry install --no-interaction --no-ansi || true

pip install --upgrade 'setuptools>=68,<81' pytest pytest-cov lxml jsonschema || true

python3 -c "import cyclonedx, lxml, jsonschema, pytest, pkg_resources; print('DEPS_OK')"
"""


def _run_sh() -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
python3 -m pytest -v --continue-on-collection-errors
"""


def _test_run_sh() -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
python3 -m pytest -v --continue-on-collection-errors
"""


def _fix_run_sh() -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python3 -m pytest -v --continue-on-collection-errors
"""


def _pr_files(pr: PullRequest) -> list:
    sha = pr.base.sha
    return [
        File(".", "fix.patch", pr.fix_patch),
        File(".", "test.patch", pr.test_patch),
        File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
        File(".", "prepare.sh", _prepare_sh(sha)),
        File(".", "run.sh", _run_sh()),
        File(".", "test-run.sh", _test_run_sh()),
        File(".", "fix-run.sh", _fix_run_sh()),
    ]


def _pr_copy_commands(files: list) -> str:
    return "\n".join(f"COPY {f.name} /home/" for f in files) + "\n"


class ImageBase947(Image):
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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-py311-947"

    def workdir(self) -> str:
        return "base_py311_947"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org)


class ImageDefault947(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBase947(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            _pr_copy_commands(self.files()),
        )


class _CycloneDxPythonLib947(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault947(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        passed_pattern = re.compile(r"^([^\s]+)\s+PASSED\b", re.MULTILINE)
        failed_pattern = re.compile(r"^FAILED\s+([^\s-]+)", re.MULTILINE)
        error_pattern = re.compile(r"^ERROR\s+([^\s-]+)", re.MULTILINE)
        skipped_pattern = re.compile(r"^([^\s]+)\s+SKIPPED\b", re.MULTILINE)

        for m in passed_pattern.finditer(test_log):
            passed_tests.add(m.group(1).strip())
        for m in failed_pattern.finditer(test_log):
            failed_tests.add(m.group(1).strip())
        for m in error_pattern.finditer(test_log):
            failed_tests.add(m.group(1).strip())
        for m in skipped_pattern.finditer(test_log):
            skipped_tests.add(m.group(1).strip())

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


@Instance.register("CycloneDX", "cyclonedx-python-lib")
class CycloneDxPythonLibRouter(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if pr.number > 906:
            return _CycloneDxPythonLib947(pr, config, *args, **kwargs)
        return _OldCycloneDxPythonLib(pr, config, *args, **kwargs)
