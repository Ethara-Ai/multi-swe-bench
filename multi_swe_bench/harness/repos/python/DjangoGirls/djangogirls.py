import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "djangogirls"


_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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
"""


_PG_START_SH = """\
export PGDATA=/var/lib/postgresql/data
if ! id postgres >/dev/null 2>&1; then
    useradd -r -s /bin/bash postgres || true
fi
mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 0700 "$PGDATA"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    su -s /bin/bash postgres -c "/usr/lib/postgresql/*/bin/initdb -D $PGDATA -U postgres --auth-local=trust --auth-host=trust" >/dev/null
fi
if ! pgrep -x postgres >/dev/null 2>&1; then
    su -s /bin/bash postgres -c "/usr/lib/postgresql/*/bin/pg_ctl -D $PGDATA -l /tmp/pg.log -o '-c listen_addresses=127.0.0.1 -c unix_socket_directories=/var/run/postgresql' start" >/dev/null
fi
for i in $(seq 1 30); do
    if su -s /bin/bash postgres -c "psql -h 127.0.0.1 -U postgres -c 'select 1' >/dev/null 2>&1"; then
        break
    fi
    sleep 1
done
su -s /bin/bash postgres -c "psql -h 127.0.0.1 -U postgres -c \\"ALTER USER postgres WITH SUPERUSER CREATEDB PASSWORD 'postgres';\\"" >/dev/null 2>&1 || true
su -s /bin/bash postgres -c "psql -h 127.0.0.1 -U postgres -tc \\"SELECT 1 FROM pg_database WHERE datname='djangogirls'\\" | grep -q 1 || psql -h 127.0.0.1 -U postgres -c \\"CREATE DATABASE djangogirls OWNER postgres;\\"" >/dev/null 2>&1 || true
"""


def _base_dockerfile(from_image: str, org: str) -> str:
    return f"""\
# syntax=docker/dockerfile:1.6

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
    CI=true \\
    DJANGO_SETTINGS_MODULE=djangogirls.settings \\
    DJANGO_SECRET_KEY=ci-test-dummy-secret-not-a-real-secret \\
    PGHOST=127.0.0.1 \\
    PGPORT=5432 \\
    PGUSER=postgres \\
    PGPASSWORD=postgres \\
    PGDATABASE=djangogirls \\
    DATABASE_URL=postgres://postgres:postgres@127.0.0.1:5432/djangogirls \\
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
        ca-certificates \\
        git \\
        curl \\
        gnupg \\
        sudo \\
        build-essential \\
        libpq-dev \\
        postgresql \\
        postgresql-client \\
        libjpeg-dev \\
        zlib1g-dev \\
        libffi-dev \\
        libssl-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN git -C /home clone "${{REPO_URL}}" {REPO_DIR}

CMD ["/bin/bash"]
"""


def _pr_dockerfile(name: str, tag: str, sha: str, copy_commands: str) -> str:
    return f"""\
FROM {name}:{tag}

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
    return f"""\
#!/bin/bash
set -e

cd /home/{REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

if [ -f .environment-example ] && [ ! -f .environment ]; then
    cp .environment-example .environment || true
fi

pip install --upgrade pip wheel || true
pip install --upgrade "setuptools>=68,<81" || true
if [ -f requirements.txt ]; then
    pip install -r requirements.txt || true
fi
if [ -f requirements-dev.txt ]; then
    pip install -r requirements-dev.txt || true
fi
pip install --upgrade "setuptools>=68,<81" pytest pytest-django pytest-cov || true

python3 -c "import django, pytest, pkg_resources; print('DEPS_OK')"
"""


def _run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_PG_START_SH}
cd /home/{REPO_DIR}
python3 manage.py migrate --noinput || true
py.test -v --cov=. --cov-report=term-missing --continue-on-collection-errors
"""


def _test_run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_PG_START_SH}
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
python3 manage.py migrate --noinput || true
py.test -v --cov=. --cov-report=term-missing --continue-on-collection-errors
"""


def _fix_run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_PG_START_SH}
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python3 manage.py migrate --noinput || true
py.test -v --cov=. --cov-report=term-missing --continue-on-collection-errors
"""


def _pr_files(pr: PullRequest) -> list[File]:
    return [
        File(".", "fix.patch", f"{pr.fix_patch}"),
        File(".", "test.patch", f"{pr.test_patch}"),
        File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
        File(".", "prepare.sh", _prepare_sh(pr.base.sha)),
        File(".", "run.sh", _run_sh()),
        File(".", "test-run.sh", _test_run_sh()),
        File(".", "fix-run.sh", _fix_run_sh()),
    ]


def _pr_copy_commands(files: list[File]) -> str:
    return "\n".join(f"COPY {f.name} /home/" for f in files) + "\n"


class ImageBase(Image):
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
        return "python:3.9-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-py39"

    def workdir(self) -> str:
        return "base_py39"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org)


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "Image":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            _pr_copy_commands(self.files()),
        )


@Instance.register("DjangoGirls", "djangogirls")
class DjangoGirls(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

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
