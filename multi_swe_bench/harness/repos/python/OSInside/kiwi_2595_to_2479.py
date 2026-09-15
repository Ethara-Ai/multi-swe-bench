import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

UBUNTU_IMAGE = "ubuntu:22.04"
_BASE_APT = "ca-certificates curl build-essential git gnupg make python3 python3-pip python3-dev libssl-dev libffi-dev sudo wget libxml2-dev libxslt1-dev libattr1-dev zlib1g-dev"

_PR_NUMBERS: set = set()


class KiwiImageBase_2595_to_2479(Image):
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
        return UBUNTU_IMAGE

    def image_tag(self) -> str:
        return "base-2595-to-2479"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    POETRY_VIRTUALENVS_CREATE=false \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl/certs /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends {_BASE_APT}; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}
CMD ["/bin/bash"]
"""


class KiwiImageDefault_2595_to_2479(Image):
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
        return KiwiImageBase_2595_to_2479(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "pip install poetry || true\n"
            "sed -i 's/types-pkg_resources/types-setuptools/' pyproject.toml || true\n"
            "poetry install --all-extras || true\n"
            "python3 -c \"import kiwi, pytest, lxml, yaml, docopt; from unittest import mock; print('DEPS_OK')\"\n"
        )

        conftest_setup = (
            "cat > test/unit/conftest.py <<'PYEOF'\n"
            "import os\n"
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def _kiwi_chdir_test_unit():\n"
            "    old = os.getcwd()\n"
            "    os.chdir(os.path.dirname(__file__))\n"
            "    try:\n"
            "        yield\n"
            "    finally:\n"
            "        os.chdir(old)\n"
            "PYEOF\n"
        )

        test_cmd = (
            f"{conftest_setup}"
            "cd test/unit && poetry run pytest -v --doctest-modules "
            "--no-cov-on-fail --cov=kiwi --cov-report=term-missing "
            "--cov-fail-under=100 --cov-config .coveragerc"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            f"{test_cmd}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{test_cmd}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{test_cmd}\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("OSInside", "kiwi_2595_to_2479")
class KIWI_2595_TO_2479(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _PR_NUMBERS.add(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KiwiImageDefault_2595_to_2479(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_pattern = re.compile(r"^([^\s]+)\s+PASSED\b", re.MULTILINE)
        for m in passed_pattern.finditer(log):
            passed_tests.add(m.group(1).strip())

        failed_pattern = re.compile(r"^FAILED\s+([^\s-]+)", re.MULTILINE)
        for m in failed_pattern.finditer(log):
            failed_tests.add(m.group(1).strip())

        error_pattern = re.compile(r"^ERROR\s+([^\s-]+)", re.MULTILINE)
        for m in error_pattern.finditer(log):
            failed_tests.add(m.group(1).strip())

        skipped_pattern = re.compile(r"^([^\s]+)\s+SKIPPED\b", re.MULTILINE)
        for m in skipped_pattern.finditer(log):
            skipped_tests.add(m.group(1).strip())

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
