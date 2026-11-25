import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_BASE_IMAGE = "python:3.12-bookworm"


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
        return PYTHON_BASE_IMAGE

    def image_tag(self) -> str:
        return "base-pr-12017-14731"

    def workdir(self) -> str:
        return "base-pr-12017-14731"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo_name = self.pr.repo
        org_name = self.pr.org

        return r"""# syntax=docker/dockerfile:1.6

FROM [[PYTHON_IMAGE]]

ARG TARGETARCH
ARG REPO_URL="https://github.com/[[ORG_NAME]]/[[REPO_NAME]].git"
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="[[ORG_NAME]]/[[REPO_NAME]]" \
      org.opencontainers.image.description="[[ORG_NAME]]/[[REPO_NAME]] Docker image" \
      org.opencontainers.image.source="https://github.com/[[ORG_NAME]]/[[REPO_NAME]]" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git make sudo wget \
    build-essential pkg-config \
    libjpeg-dev libpng-dev libtiff-dev libwebp-dev libfreetype6-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/[[REPO_NAME]]

WORKDIR /home/[[REPO_NAME]]

CMD ["/bin/bash"]
""".replace("[[PYTHON_IMAGE]]", PYTHON_BASE_IMAGE).replace(
            "[[ORG_NAME]]", org_name
        ).replace("[[REPO_NAME]]", repo_name)


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "resolve_django.py",
                r"""import re

# The repo's install_requires carries a deliberately wide Django range
# (e.g. "Django>=4.2,<6.0"), which lets pip resolve a Django newer than the
# commit actually supports. The repo's own tox matrix names the Django
# versions this commit is tested against, so the highest concrete entry there
# is the era-correct ceiling. Emit it as a pip specifier; pip then picks the
# newest release at or below that ceiling which supports this interpreter.
versions = []
try:
    body = open("tox.ini", encoding="utf-8", errors="replace").read()
except OSError:
    body = ""

for match in re.finditer(r"^\s*dj\d+\s*:\s*Django\s*~=\s*(\d+)\.(\d+)", body, re.M):
    versions.append((int(match.group(1)), int(match.group(2))))

if not versions:
    try:
        ci = open(".github/workflows/test.yml", encoding="utf-8", errors="replace").read()
    except OSError:
        ci = ""
    for match in re.finditer(r"Django>=(\d+)\.(\d+)\s*,\s*<", ci):
        versions.append((int(match.group(1)), int(match.group(2))))

if versions:
    major, minor = max(versions)
    print("Django<%d.%d" % (major, minor + 1))
""",
            ),
            File(
                ".",
                "resolve_new_deps.py",
                r"""import re

# A fix patch may introduce a new runtime dependency and declare it in the
# project's own metadata (e.g. a new "django-ninja" entry under
# [project] dependencies). The image installs dependencies at the base
# commit, so such a package is absent when the fix patch's code imports it and
# the fix stage dies during django.setup() with ModuleNotFoundError.
# Emit the requirements the patch itself declares so the environment matches
# what the PR says it needs. Only the runtime `dependencies = [` table is
# read: optional extras hold linters and docs tooling that tests do not need.
try:
    patch = open("/home/fix.patch", encoding="utf-8", errors="replace").read()
except OSError:
    patch = ""

requirements = []
in_metadata = False
hunk_context = ""
for raw_line in patch.splitlines():
    if raw_line.startswith("diff --git"):
        in_metadata = "pyproject.toml" in raw_line or "setup.py" in raw_line
        hunk_context = ""
        continue
    if not in_metadata:
        continue
    if raw_line.startswith("@@"):
        hunk_context = raw_line.split("@@")[-1].strip()
        continue
    if not hunk_context.startswith("dependencies = ["):
        continue
    added = re.match(r'^\+\s*"([^"]+)",?\s*$', raw_line)
    if added:
        requirements.append(added.group(1))

seen = set()
for requirement in requirements:
    if requirement not in seen:
        seen.add(requirement)
        print(requirement)
""",
            ),
            File(
                ".",
                "report_tests.py",
                r"""import os
import xml.etree.ElementTree as ET

path = "/home/results.xml"
root = None
if os.path.exists(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        root = None

if root is not None:
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname") or ""
        name = (testcase.get("name") or "").replace("\r", " ").replace("\n", " ")
        located = (testcase.get("file") or "").replace("\\", "/")
        holder = ""
        if located:
            file_path = located
            module = located[:-3].replace("/", ".") if located.endswith(".py") else ""
            if module and classname.startswith(module + "."):
                holder = classname[len(module) + 1:]
        else:
            parts = classname.split(".")
            if len(parts) > 1 and parts[-1][:1].isupper():
                holder = parts[-1]
                parts = parts[:-1]
            file_path = "/".join(parts) + ".py"

        status = "PASSED"
        for child in testcase:
            if child.tag in ("failure", "error"):
                status = "FAILED"
                break
            if child.tag == "skipped":
                status = "SKIPPED"

        node_id = file_path + "::" + (holder + "::" if holder else "") + name
        print("TESTCASE " + node_id + " " + status)
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

export DJANGO_SETTINGS_MODULE=wagtail.test.settings

DJANGO_PIN=$(python -I /home/resolve_django.py)
echo "era-correct Django ceiling from tox matrix: ${DJANGO_PIN:-<none>}"

python -m pip install --upgrade pip setuptools wheel

# beautifulsoup4 is capped ("<5") but its soupsieve dependency is not, so pip is
# free to pull a post-era soupsieve. soupsieve 4.0 (released 2026-11-03, after
# every base commit in this shard) implements the HTML5 <template> content
# model, under which a child combinator no longer reaches into a <template>.
# The suite's own selectors rely on the earlier behaviour, e.g.
# `header_buttons.select_one(":is(template > a)")`, which silently returns None
# on soupsieve 4 and fails tests that the commit passes. Keep the era behaviour.
python -m pip install -e ".[testing]" ${DJANGO_PIN:+"$DJANGO_PIN"} "soupsieve<4"
python -m pip install "pytest>=7,<9" "pytest-django>=4.5"

python -I /home/resolve_new_deps.py > /home/new_deps.txt
if [ -s /home/new_deps.txt ]; then
    echo "runtime dependencies declared by this PR's fix patch:"
    cat /home/new_deps.txt
    python -m pip install -r /home/new_deps.txt
else
    echo "this PR's fix patch declares no new runtime dependency"
fi

python --version
python -c "import wagtail; print('wagtail', wagtail.VERSION)"
python -c "import django; print('django', django.VERSION)"
python -c "import pytest_django, freezegun, factory, bs4, jinja2"
python -m pytest --version
cd /home/[[REPO_NAME]]
python -c "import django; django.setup(); from django.apps import apps; assert apps.ready; print('django apps ready')"
echo "DEPS_OK"
""".replace("[[REPO_NAME]]", repo_name).replace("[[BASE_SHA]]", base_sha),
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
export DJANGO_SETTINGS_MODULE=wagtail.test.settings
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]

