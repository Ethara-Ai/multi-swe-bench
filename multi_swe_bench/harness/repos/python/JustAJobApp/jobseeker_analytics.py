import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PY_IMAGE = "python:3.10-slim"
_BASE_APT = (
    "git ca-certificates build-essential libpq-dev postgresql postgresql-client procps"
)
_SHIM_DIR = "/opt/tc-shim"
_PG_DATA = "/var/lib/pgsql/data"
_PG_PORT = "5433"
_PG_DB = "jobseeker_analytics"
_TEST_CMD = (
    "python -m pytest backend/tests -v -p no:cacheprovider"
    " --continue-on-collection-errors --disable-warnings"
)

_PREPARE_TEMPLATE = """set -e
STATUS_LOG=/home/__REPO__/prepare-status.log
: > "$STATUS_LOG"
cd /home/__REPO__
echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"
echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"

cat > backend/.env <<'ENV_EOF'
GOOGLE_SCOPES=["https://www.googleapis.com/auth/gmail.readonly", "openid"]
GOOGLE_CLIENT_ID=your-client-id-here
GOOGLE_API_KEY=your-api-key-here
COOKIE_SECRET=your-random-secret-here
REDIRECT_URI=http://localhost:8000/login
ENV=dev
CLIENT_SECRETS_FILE=credentials.json
APP_URL=http://localhost:3000
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:__PG_PORT__/__PG_DB__
DATABASE_URL_LOCAL_VIRTUAL_ENV=postgresql://postgres:postgres@127.0.0.1:__PG_PORT__/__PG_DB__
DATABASE_URL_DOCKER=postgresql://postgres:postgres@127.0.0.1:__PG_PORT__/__PG_DB__
ENV_EOF

mkdir -p __SHIM_DIR__/testcontainers
: > __SHIM_DIR__/testcontainers/__init__.py
cat > __SHIM_DIR__/testcontainers/postgres.py <<'SHIM_EOF'
import os


class PostgresContainer:
    def __init__(self, image=None, port=None, username=None, password=None, dbname=None, **kwargs):
        self.image = image
        self.host = os.environ.get("MSB_PG_HOST", "127.0.0.1")
        self.port = int(os.environ.get("MSB_PG_PORT", "5433"))
        self.username = username or os.environ.get("MSB_PG_USER", "postgres")
        self.password = password or os.environ.get("MSB_PG_PASSWORD", "postgres")
        self.dbname = dbname or os.environ.get("MSB_PG_DB", "jobseeker_analytics")

    def start(self):
        return self

    def stop(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
        return False

    def get_container_host_ip(self):
        return self.host

    def get_exposed_port(self, port=None):
        return str(self.port)

    def get_connection_url(self, *args, **kwargs):
        return "postgresql+psycopg2://%s:%s@%s:%s/%s" % (
            self.username,
            self.password,
            self.host,
            self.port,
            self.dbname,
        )
SHIM_EOF

cat > /home/pg-boot.sh <<'PG_EOF'
set -e
PG_BIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)
mkdir -p /var/run/postgresql
chown -R postgres:postgres /var/run/postgresql
if ! su postgres -c "$PG_BIN/pg_ctl -D __PG_DATA__ status" > /dev/null 2>&1; then
  su postgres -c "$PG_BIN/pg_ctl -D __PG_DATA__ -l /var/lib/pgsql/pg.log -w start"
fi
for _ in $(seq 1 30); do
  if "$PG_BIN/pg_isready" -h 127.0.0.1 -p __PG_PORT__ -q; then
    exit 0
  fi
  sleep 1
done
cat /var/lib/pgsql/pg.log
exit 1
PG_EOF
chmod +x /home/pg-boot.sh

PG_BIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)
mkdir -p /var/lib/pgsql /var/run/postgresql
chown -R postgres:postgres /var/lib/pgsql /var/run/postgresql
su postgres -c "$PG_BIN/initdb -D __PG_DATA__ -U postgres --auth-local=trust --auth-host=trust"
echo "port = __PG_PORT__" >> __PG_DATA__/postgresql.conf
echo "listen_addresses = '127.0.0.1'" >> __PG_DATA__/postgresql.conf
bash /home/pg-boot.sh
su postgres -c "$PG_BIN/createdb -h 127.0.0.1 -p __PG_PORT__ -U postgres __PG_DB__"
su postgres -c "$PG_BIN/pg_ctl -D __PG_DATA__ -m fast -w stop"

set +e
pip install --no-cache-dir --upgrade pip setuptools wheel
pip install --no-cache-dir -r backend/requirements.txt
PIP_EXIT=$?
echo "PREPARE_PIP_EXIT=$PIP_EXIT" >> "$STATUS_LOG"
set -e
test "$PIP_EXIT" = "0"

export PYTHONPATH=__SHIM_DIR__
set +e
python -m pytest backend/tests --collect-only -q -p no:cacheprovider --continue-on-collection-errors > /home/collect.log 2>&1
COLLECT_EXIT=$?
set -e
tail -n 40 /home/collect.log || true
COLLECTED=$(grep -cE "::" /home/collect.log 2>/dev/null || true)
COLLECTED=${COLLECTED:-0}
echo "PREPARE_COLLECT_EXIT=$COLLECT_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECTED=$COLLECTED" >> "$STATUS_LOG"
if [ "$COLLECT_EXIT" -ge 128 ]; then
  echo "PREPARE_SIGNAL_DEATH=1" >> "$STATUS_LOG"
else
  test "$COLLECTED" -gt 0
fi
echo "DEPS_OK"
"""

