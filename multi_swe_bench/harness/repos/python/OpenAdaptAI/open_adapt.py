import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PY_IMAGE = "python:3.10-slim-bookworm"
_BASE_APT = (
    "git ca-certificates build-essential tesseract-ocr xvfb xauth"
    " libgl1 libglib2.0-0 libsm6 libxext6 libxrender1"
)
_SPACY_MODEL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_trf-3.5.0/en_core_web_trf-3.5.0-py3-none-any.whl"
)
_TEST_CMD = (
    "xvfb-run -a python -m pytest tests -v --tb=short"
    " -p no:cacheprovider -p oa_linux_shim --continue-on-collection-errors"
)

_PREPARE_TEMPLATE = """set -e
PR_ORG="__PR_ORG__"
PR_REPO="__REPO__"
PR_NUMBER="__PR_NUMBER__"
STATUS_LOG=/home/__REPO__/prepare-status.log
: > "$STATUS_LOG"
cd /home/__REPO__
echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"
echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"

cat > /home/constraints.txt <<'CON_EOF'
numpy==1.24.4
torch==2.0.0
transformers==4.29.2
tokenizers==0.13.3
spacy==3.5.4
spacy-transformers==1.2.5
Pillow==9.5.0
protobuf==3.20.3
pyinstaller==5.13.2
sphinx==6.2.1
CON_EOF

cat > /home/extras.txt <<'EXT_EOF'
__SPACY_MODEL__
EXT_EOF

if grep -qi qemu /proc/self/maps 2>/dev/null; then
  STRICT=0
else
  STRICT=1
fi
echo "PREPARE_STRICT=$STRICT" >> "$STATUS_LOG"

python -m pip install --no-cache-dir "pip<24.1" "setuptools<70" wheel

cat > /home/apply_patches.sh <<'AP_EOF'
set -e
cd /home/__REPO__
PATCHES="$@"
BIN_PATHS=$(grep -hoE "^Binary files [^ ]+ and b/[^ ]+ differ" $PATCHES | sed -E "s|^Binary files [^ ]+ and b/||; s| differ$||" | sort -u)
EXCLUDES=""
for BP in $BIN_PATHS; do
  EXCLUDES="$EXCLUDES --exclude=$BP"
done
git apply --whitespace=nowarn $EXCLUDES $PATCHES
for BP in $BIN_PATHS; do
  if [ -f "/home/binassets/$BP" ]; then
    mkdir -p "$(dirname "$BP")"
    cp "/home/binassets/$BP" "$BP"
  fi
done
AP_EOF

mkdir -p /opt/pgw-shim/pygetwindow
cat > /opt/pgw-shim/pygetwindow/__init__.py <<'PGW_EOF'
def __getattr__(name):
    def _unsupported(*args, **kwargs):
        raise NotImplementedError(
            "pygetwindow is not available on this platform: " + name
        )

    return _unsupported
PGW_EOF

mkdir -p /opt/oa-shim
cat > /opt/oa-shim/oa_linux_shim.py <<'OAS_EOF'
from openadapt import utils

utils.override_double_click_interval_seconds(0.5)
utils.get_double_click_distance_pixels = lambda: 10
OAS_EOF

mkdir -p /home/binassets
BIN_PATHS=$(grep -hoE "^Binary files [^ ]+ and b/[^ ]+ differ" /home/test.patch /home/fix.patch | sed -E "s|^Binary files [^ ]+ and b/||; s| differ$||" | sort -u)
echo "PREPARE_BIN_PATHS=$BIN_PATHS" >> "$STATUS_LOG"
set +e
if [ -n "$BIN_PATHS" ]; then
  rm -rf /tmp/prsrc
  git clone --filter=blob:none --no-checkout --quiet "https://github.com/$PR_ORG/$PR_REPO.git" /tmp/prsrc
  git -C /tmp/prsrc fetch --filter=blob:none --quiet origin "refs/pull/$PR_NUMBER/head"
  for BP in $BIN_PATHS; do
    mkdir -p "/home/binassets/$(dirname "$BP")"
    if git -C /tmp/prsrc show "FETCH_HEAD:$BP" > "/home/binassets/$BP" 2>/dev/null; then
      echo "fetched $BP"
    else
      rm -f "/home/binassets/$BP"
      echo "could not fetch $BP"
    fi
  done
fi
BIN_FETCH_EXIT=$?
set -e
echo "PREPARE_BIN_FETCH_EXIT=$BIN_FETCH_EXIT" >> "$STATUS_LOG"

bash /home/apply_patches.sh /home/test.patch /home/fix.patch

sed -i -E 's|^-e +(git\\+)|\\1|' requirements.txt
sed -i -E 's|(git\\+[^ ]+)#egg=[A-Za-z0-9_.-]+|\\1|' requirements.txt
echo "PREPARE_VCS_LINES=$(grep -c 'git+' requirements.txt || true)" >> "$STATUS_LOG"

BASE_DATE=$(git show -s --format=%cI HEAD)
echo "PREPARE_BASE_DATE=$BASE_DATE" >> "$STATUS_LOG"
set +e
VCS_IDX=0
for VCS_URL in $(grep -oE "git\\+https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\\.git)?" requirements.txt | sed "s|^git+||" | sort -u); do
  VCS_IDX=$((VCS_IDX + 1))
  VCS_CLEAN=${VCS_URL%.git}
  rm -rf "/tmp/vcs$VCS_IDX"
  if git clone --filter=blob:none --no-checkout --quiet "$VCS_CLEAN.git" "/tmp/vcs$VCS_IDX"; then
    VCS_SHA=$(git -C "/tmp/vcs$VCS_IDX" rev-list -1 --before="$BASE_DATE" HEAD)
    if [ -n "$VCS_SHA" ]; then
      sed -i "s|$VCS_URL|$VCS_CLEAN.git@$VCS_SHA|g" requirements.txt
      echo "pinned $VCS_CLEAN -> $VCS_SHA"
    fi
  fi
done
VCS_PIN_EXIT=$?
set -e
echo "PREPARE_VCS_PIN_EXIT=$VCS_PIN_EXIT" >> "$STATUS_LOG"
grep "git+" requirements.txt >> "$STATUS_LOG" || true

pip_retry() {
  ATTEMPT=1
  while [ "$ATTEMPT" -le 3 ]; do
    python -m pip install --no-cache-dir --retries 10 "$@"
    RC=$?
    if [ "$RC" = "0" ]; then
      return 0
    fi
    echo "pip attempt $ATTEMPT failed (rc=$RC), clearing partial downloads and retrying"
    rm -rf /tmp/pip-unpack-* /tmp/pip-req-build-* /tmp/pip-install-* 2>/dev/null
    ATTEMPT=$((ATTEMPT + 1))
    sleep 5
  done
  return "$RC"
}

set +e
TORCH_SPEC=$(grep -iE "^torch==" requirements.txt | head -1)
echo "PREPARE_TORCH_SPEC=$TORCH_SPEC" >> "$STATUS_LOG"
if [ -n "$TORCH_SPEC" ]; then
  pip_retry --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple "$TORCH_SPEC"
  TORCH_CPU_EXIT=$?
  echo "PREPARE_TORCH_CPU_EXIT=$TORCH_CPU_EXIT" >> "$STATUS_LOG"
fi
pip_retry -c /home/constraints.txt -r requirements.txt
PIP_REQ=$?
pip_retry -c /home/constraints.txt -r /home/extras.txt
PIP_EXTRA=$?
set -e

git reset --hard
git clean -fd

echo "PREPARE_PIP_REQ=$PIP_REQ" >> "$STATUS_LOG"
echo "PREPARE_PIP_EXTRA=$PIP_EXTRA" >> "$STATUS_LOG"
test "$PIP_REQ" = "0"
test "$PIP_EXTRA" = "0"

set +e
export PYTHONPATH=/opt/pgw-shim:/opt/oa-shim
python -c "import openadapt.config; print('config ok')"
CONFIG_EXIT=$?
xvfb-run -a python -m pytest tests --collect-only -q -p no:cacheprovider -p oa_linux_shim --continue-on-collection-errors > /home/collect.log 2>&1
COLLECT_EXIT=$?
set -e

tail -n 80 /home/collect.log || true
COLLECTED=$(grep -cE "::" /home/collect.log 2>/dev/null || true)
COLLECTED=${COLLECTED:-0}
echo "PREPARE_CONFIG_EXIT=$CONFIG_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECT_EXIT=$COLLECT_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECTED=$COLLECTED" >> "$STATUS_LOG"

if [ "$CONFIG_EXIT" -ge 128 ]; then
  echo "PREPARE_SIGNAL_DEATH=1" >> "$STATUS_LOG"
else
  test "$CONFIG_EXIT" = "0"
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
export MPLBACKEND=Agg
export PYTHONPATH=/opt/pgw-shim:/opt/oa-shim
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_TELEMETRY=1
__APPLY____TEST_CMD__
"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__REPO__", pr.repo)
        .replace("__PR_ORG__", pr.org)
        .replace("__PR_NUMBER__", str(pr.number))
        .replace("__SPACY_MODEL__", _SPACY_MODEL)
        .replace("__TEST_CMD__", _TEST_CMD)
    )


def _run_script(pr: PullRequest, apply_cmd: str) -> str:
    return _render(_RUN_TEMPLATE, pr).replace("__APPLY__", apply_cmd)


class OpenAdaptImageBase(Image):
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
    PIP_DEFAULT_TIMEOUT=120 \\
    MPLBACKEND=Agg \\
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


class OpenAdaptImageDefault(Image):
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
        return OpenAdaptImageBase(self.pr, self._config)

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
            File(".", "prepare.sh", _render(_PREPARE_TEMPLATE, self.pr)),
            File(".", "run.sh", _run_script(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(self.pr, "bash /home/apply_patches.sh /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    self.pr,
                    "bash /home/apply_patches.sh /home/test.patch /home/fix.patch\n",
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


@Instance.register("OpenAdaptAI", "OpenAdapt")
class OPEN_ADAPT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenAdaptImageDefault(self.pr, self._config)

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
