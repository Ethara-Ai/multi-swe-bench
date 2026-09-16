import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.11-slim"
UV_VERSION = "0.5.7"
HATCH_ENV = "test"
NUMPY_SERIES = "1.26"
VENV_DIR = "/opt/zarr-venv"
_BASE_APT = "build-essential ca-certificates git"

_PYTEST_CMD = (
    f"{VENV_DIR}/bin/python -m pytest -v -rA --color=no -p no:cacheprovider "
    "--continue-on-collection-errors"
)


class ZarrV3ImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        interval = self.pr.number_interval or ""
        _, _, span = interval.rpartition("python_")
        return f"base-{span}" if span else "base"

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
    TZ=UTC \\
    PYTHONUNBUFFERED=1 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "uv=={UV_VERSION}" && \\
    uv --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class ZarrV3ImageDefault(Image):
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
        return ZarrV3ImageBase(self.pr, self._config)

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
            "set -euo pipefail\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "ERA_CUTOFF=\"$(TZ=UTC git show -s --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)\"\n"
            f"uv venv --python /usr/local/bin/python3 {VENV_DIR}\n"
            f"python3 - {HATCH_ENV} {NUMPY_SERIES} > {VENV_DIR}/hatch-requirements.txt <<'HATCH_ENV_REQS'\n"
            "import sys, tomllib\n"
            "env = tomllib.load(open('pyproject.toml', 'rb'))['tool']['hatch']['envs'][sys.argv[1]]\n"
            "features = sorted(set(env.get('features', [])) | {'optional'})\n"
            "print('-e .[' + ','.join(features) + ']')\n"
            "for req in env.get('dependencies', []) + env.get('extra-dependencies', []):\n"
            "    print(req.replace('{matrix:numpy}', sys.argv[2]))\n"
            "HATCH_ENV_REQS\n"
            f"uv pip install --python {VENV_DIR}/bin/python "
            f'--exclude-newer "${{ERA_CUTOFF}}" -r {VENV_DIR}/hatch-requirements.txt\n'
            f"{VENV_DIR}/bin/python - {NUMPY_SERIES} <<'DEPS_GATE'\n"
            "import sys\n"
            "import zarr, zarr.v2\n"
            f"assert zarr.__file__.startswith('/home/{repo}/src/zarr/'), zarr.__file__\n"
            "from zarr import store, array, group\n"
            "import numpy\n"
            "assert numpy.__version__.startswith(sys.argv[1] + '.'), numpy.__version__\n"
            "import numcodecs, crc32c, zstandard, donfig, fasteners, asciitree, msgpack, lmdb\n"
            "import pytest, pytest_asyncio\n"
            "print('DEPS_OK')\n"
            "DEPS_GATE\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            f"{_PYTEST_CMD}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_PYTEST_CMD}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "export CI=true\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_PYTEST_CMD}\n"
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

{copy_commands}
RUN bash /home/prepare.sh

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
"""


@Instance.register("zarr-developers", "zarr_python_1884_to_1884")
class ZARR_PYTHON_1884_TO_1884(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ZarrV3ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        statuses = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        re_verbose = re.compile(
            rf"^(\S+?::.+?)\s+({statuses})(?:\s+\[\s*\d+%\])?\s*$"
        )
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(\S+?::.+?)(?:\s+-\s.*)?$"
        )

        def record(name: str, status: str) -> None:
            if status in ("FAILED", "ERROR"):
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status in ("PASSED", "XPASS"):
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        for line in test_log.splitlines():
            line = line.strip()
            m = re_verbose.match(line)
            if m:
                record(m.group(1), m.group(2))
                continue
            m = re_summary.match(line)
            if m:
                record(m.group(2), m.group(1))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