rm -f /home/results.xml

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
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace("[[REPO_NAME]]", repo_name),
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
export DJANGO_SETTINGS_MODULE=wagtail.test.settings
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch

rm -f /home/results.xml

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
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace("[[REPO_NAME]]", repo_name),
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
export DJANGO_SETTINGS_MODULE=wagtail.test.settings
export OMP_NUM_THREADS=1

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

rm -f /home/results.xml

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
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo_name = self.pr.repo
        base_sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return r"""FROM [[BASE_IMAGE]]

ARG BASE_COMMIT="[[BASE_SHA]]"

WORKDIR /home/[[REPO_NAME]]

[[COPY_COMMANDS]]
RUN bash /home/prepare.sh

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
""".replace("[[BASE_IMAGE]]", f"{name}:{tag}").replace(
            "[[BASE_SHA]]", base_sha
        ).replace("[[REPO_NAME]]", repo_name).replace(
            "[[COPY_COMMANDS]]", copy_commands
        )


@Instance.register("wagtail", "wagtail")
class WAGTAIL(Instance):
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
        pattern = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")
        in_detail = False

        for line in log.splitlines():
            stripped = line.strip()
            if stripped.startswith("===== BEGIN TEST DETAIL ====="):
                in_detail = True
                continue
            if stripped.startswith("===== END TEST DETAIL ====="):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = pattern.match(stripped)
            if not match:
                continue

            if match.group(2) == "PASSED":
                passed_tests.add(match.group(1))
            elif match.group(2) == "FAILED":
                failed_tests.add(match.group(1))
            else:
                skipped_tests.add(match.group(1))

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
