"""securo-finance/securo, PRs #586-#1467 (merged 2026-08-19 .. 2026-11-17), one era.

Era analysis (PR range 881 > 500, checked at the earliest and latest base commits,
0133385c94 for #586 and c6b3171428 for #1467):
  * layout        monorepo; the Python project is backend/ (frontend/ is TypeScript and
                  is not exercised by these instances)
  * runtime       backend/.python-version = 3.14, requires-python >= 3.11 at both ends
  * build tool    uv with backend/uv.lock at both ends (CI: uv sync --locked --all-extras)
  * test runner   pytest + pytest-asyncio + pytest-xdist, testpaths = ["tests"]
  * services      PostgreSQL 16 + pgvector in CI (POSTGRES_TEST_URL); the conftest
                  fails PostgreSQL tests when CI is set and the URL is missing, so the
                  base image carries a local PostgreSQL 17 + pgvector cluster and the
                  run scripts start it (#1467's tests use the postgres_sessions fixture)
Nothing changes across the range, so a single shared base serves every PR.

Tests run as an unprivileged user (`tester`), as on CI's runners. The plugin sandbox
(backend/plugin_runner/sandbox.py) drops to a per-plugin uid only when it runs as root, and
that uid cannot read pytest's private temp dirs, so under root every plugin test fails with
"No module named ...". PostgreSQL is started and patches are applied as root first.

Test names are pytest node ids relative to backend/, reported with a "backend/" prefix
so they match the repo-relative paths in test.patch.
"""

import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "securo-finance"
_REPO = "securo"
_ERA = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"securo_(\d+)_to_(\d+)", _ERA)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like securo_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_LO}_to_{_ERA_HI}"

_PY_IMAGE = "python:3.14.10-trixie"
_PY_VERSION = "3.14"
_UV_VERSION = "0.13.8"
_PG_MAJOR = "17"
_PG_URL = "postgresql+asyncpg://securo_test@127.0.0.1:5432/securo_test"
_REPO_DIR = f"/home/{_REPO}"
_PROJECT_DIR = f"{_REPO_DIR}/backend"
_VENV = f"{_PROJECT_DIR}/.venv"
_NAME_PREFIX = "backend/"
_TEST_USER = "tester"

_APPLY = "git apply --whitespace=nowarn"
# --continue-on-collection-errors: a test module that cannot import until the fix exists
# must not stop the test stage from running every other test.
_PYTEST = "python -m pytest -v -p no:cacheprovider -n 4 --dist loadfile --continue-on-collection-errors"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_XDIST_RESULT = re.compile(
    r"^\[gw\d+\] \[\s*\d+%\] (PASSED|XPASS|FAILED|ERROR|SKIPPED|XFAIL) (tests/\S.*?)\s*$", re.MULTILINE
)
_SERIAL_RESULT = re.compile(
    r"^(tests/\S+?::\S.*?) (PASSED|XPASS|FAILED|ERROR|SKIPPED|XFAIL)(?:\s+\[\s*\d+%\])?\s*$", re.MULTILINE
)
_COLLECT_ERROR = re.compile(r"^ERROR (tests/[^\s:]+\.py)(?: - .*)?\s*$", re.MULTILINE)

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

_START_POSTGRES = f"""#!/bin/bash
set -euo pipefail

# Local PostgreSQL {_PG_MAJOR} + pgvector, standing in for the CI service container.
if ! pg_ctlcluster {_PG_MAJOR} main status > /dev/null 2>&1; then
    pg_ctlcluster {_PG_MAJOR} main start
fi
for _ in $(seq 1 60); do
    if pg_isready -h 127.0.0.1 -p 5432 -q; then
        echo "start_postgres: ready"
        exit 0
    fi
    sleep 1
done
echo "start_postgres: PostgreSQL did not become ready"
exit 1
"""


def _sections(*parts: str) -> str:
    return "\n\n".join(filter(None, parts)) + "\n"


def _python_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            f"export VIRTUAL_ENV={_VENV}",
            f"export PATH={_VENV}/bin:$PATH",
            f"export POSTGRES_TEST_URL={_PG_URL}",
        )
    )


