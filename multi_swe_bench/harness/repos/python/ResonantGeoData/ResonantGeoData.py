import re

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


BASE_IMAGE = "python:3.8"

REPO_DIR = "/home/ResonantGeoData"
TESTS_DIR = f"{REPO_DIR}/django-rgd/tests"

NODEID_PREFIX = "django-rgd/tests/"

REPORT_FILE = "/home/pytest-report.xml"
STDOUT_LOG = "/home/pytest-stdout.log"

APT_PACKAGES = [
    "postgresql-15",
    "postgresql-15-postgis-3",
    "postgresql-client-15",
    "gdal-bin",
    "libgeos-c1v5",
    "libmagic1",
    "procps",
]

MINIO_RELEASE = "RELEASE.2021-11-09T03-21-45Z"

PINNED_REQUIREMENTS = [
    "boto3==1.19.11",
    "celery==5.1.2",
    "Django==3.2.9",
    "django-allauth==0.45.0",
    "django-click==2.3.0",
    "django-crum==0.7.9",
    "django-extensions==3.1.3",
    "django-filter==21.1",
    "django-girder-utils==0.11.0",
    "django-model-utils==4.2.0",
    "django-oauth-toolkit==1.5.0",
    "djangorestframework==3.12.4",
    "drf-yasg==1.20.0",
    "filelock==3.3.2",
    "pooch==1.5.2",
    "psycopg2-binary==2.9.1",
    "python-magic==0.4.24",
    "flower==1.0.0",
    "psutil==5.8.0",
    "django-s3-file-field[minio]==0.2.0",
    "django-minio-storage==0.3.10",
    "minio==6.0.2",
    "ruamel.yaml==0.17.17",
    "requests==2.26.0",
    "requests-toolbelt==0.9.1",
    "geomet==0.3.0",
    "tqdm==4.62.3",
    "validators==0.18.2",
    "factory-boy==3.2.1",
    "pytest==6.2.5",
    "pytest-django==4.4.0",
    "pytest-factoryboy==2.1.0",
    "pytest-mock==3.6.1",
    "pytest-cov==3.0.0",
    "pytest-memprof==0.2.0",
    "amqp==5.0.6",
    "appdirs==1.4.4",
    "asgiref==3.4.1",
    "attrs==21.2.0",
    "billiard==3.6.4.0",
    "click==7.1.2",
    "click-didyoumean==0.0.3",
    "click-plugins==1.1.1",
    "click-repl==0.2.0",
    "prompt-toolkit==3.0.22",
    "coverage==6.1.1",
    "Faker==9.8.0",
    "inflection==0.5.1",
    "iniconfig==1.1.1",
    "kombu==5.1.0",
    "pluggy==1.0.0",
    "py==1.11.0",
    "pytz==2021.3",
    "six==1.16.0",
    "sqlparse==0.4.2",
    "toml==0.10.2",
    "tornado==6.1",
    "uritemplate==4.1.1",
    "vine==5.0.0",
]

INSTALL_CMD = (
    "pip install --no-cache-dir --no-deps "
    "-e ./django-rgd -e ./testing-utils -e ./django-rgd/client"
)

PYTEST_CMD = (
    "python -m pytest -rA -p no:cacheprovider --continue-on-collection-errors "
    f"--junitxml={REPORT_FILE}"
)

TEST_ENV = """export CI=true
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
# testing-utils/rgd_testing_utils/settings.py reads all of the following.
export DATABASE_HOST=127.0.0.1
export MINIO_STORAGE_ENDPOINT=127.0.0.1:9000
export MINIO_STORAGE_ACCESS_KEY=minioAccessKey
export MINIO_STORAGE_SECRET_KEY=minioSecretKey
export MINIO_STORAGE_MEDIA_URL=http://127.0.0.1:9000/django-storage
# test_project/settings.py sets CELERY_TASK_ALWAYS_EAGER, so no broker is ever
# contacted; the in-memory transport guarantees that a stray .delay() cannot
# block for amqp's 30s connect timeout.
export CELERY_BROKER_URL=memory://localhost/"""

TEST_BODY = f"""rm -f {REPORT_FILE} {STDOUT_LOG}
cd {TESTS_DIR}
set +e
{PYTEST_CMD} > {STDOUT_LOG} 2>&1
STATUS=$?
set -e
cat {STDOUT_LOG}
echo "pytest exit status: $STATUS"
if [ ! -s {REPORT_FILE} ]; then
    echo "FATAL: pytest wrote no JUnit report; the runner never completed." >&2
    exit 1
fi
"""


