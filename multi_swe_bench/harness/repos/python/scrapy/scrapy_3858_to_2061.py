
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.8-slim-bookworm"

CONSTRAINTS = (
    "'Twisted<22.0.0' 'pyOpenSSL==20.0.1' 'cryptography==3.4.8' 'w3lib<2'"
    " 'service_identity<22'"
)

RUNNER = "'pytest>=6.2,<7' 'pytest-cov>=3,<4' 'pytest-xdist>=2.5,<3'"

TWISTED_INI_PROBE = "grep -qE '^[[:space:]]*twisted[[:space:]]*=' pytest.ini"

EXTRA_TEST_DEPS = "testfixtures jmespath Pillow google-cloud-storage"

PYTEST_CMD = (
    "python -m pytest tests -v -rA --tb=no"
    " --continue-on-collection-errors -p no:cacheprovider"
)

SHELL_ENV = """export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY"""


BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \
      org.opencontainers.image.description="{org}/{repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git pkg-config \
    libxml2-dev libxslt1-dev libssl-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

{fetch}

CMD ["/bin/bash"]
"""


class ScrapyLegacyImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return BASE_DOCKERFILE.format(
            image=image_name,
            org=self.pr.org,
            repo=self.pr.repo,
            fetch=fetch,
        )


class ScrapyLegacyImageDefault(Image):
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
        return ScrapyLegacyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env = SHELL_ENV
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

{env}

cd /home/{repo}

bash /home/check_git_changes.sh

python -m pip install --upgrade pip setuptools wheel

if [ -s requirements-py3.txt ]; then
  echo "prepare: runtime requirements from requirements-py3.txt"
  python -m pip install -r requirements-py3.txt || true
fi

python -m pip install -e .

for req in tests/requirements-py3.txt tests/requirements.txt; do
  if [ -s "$req" ]; then
    echo "prepare: test requirements from $req"
    sed -e 's/\\r$//' \\
        -e 's/[[:space:]][[:space:]]*#.*$//' \\
        -e 's/[[:space:]][[:space:]]*$//' "$req" > /tmp/test-requirements.txt
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;
      esac
      python -m pip install "$line" || echo "prepare: optional test dep skipped: $line"
    done < /tmp/test-requirements.txt
    break
  fi
done

echo "prepare: applying dependency caps"
python -m pip install {CONSTRAINTS}

python -m pip install {RUNNER}

if {TWISTED_INI_PROBE}; then
  echo "prepare: pytest.ini declares twisted, installing pytest-twisted"
  python -m pip install 'pytest-twisted>=1.13,<1.14'
fi

python -m pip install {EXTRA_TEST_DEPS} || true

python -m pip install {CONSTRAINTS}

python -c "import scrapy; print('prepare: scrapy', scrapy.__version__)"

python -m pytest tests --collect-only -q > /tmp/collect.log 2>&1 || true
COLLECTED=$(grep -cE '::' /tmp/collect.log || true)
echo "prepare: collected $COLLECTED test ids"
if [ "$COLLECTED" -lt 1 ]; then
  echo "prepare: pytest collected no tests from tests/, output follows:" >&2
  tail -40 /tmp/collect.log >&2
  exit 1
fi

if grep -q 'INTERNALERROR' /tmp/collect.log; then
  echo "prepare: pytest raised INTERNALERROR despite collecting, output follows:" >&2
  tail -40 /tmp/collect.log >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{PYTEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        if "::" not in name and "/" not in name:
            continue

        status = match.group("status")
        if status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status in _SKIP_STATUSES:
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("scrapy", "scrapy_3858_to_2061")
class SCRAPY_3858_TO_2061(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScrapyLegacyImageDefault(self.pr, self._config)

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
        return parse_pytest_log(test_log)
