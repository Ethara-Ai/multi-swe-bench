import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Environment expected by the Django backend test-suite. Mirrors the "test-back"
# job of .github/workflows/impress.yml (PostgreSQL + MinIO + django-configurations).
ENV_EXPORTS = """export CI=true
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PYTHONPATH=/home/docs/src/backend
export DJANGO_SETTINGS_MODULE=impress.settings
export DJANGO_CONFIGURATION=Test
export DJANGO_SECRET_KEY=ThisIsAnExampleKeyForTestPurposeOnly
export OIDC_OP_JWKS_ENDPOINT=/endpoint-for-test-purpose-only
export DB_HOST=127.0.0.1
export DB_NAME=impress
export DB_USER=dinum
export DB_PASSWORD=pass
export DB_PORT=5432
export STORAGES_STATICFILES_BACKEND=django.contrib.staticfiles.storage.StaticFilesStorage
export AWS_S3_ENDPOINT_URL=http://127.0.0.1:9000
export AWS_S3_ACCESS_KEY_ID=impress
export AWS_S3_SECRET_ACCESS_KEY=password
export AWS_S3_REGION_NAME=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_STORAGE_BUCKET_NAME=impress-media-storage"""

# Identical in run.sh / test-run.sh / fix-run.sh so the three stages are comparable.
TEST_CMD = (
    "python -m pytest --color=no -p no:cacheprovider --continue-on-collection-errors"
)


class DocsImageBase(Image):
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
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>` rather than a shared `base`: the Dockerfile QC
        # contract requires the PR layer to inherit
        # `mswebench/<org>_m_<repo>:base-pr-<N>`, and a shared tag hides a real
        # hazard -- DockerfileEnhancer bakes one BASE_COMMIT into this image, so a
        # reused `base` stays pinned to whichever PR built it first and any later
        # PR whose base commit is unreachable from that sha dies in prepare.sh.
        # Costs one base image per PR instead of one per repo; deliberate.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    gettext \\
    git \\
    gnupg \\
    libmagic1 \\
    libpq-dev \\
    make \\
    media-types \\
    nodejs \\
    npm \\
    pandoc \\
    postgresql \\
    postgresql-client \\
    postgresql-contrib \\
    procps \\
    shared-mime-info \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g yarn@1.22.22

RUN set -eux; \\
    minio_release=RELEASE.2025-09-07T16-13-09Z; \\
    mc_release=RELEASE.2025-08-13T08-35-41Z; \\
    arch="$(dpkg --print-architecture)"; \\
    case "$arch" in \\
        amd64) minio_arch=amd64 ;; \\
        arm64) minio_arch=arm64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://dl.min.io/server/minio/release/linux-$minio_arch/archive/minio.$minio_release" -o /usr/local/bin/minio; \\
    curl -fsSL "https://dl.min.io/client/mc/release/linux-$minio_arch/archive/mc.$mc_release" -o /usr/local/bin/mc; \\
    chmod +x /usr/local/bin/minio /usr/local/bin/mc; \\
    /usr/local/bin/minio --version; \\
    /usr/local/bin/mc --version

RUN mkdir -p /data/media /data/static /data/minio && chmod -R 777 /data

WORKDIR /home/

{code}

{self.clear_env}

"""


class DocsImageDefault(Image):
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
        return DocsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "start_services.sh",
                """#!/bin/bash
# Starts the backing services the Django backend test-suite needs:
# PostgreSQL (django db) and MinIO (S3 compatible media storage).
set -e

service postgresql start

for _ in $(seq 1 60); do
    if pg_isready -q -h 127.0.0.1 -p 5432; then
        break
    fi
    sleep 1
done

su postgres -c "psql -c \\"CREATE ROLE dinum WITH LOGIN SUPERUSER CREATEDB PASSWORD 'pass'\\"" || true
su postgres -c "psql -c \\"CREATE DATABASE impress OWNER dinum\\"" || true

mkdir -p /data/media /data/static /data/minio

if ! pgrep -x minio > /dev/null 2>&1; then
    MINIO_ROOT_USER=impress \\
    MINIO_ROOT_PASSWORD=password \\
    MINIO_ACCESS_KEY=impress \\
    MINIO_SECRET_KEY=password \\
    nohup minio server --address :9000 --console-address :9001 /data/minio \\
        > /tmp/minio.log 2>&1 &
fi

for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:9000/minio/health/live > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

mc alias set impress http://127.0.0.1:9000 impress password > /dev/null
mc mb --ignore-existing impress/impress-media-storage > /dev/null
mc version enable impress/impress-media-storage > /dev/null

echo "start_services: PostgreSQL and MinIO are ready"

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{env_exports}

mkdir -p /data/media /data/static /data/minio
chmod -R 777 /data

# Let the test-suite reach the local cluster without a password.
for hba in /etc/postgresql/*/main/pg_hba.conf; do
    sed -i 's/peer/trust/g; s/scram-sha-256/trust/g; s/md5/trust/g' "$hba"
done

# NOTE: PostgreSQL and MinIO are deliberately NOT started here. A multi-arch
# `docker buildx build --platform linux/amd64,linux/arm64` runs both platform
# legs concurrently in a shared network namespace, so binding 5432/9000 during
# the build makes the second leg die with "Address already in use". Nothing
# below needs a database or object store, and run.sh / test-run.sh / fix-run.sh
# each call start_services.sh (which creates the role and database) at runtime.

cd /home/{pr.repo}/src/backend
pip install --no-cache-dir --upgrade pip setuptools wheel || true
pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir ".[dev]" || true
# Runtime dependencies added by this pull request. They are installed up-front so
# that the environment is strictly identical in the three test stages.
pip install --no-cache-dir "beautifulsoup4==4.12.3" "y-py==0.6.2" || true

python manage.py compilemessages || true

# The invitation e-mail templates are generated from the MJML sources.
cd /home/{pr.repo}/src/mail
yarn install --frozen-lockfile && yarn build || true

""".format(pr=self.pr, env_exports=ENV_EXPORTS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{env_exports}

bash /home/start_services.sh

cd /home/{pr.repo}/src/backend
{test_cmd}

""".format(pr=self.pr, env_exports=ENV_EXPORTS, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{env_exports}

bash /home/start_services.sh

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

cd /home/{pr.repo}/src/backend
{test_cmd}

""".format(pr=self.pr, env_exports=ENV_EXPORTS, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{env_exports}

bash /home/start_services.sh

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch fix.patch failed" >&2
    exit 1
fi

cd /home/{pr.repo}/src/backend
{test_cmd}

""".format(pr=self.pr, env_exports=ENV_EXPORTS, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("suitenumerique", "docs")
class Docs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DocsImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI colour codes first, then carriage returns from the progress bar.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        clean_log = clean_log.replace("\r", "\n")

        statuses = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"

        # Verbose line: "core/tests/documents/test_x.py::test_y[param] PASSED [ 12%]"
        re_verbose = re.compile(rf"^(?P<name>\S+::\S.*?)\s+(?P<status>{statuses})\b")
        # Short summary line: "FAILED core/tests/documents/test_x.py::test_y - Error"
        re_summary = re.compile(
            rf"^(?P<status>{statuses})\s+(?P<name>\S+::\S.*?)(?:\s+-\s.*)?$"
        )

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_summary.match(line) or re_verbose.match(line)
            if not match:
                continue

            name = match.group("name").strip()
            if "::" not in name:
                continue

            status = match.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Enforce the TestResult invariants: the three sets must be disjoint.
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
