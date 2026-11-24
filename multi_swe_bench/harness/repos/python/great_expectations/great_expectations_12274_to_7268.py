import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_NAME = "great_expectations"
REPO_URL = "https://github.com/great-expectations/great_expectations.git"
PY_IMAGE = "python:3.10-bookworm"


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

    def dependency(self) -> Union[str, "Image"]:
        return PY_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return r"""# syntax=docker/dockerfile:1.6

FROM [[PY_IMAGE]]

ARG TARGETARCH
ARG REPO_URL="[[REPO_URL]]"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=180

LABEL org.opencontainers.image.title="great-expectations/great_expectations" \
      org.opencontainers.image.description="great-expectations/great_expectations Docker image" \
      org.opencontainers.image.source="https://github.com/great-expectations/great_expectations" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    pkg-config \
    libpq-dev \
    locales \
    sudo \
    wget \
    && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

RUN /usr/local/bin/python3 -m pip install --no-cache-dir uv \
    && uv venv --seed --python /usr/local/bin/python3 /opt/venv \
    && python --version \
    && python -m pip --version

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/[[REPO_NAME]]

WORKDIR /home/[[REPO_NAME]]

CMD ["/bin/bash"]
""".replace("[[PY_IMAGE]]", PY_IMAGE).replace("[[REPO_URL]]", REPO_URL).replace(
            "[[REPO_NAME]]", REPO_NAME
        )


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

    def dependency(self) -> Union[str, "Image"]:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        base_sha = self.pr.base.sha
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
                r"""#!/bin/bash
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
                "prepare.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[BASE_SHA]]
bash /home/check_git_changes.sh

ERA_CUTOFF="$(git show -s --format=%cI HEAD)"
echo "base commit: $(git rev-parse HEAD)"
echo "era cutoff : ${ERA_CUTOFF}"

REQ_FILE="reqs/requirements-dev-lite.txt"
test -f "${REQ_FILE}"

if git grep -qE '^(import pkg_resources|from pkg_resources)' -- great_expectations; then
    SETUPTOOLS_SPEC="setuptools<67.3"
else
    SETUPTOOLS_SPEC="setuptools"
fi
echo "setuptools : ${SETUPTOOLS_SPEC}"

CONTRIB_ARGS=""
for d in $(grep -E '^\+\+\+ b/contrib/[^/]+/' /home/test.patch | sed -E 's#^\+\+\+ b/(contrib/[^/]+)/.*#\1#' | sort -u); do
    if [ -f "$d/requirements.txt" ]; then
        CONTRIB_ARGS="$CONTRIB_ARGS -r $d/requirements.txt"
    fi
    if [ -f "$d/setup.py" ] || [ -f "$d/pyproject.toml" ]; then
        CONTRIB_ARGS="$CONTRIB_ARGS -e $d"
    fi
done
echo "contrib    : ${CONTRIB_ARGS:-none}"

CONSTRAINT_ARGS=""
if [ -f constraints-dev.txt ]; then
    CONSTRAINT_ARGS="-c constraints-dev.txt"
fi

uv pip install --exclude-newer "${ERA_CUTOFF}" ${CONSTRAINT_ARGS} \
    --reinstall-package setuptools "${SETUPTOOLS_SPEC}" \
    -e . -r "${REQ_FILE}" ${CONTRIB_ARGS}

cd /
python --version
python -c "import great_expectations; print('great_expectations', great_expectations.__version__)"
python -c "import pandas, numpy, sqlalchemy, pytest_mock, pyfakefs, freezegun, moto"
python -m pytest --version
python -c "import locale; locale.setlocale(locale.LC_ALL, 'en_US.UTF-8'); print('locale en_US.UTF-8 OK')"
echo "DEPS_OK"
""".replace("[[REPO_NAME]]", REPO_NAME).replace("[[BASE_SHA]]", base_sha),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export TZ=UTC
export GE_USAGE_STATS=False
export GX_ANALYTICS_ENABLED=false
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]

TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*|[^/]*_test|tests)\.py$' \
    | sort -u)

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    python -m pytest $TEST_TARGETS -v \
        -o cache_dir=/tmp/.pytest_cache \
        -p no:randomly \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        -W 'ignore:_SixMetaPathImporter.:ImportWarning' \
        --override-ini='python_files=test_*.py *_test.py tests.py'
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi
""".replace("[[REPO_NAME]]", REPO_NAME),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export TZ=UTC
export GE_USAGE_STATS=False
export GX_ANALYTICS_ENABLED=false
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch

TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*|[^/]*_test|tests)\.py$' \
    | sort -u)

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    python -m pytest $TEST_TARGETS -v \
        -o cache_dir=/tmp/.pytest_cache \
        -p no:randomly \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        -W 'ignore:_SixMetaPathImporter.:ImportWarning' \
        --override-ini='python_files=test_*.py *_test.py tests.py'
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi
""".replace("[[REPO_NAME]]", REPO_NAME),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export TZ=UTC
export GE_USAGE_STATS=False
export GX_ANALYTICS_ENABLED=false
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*|[^/]*_test|tests)\.py$' \
    | sort -u)

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    python -m pytest $TEST_TARGETS -v \
        -o cache_dir=/tmp/.pytest_cache \
        -p no:randomly \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        -W 'ignore:_SixMetaPathImporter.:ImportWarning' \
        --override-ini='python_files=test_*.py *_test.py tests.py'
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi
""".replace("[[REPO_NAME]]", REPO_NAME),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = r"""FROM [[BASE_IMAGE]]

ARG BASE_COMMIT="[[BASE_SHA]]"

[[COPY_COMMANDS]]
RUN bash /home/prepare.sh

WORKDIR /home/[[REPO_NAME]]

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""
        dockerfile_content = (
            dockerfile_content.replace("[[BASE_IMAGE]]", f"{name}:{tag}")
            .replace("[[BASE_SHA]]", self.pr.base.sha)
            .replace("[[COPY_COMMANDS]]", copy_commands)
            .replace("[[REPO_NAME]]", REPO_NAME)
        )

        return dockerfile_content


@Instance.register("great-expectations", "great_expectations")
class GREAT_EXPECTATIONS(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        verbose = re.compile(
            r"^(\S+?::\S.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?: \(.*\))?\s*(?:\[\s*\d+%\].*)?$"
        )
        collect_error = re.compile(r"^ERROR (\S+\.py)(?: - .*)?$")

        for line in log.splitlines():
            line = line.strip()
            m = verbose.match(line)
            if m:
                name, status = m.group(1), m.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            m = collect_error.match(line)
            if m:
                failed_tests.add(m.group(1))

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