class ImageBase(Image):
    """Repo-level base image. Carries the stock harness shape and nothing more:
    FROM, apt, clone, checkout, history scrub, CMD. Every bespoke setup step
    lives in prepare.sh on the PR layer instead.

    Tagged per-PR (`base-pr-<N>`) -- a shared `base` tag would collapse every PR
    of the repo onto one image, and because the enhancer scrubs git history down
    to a single commit lineage, a second PR's prepare.sh could then no longer
    check out its own base commit."""

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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return APT_PACKAGES

    _DEFAULT_APT_PACKAGES = [
        "ca-certificates",
        "curl",
        "build-essential",
        "git",
        "gnupg",
        "make",
        "python3",
        "sudo",
        "wget",
    ]

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo = _safe_path_component(self.pr.repo)
        packages_str = " \\\n    ".join(
            self._DEFAULT_APT_PACKAGES + self.extra_packages()
        )

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(self._get_apt_update_command(packages_str, base_img))
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        extra_setup = self.extra_setup()
        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add patches + scripts, then run
    prepare.sh -- which is where the entire environment build now happens."""

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        """The full environment build, executed once by the PR image's
        `RUN bash /home/prepare.sh`.

        Every step is idempotent, so the script is still correct on the
        envagent replay path (where the harness re-feeds it before each stage).
        `set -e` holds for the whole setup: at image-build time a failure here
        MUST fail the build loudly rather than yield an image that dies
        mid-suite. Only the trailing editable install is tolerant, per the
        runbook, and the asserts immediately after it would catch a genuine
        breakage anyway."""
        requirements = " \\\n    ".join(f'"{r}"' for r in PINNED_REQUIREMENTS)
        return """#!/bin/bash
set -e
cd [[REPO_DIR]]

# --- Pristine tree at the PR's base commit -------------------------------
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh
# `git clean` is deliberately NOT used: it would delete the *.egg-info
# directories the editable installs depend on. They are gitignored by the
# repo, so check_git_changes stays green regardless.

# --- MinIO ---------------------------------------------------------------
# Arch is derived from `uname -m`, not from a Dockerfile ARG: the PR image is
# not processed by DockerfileEnhancer (its dependency() is an Image, not a
# string), so it carries no `ARG TARGETARCH` and $TARGETARCH would be empty.
if [ ! -x /usr/local/bin/minio ]; then
    case "$(uname -m)" in
        x86_64)  MINIO_ARCH=amd64 ;;
        aarch64) MINIO_ARCH=arm64 ;;
        *) echo "FATAL: unsupported architecture $(uname -m)" >&2; exit 1 ;;
    esac
    curl -fsSL -o /usr/local/bin/minio \\
        "https://dl.min.io/server/minio/release/linux-${MINIO_ARCH}/archive/minio.[[MINIO_RELEASE]]"
    chmod +x /usr/local/bin/minio
fi
minio --version > /dev/null

# --- PostgreSQL / PostGIS cluster ----------------------------------------
pg_lsclusters | grep -qE '^15 +main' || pg_createcluster 15 main
# Trust rules are prepended so they win on first match; this is a throwaway
# single-tenant test cluster reachable only from inside the container.
if ! head -3 /etc/postgresql/15/main/pg_hba.conf | grep -q 'all all 127.0.0.1/32 trust'; then
    printf 'local all all trust\\nhost all all 127.0.0.1/32 trust\\nhost all all ::1/128 trust\\n' \\
        > /tmp/pg_hba.conf
    cat /etc/postgresql/15/main/pg_hba.conf >> /tmp/pg_hba.conf
    mv /tmp/pg_hba.conf /etc/postgresql/15/main/pg_hba.conf
    chown postgres:postgres /etc/postgresql/15/main/pg_hba.conf
    printf '\\nfsync = off\\nsynchronous_commit = off\\nfull_page_writes = off\\n' \\
        >> /etc/postgresql/15/main/postgresql.conf
fi
pg_ctlcluster 15 main start
su postgres -c "psql -c \\"ALTER USER postgres WITH PASSWORD 'postgres'\\"" > /dev/null
su postgres -c "psql -d template1 -c 'CREATE EXTENSION IF NOT EXISTS postgis'" > /dev/null
su postgres -c "psql -lqtA -F: | cut -d: -f1 | grep -qx django || createdb django"
su postgres -c "psql -d django -c 'SELECT postgis_version()'" > /dev/null
pg_ctlcluster 15 main stop

# --- Python stack --------------------------------------------------------
# setuptools-scm belongs in this BOOTSTRAP step, not the pin list:
# django-minio-storage is an sdist that declares setup_requires=["setuptools_scm"],
# and pip builds every sdist in one pass before installing any of them -- so a
# setuptools-scm listed alongside it would not yet be importable when its
# setup.py runs, and setuptools would fall back to fetching a modern
# setuptools_scm that demands setuptools>=61.
pip install --no-cache-dir -U "pip==23.3.2" "setuptools==59.6.0" "wheel==0.37.1" "setuptools-scm==6.3.2"
pip install --no-cache-dir \\
    [[REQUIREMENTS]]
