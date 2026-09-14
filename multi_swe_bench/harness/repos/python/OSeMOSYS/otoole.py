import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PY_IMAGE = "python:3.10-slim-bookworm"
_BASE_APT = "git ca-certificates build-essential graphviz"
_TEST_CMD = (
    "python -m pytest tests -v --override-ini=addopts="
    " -p no:cacheprovider --continue-on-collection-errors"
)

_PREPARE_TEMPLATE = """set -e
STATUS_LOG=/home/__REPO__/prepare-status.log
: > "$STATUS_LOG"
cd /home/__REPO__
echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"
echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"

cat > /home/constraints.txt <<'CON_EOF'
numpy==1.24.4
pandas==1.5.3
pydantic==1.10.13
frictionless==4.40.8
datapackage==1.15.2
openpyxl==3.1.2
xlrd==2.0.1
networkx==2.8.8
pyyaml==6.0.1
amply==0.1.5
flatten-dict==0.4.2
pandas-datapackage-reader==0.18.0
pydot==1.4.2
pytest==7.4.4
pytest-cov==4.1.0
CON_EOF

export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
echo "PREPARE_SCM_VERSION=$SETUPTOOLS_SCM_PRETEND_VERSION" >> "$STATUS_LOG"

python -m pip install --no-cache-dir --upgrade pip setuptools setuptools_scm wheel

set +e
python -m pip install --no-cache-dir --retries 10 -c /home/constraints.txt -r requirements.txt
PIP_REQ=$?
python -m pip install --no-cache-dir --retries 10 -c /home/constraints.txt pytest pytest-cov
PIP_TEST=$?
python -m pip install --no-cache-dir --no-deps -e .
PIP_PKG=$?
set -e

echo "PREPARE_PIP_REQ=$PIP_REQ" >> "$STATUS_LOG"
echo "PREPARE_PIP_TEST=$PIP_TEST" >> "$STATUS_LOG"
echo "PREPARE_PIP_PKG=$PIP_PKG" >> "$STATUS_LOG"
test "$PIP_REQ" = "0"
test "$PIP_TEST" = "0"
test "$PIP_PKG" = "0"

set +e
python -c "import otoole; print('import ok')"
IMPORT_EXIT=$?
python -m pytest tests --collect-only -q --override-ini=addopts= -p no:cacheprovider --continue-on-collection-errors > /home/collect.log 2>&1
COLLECT_EXIT=$?
set -e

tail -n 60 /home/collect.log || true
COLLECTED=$(grep -cE "::" /home/collect.log 2>/dev/null || true)
COLLECTED=${COLLECTED:-0}
echo "PREPARE_IMPORT_EXIT=$IMPORT_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECT_EXIT=$COLLECT_EXIT" >> "$STATUS_LOG"
echo "PREPARE_COLLECTED=$COLLECTED" >> "$STATUS_LOG"

if [ "$IMPORT_EXIT" -ge 128 ] || [ "$COLLECT_EXIT" -ge 128 ]; then
  echo "PREPARE_SIGNAL_DEATH=1" >> "$STATUS_LOG"
else
  test "$IMPORT_EXIT" = "0"
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
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
__APPLY____TEST_CMD__
"""


def _render(template: str, repo: str) -> str:
    return template.replace("__REPO__", repo).replace("__TEST_CMD__", _TEST_CMD)


def _run_script(repo: str, apply_cmd: str) -> str:
    return _render(_RUN_TEMPLATE, repo).replace("__APPLY__", apply_cmd)


class OtooleImageBase(Image):
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


class OtooleImageDefault(Image):
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
        return OtooleImageBase(self.pr, self._config)

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


@Instance.register("OSeMOSYS", "otoole")
class OTOOLE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OtooleImageDefault(self.pr, self._config)

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