_CHECK_GIT_CHANGES = """set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_RUN_TEMPLATE = """set -eo pipefail
cd /home/__REPO__
git reset --hard
git clean -qfd
export CI=true
bash /home/pg-boot.sh
export PYTHONPATH=__SHIM_DIR__
export MSB_PG_HOST=127.0.0.1
export MSB_PG_PORT=__PG_PORT__
export MSB_PG_DB=__PG_DB__
__APPLY____TEST_CMD__
"""


def _render(template: str, repo: str) -> str:
    return (
        template.replace("__REPO__", repo)
        .replace("__SHIM_DIR__", _SHIM_DIR)
        .replace("__PG_DATA__", _PG_DATA)
        .replace("__PG_PORT__", _PG_PORT)
        .replace("__PG_DB__", _PG_DB)
    )


def _run_script(repo: str, apply_cmd: str) -> str:
    return (
        _render(_RUN_TEMPLATE, repo)
        .replace("__APPLY__", apply_cmd)
        .replace("__TEST_CMD__", _TEST_CMD)
    )


class JobseekerAnalyticsImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
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
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends {_BASE_APT} && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class JobseekerAnalyticsImageDefault(Image):
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
        return JobseekerAnalyticsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE_TEMPLATE, repo)),
            File(".", "run.sh", _run_script(repo, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(repo, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).rstrip("\n")
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {name}:{tag}\n\n"
            f"WORKDIR /home/{repo}\n\n"
            "RUN git reset --hard\n\n"
            f"RUN git checkout {sha}\n\n"
            f"{hardening}\n\n"
            "WORKDIR /home/\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n"
        )


@Instance.register("JustAJobApp", "jobseeker-analytics")
class JOBSEEKER_ANALYTICS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JobseekerAnalyticsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        log_clean = re.sub(r"\x1b\[[0-9;]*m", "", log)

        pattern = re.compile(
            r"(\S+\.py::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)\b"
            r"|^(PASSED|FAILED|SKIPPED|ERROR)\s+(\S+\.py::\S+)"
        )

        for line in log_clean.splitlines():
            m = pattern.search(line.strip())
            if not m:
                continue
            if m.group(1):
                name, status = m.group(1), m.group(2)
            else:
                status, name = m.group(3), m.group(4)

            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