[[INSTALL_CMD]] || true

# --- Fail-fast asserts ---------------------------------------------------
# Cheap, and they convert two silent mid-suite failure modes (GeoDjango unable
# to dlopen its C libraries; a package that resolved but cannot import) into a
# loud build failure.
python -c "import ctypes.util as u; \\
assert u.find_library('gdal'), 'libgdal not resolvable by ctypes'; \\
assert u.find_library('geos_c'), 'libgeos_c not resolvable by ctypes'"
python -c "import django, psycopg2, minio_storage, rgd, rgd_client, rgd_testing_utils; \\
print('django', django.get_version(), '| django-rgd', rgd.__version__)"

# The trailing `set +e` matters on the envagent replay path: the body is fed
# into the same persistent bash session that later runs the tests, and a leaked
# `-e` would tear that session down the moment pytest exits non-zero --
# truncating the log parse_log is meant to read.
set +e
""".replace("[[REPO_DIR]]", REPO_DIR) \
   .replace("[[BASE_SHA]]", self.pr.base.sha) \
   .replace("[[MINIO_RELEASE]]", MINIO_RELEASE) \
   .replace("[[REQUIREMENTS]]", requirements) \
   .replace("[[INSTALL_CMD]]", INSTALL_CMD)

    def files(self) -> list[File]:
        def stage(patch_line: str) -> str:
            return """#!/bin/bash
set -eo pipefail
[[TEST_ENV]]
cd [[REPO_DIR]]
[[PATCH_LINE]]bash /home/start-services.sh
[[TEST_BODY]]""".replace("[[TEST_ENV]]", TEST_ENV) \
                 .replace("[[REPO_DIR]]", REPO_DIR) \
                 .replace("[[PATCH_LINE]]", patch_line) \
                 .replace("[[TEST_BODY]]", TEST_BODY)

        apply_test = (
            f'git -C {REPO_DIR} apply --whitespace=nowarn /home/test.patch'
            ' || { echo "Error: git apply failed" >&2; exit 1; }\n'
        )
        apply_both = (
            f'git -C {REPO_DIR} apply --whitespace=nowarn'
            ' /home/test.patch /home/fix.patch'
            ' || { echo "Error: git apply failed" >&2; exit 1; }\n'
        )

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "start-services.sh",
                """#!/bin/bash
# PostGIS + MinIO: the two services django-rgd/tests cannot run without. Both
# were installed and initialised by prepare.sh at image-build time; this only
# starts them. Never `exit 1` on a service that is merely already up.
set -e

pg_ctlcluster 15 main start 2>/dev/null || true
for _ in $(seq 1 60); do
    pg_isready -h 127.0.0.1 -p 5432 -U postgres > /dev/null 2>&1 && break
    sleep 1
done
pg_isready -h 127.0.0.1 -p 5432 -U postgres

mkdir -p /var/lib/minio
if ! pgrep -x minio > /dev/null 2>&1; then
    MINIO_ROOT_USER="$MINIO_STORAGE_ACCESS_KEY" \\
    MINIO_ROOT_PASSWORD="$MINIO_STORAGE_SECRET_KEY" \\
    MINIO_ACCESS_KEY="$MINIO_STORAGE_ACCESS_KEY" \\
    MINIO_SECRET_KEY="$MINIO_STORAGE_SECRET_KEY" \\
    nohup minio server /var/lib/minio --address :9000 --console-address :9001 \\
        > /var/log/minio.log 2>&1 &
fi
for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:9000/minio/health/live > /dev/null 2>&1 && break
    sleep 1
done
curl -sf http://127.0.0.1:9000/minio/health/live > /dev/null

echo "start-services: postgres and minio are up"
""",
            ),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", stage("")),
            File(".", "test-run.sh", stage(apply_test)),
            File(".", "fix-run.sh", stage(apply_both)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("ResonantGeoData", "ResonantGeoData")
class ResonantGeoData(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        status_first = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(\S+::\S+)")
        node_first = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        for raw in clean_log.split("\n"):
            line = raw.strip()

            match = status_first.match(line)
            if match:
                status, name = match.group(1), match.group(2)
            else:
                match = node_first.match(line)
                if not match:
                    continue
                name, status = match.group(1), match.group(2)

            if not name.startswith(NODEID_PREFIX):
                name = NODEID_PREFIX + name

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

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