def _prepare_script(pr: PullRequest) -> str:
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        "\n".join(
            (
                "export CI=true",
                "export UV_PYTHON_PREFERENCE=only-system",
                "export UV_PYTHON_DOWNLOADS=never",
                "export UV_LINK_MODE=copy",
                "export UV_CACHE_DIR=/tmp/uv-cache",
                f"export UV_PROJECT_ENVIRONMENT={_VENV}",
            )
        ),
        f"""cd {_REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        f"""cd {_PROJECT_DIR}
uv --version
uv sync --frozen --all-extras --python {_PY_VERSION}
rm -rf /tmp/uv-cache
{_VENV}/bin/python --version
chown -R {_TEST_USER}:{_TEST_USER} {_REPO_DIR}""",
        f"""bash /home/start_postgres.sh
pg_ctlcluster {_PG_MAJOR} main stop
{_VENV}/bin/python -c "import app, app.main, sqlalchemy, asyncpg, aiosqlite, pytest, pytest_asyncio, xdist; print('DEPS_OK')"
collected=$(runuser -u {_TEST_USER} -- {_VENV}/bin/python -m pytest --collect-only -q -p no:cacheprovider | grep -c "::")
echo "prepare: $collected tests collected"
test "$collected" -gt 0""",
    )


def _run_script(apply: str = "") -> str:
    setup = "\n".join(
        filter(None, (f"cd {_REPO_DIR}", apply, f"chown -R {_TEST_USER}:{_TEST_USER} {_REPO_DIR}", "cd backend"))
    )
    as_user = (
        f"runuser -u {_TEST_USER} -- env HOME=/home/{_TEST_USER} "
        '"PATH=$PATH" "VIRTUAL_ENV=$VIRTUAL_ENV" "CI=$CI" "POSTGRES_TEST_URL=$POSTGRES_TEST_URL" \\\n    '
    )
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _python_env(),
        "bash /home/start_postgres.sh",
        setup,
        as_user + _PYTEST,
    )


class SecuroImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential \\
        postgresql-{_PG_MAJOR} postgresql-{_PG_MAJOR}-pgvector && \\
    rm -rf /var/lib/apt/lists/*

# Local stand-in for CI's pgvector/pgvector service: trust TCP logins from loopback,
# one superuser role and database for the tests, and the vector extension.
RUN sed -ri 's/^(host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\\.0\\.0\\.1\\/32|::1\\/128)[[:space:]]+)[a-z0-9-]+/\\1trust/' \\
        /etc/postgresql/{_PG_MAJOR}/main/pg_hba.conf && \\
    pg_ctlcluster {_PG_MAJOR} main start && \\
    su postgres -c "psql -v ON_ERROR_STOP=1 -c 'CREATE ROLE securo_test LOGIN SUPERUSER'" && \\
    su postgres -c "createdb -O securo_test securo_test" && \\
    su postgres -c "psql -v ON_ERROR_STOP=1 -d securo_test -c 'CREATE EXTENSION IF NOT EXISTS vector'" && \\
    pg_ctlcluster {_PG_MAJOR} main stop

# Unprivileged test user, as on CI's runners (see the module docstring).
RUN useradd --create-home --uid 1000 --shell /bin/bash {_TEST_USER}

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


class SecuroImageDefault(Image):
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
        return SecuroImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "start_postgres.sh", _START_POSTGRES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script()),
            File(".", "test-run.sh", _run_script(f"{_APPLY} /home/test.patch")),
            File(".", "fix-run.sh", _run_script(f"{_APPLY} /home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        names = " ".join(file.name for file in self.files())
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


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA)
class SECURO_1467_TO_586(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO} PR #{pr.number} is outside era {_ERA} ({_ERA_LO}-{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SecuroImageDefault(self.pr, self._config)

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

        def record(status: str, name: str) -> None:
            name = _NAME_PREFIX + name
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for status, name in _XDIST_RESULT.findall(test_log):
            record(status, name)
        for name, status in _SERIAL_RESULT.findall(test_log):
            record(status, name)
        failed_tests.update(_NAME_PREFIX + name for name in _COLLECT_ERROR.findall(test_log))

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
