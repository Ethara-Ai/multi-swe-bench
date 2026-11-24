import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "python:3.11-bookworm"
BASE_TAG = "base-5662_to_6474"

_ENV = """\
export CI=true
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export HF_HOME=/opt/hf-cache
export UV_LINK_MODE=copy
export UV_PYTHON_DOWNLOADS=automatic
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
export UV_CACHE_DIR=/root/.cache/uv
export UV_HTTP_TIMEOUT=120
export UV_HTTP_RETRIES=5
export CMAKE_ARGS="-DGGML_NATIVE=OFF"
export PATH="/root/.local/bin:$PATH"
"""

_PREPARE_CMD = """\
PY_VERSION="$(tr -d '[:space:]' < .python-version)"
test -n "$PY_VERSION"
WF=.github/workflows/test.yml
test -f "$WF"
API_SYNC="$(grep -m1 -oE 'uv sync --frozen --all-extras[^#]*' "$WF" | sed -E 's/[[:space:]]+$//')"
test -n "$API_SYNC"
read -r -a API_ARGS <<< "${API_SYNC#uv sync}"

msb_sync() {
  for attempt in 1 2 3; do
    uv sync --python "$PY_VERSION" --inexact "$@" && return 0
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: uv sync failed 3 times"; exit 1; }
    sleep 30
  done
}

uv python install "$PY_VERSION"
rm -rf .venv
if [ -f hindsight-embed/pyproject.toml ]; then
  msb_sync --frozen --index-strategy unsafe-best-match --package hindsight-embed
fi
if [ -f hindsight-clients/python/pyproject.toml ]; then
  msb_sync --frozen --index-strategy unsafe-best-match --package hindsight-client --extra test
fi
msb_sync "${API_ARGS[@]}" --package hindsight-api-slim
uv cache prune --ci

MODEL_CALLS="$(grep -oE "(SentenceTransformer|CrossEncoder)\\('[^']+'\\)" "$WF" | sort -u | paste -sd';' -)"
test -n "$MODEL_CALLS"
for attempt in 1 2 3; do
  .venv/bin/python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; ${MODEL_CALLS}" && break
  [ "$attempt" -lt 3 ] || { echo "prepare.sh: model download failed 3 times"; exit 1; }
  sleep 30
done
chmod -R a+rwX "$HF_HOME"

.venv/bin/python --version
.venv/bin/python --version 2>&1 | grep -q "Python ${PY_VERSION}\\."
.venv/bin/python -c "import hindsight_api, hindsight_api.pg0, pg0, sentence_transformers, pytest, pytest_asyncio, xdist, pytest_timeout"
.venv/bin/pytest --version
id -u msb
echo DEPS_OK"""

_TEST_CMD = """\
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
chown -R msb:msb /home/hindsight
RC=0
PYTEST_OPTS=(
  -p no:cacheprovider
  -p no:randomly
  --continue-on-collection-errors
  -n 0
  -rA
  -v
  --tb=short
  --color=no
)
API=()
CLIENT=()
EMBED=()
while read -r f; do
  b="$(basename "$f")"
  case "$b" in
    test_*.py|*_test.py) ;;
    *) continue ;;
  esac
  [ -f "$f" ] || continue
  case "$f" in
    hindsight-api-slim/tests/*) API+=("${f#hindsight-api-slim/}") ;;
    hindsight-clients/python/tests/*) CLIENT+=("${f#hindsight-clients/python/}") ;;
    hindsight-embed/tests/*) EMBED+=("${f#hindsight-embed/}") ;;
    *) continue ;;
  esac
done < <(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u)

if [ "${#API[@]}" -gt 0 ]; then
  echo "MSB_GROUP|hindsight-api-slim|${#API[@]}"
  (cd hindsight-api-slim && runuser -u msb -- ../.venv/bin/python -m pytest "${PYTEST_OPTS[@]}" -m "not hs_llm_mat and not hs_llm_core" "${API[@]}") || RC=$?
fi
if [ "${#CLIENT[@]}" -gt 0 ]; then
  echo "MSB_GROUP|hindsight-clients/python|${#CLIENT[@]}"
  (cd hindsight-clients/python && runuser -u msb -- ../../.venv/bin/python -m pytest "${PYTEST_OPTS[@]}" "${CLIENT[@]}") || RC=$?
fi
if [ "${#EMBED[@]}" -gt 0 ]; then
  echo "MSB_GROUP|hindsight-embed|${#EMBED[@]}"
  (cd hindsight-embed && runuser -u msb -- ../.venv/bin/python -m pytest "${PYTEST_OPTS[@]}" "${EMBED[@]}") || RC=$?
fi
exit $RC"""

_CHECK_GIT_CHANGES = """\
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


class Hindsight5662To6474ImageBase(Image):
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
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"
ARG UV_VERSION="0.10.9"

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
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    PIP_ROOT_USER_ACTION=ignore \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONUNBUFFERED=1 \\
    UV_LINK_MODE=copy \\
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \\
    PATH=/root/.local/bin:$PATH \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl tar gzip xz-utils unzip make build-essential pkg-config \\
        libicu72 libxml2 libssl3 libreadline8 zlib1g libuuid1 locales procps \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir "uv==${{UV_VERSION}}" && \\
    uv --version | grep -q "uv ${{UV_VERSION}}"

RUN useradd -m -s /bin/bash msb && \\
    git config --system --add safe.directory '*' && \\
    git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class Hindsight5662To6474ImageDefault(Image):
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
        return Hindsight5662To6474ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK

PREPARE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_CMD", _TEST_CMD)
                .replace("PREPARE_CMD", _PREPARE_CMD)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
    git -c pack.threads=2 -c pack.windowMemory=512m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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

{self.clear_env}
"""


@Instance.register("vectorize-io", "hindsight_5662_to_6474")
class HINDSIGHT_5662_TO_6474(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Hindsight5662To6474ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        verbose_re = re.compile(
            r"^(\S+\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: \(.*?\))?(?:\s+\[\s*\d+%\])?$"
        )
        summary_re = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+\.py(?:::\S+)?)(?: - .*)?$")

        def record(test: str, status: str) -> None:
            if status in ("PASSED", "XFAIL"):
                passed_tests.add(test)
            elif status in ("FAILED", "ERROR", "XPASS"):
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        group = ""
        for raw in log.splitlines():
            line = raw.strip()
            if line.startswith("MSB_GROUP|"):
                group = line.split("|")[1] + "/"
                continue
            m = verbose_re.match(line)
            if m:
                record(group + m.group(1), m.group(2))
                continue
            m = summary_re.match(line)
            if m:
                record(group + m.group(2), m.group(1))

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
