import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PIP_PINS = " ".join(
    [
        "homeassistant==2025.4.4",
        "pytest-homeassistant-custom-component==0.13.236",
        "alexapy==1.29.7",
        "wrapt==1.17.2",
        "pytest-timeout==2.3.1",
    ]
)

TEST_COMMAND = """cd /home/alexa_media_player
python3 -m pytest -v --timeout=9 -p no:sugar -p no:cacheprovider tests 2>&1"""

RUN_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONDONTWRITEBYTECODE=1
"""


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
        return "python:3.13-slim"

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

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
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
    TZ=UTC \\
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends git ca-certificates build-essential && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

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
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
git clean -fdx
bash /home/check_git_changes.sh

python3 -m pip install --no-cache-dir --disable-pip-version-check [[PIP_PINS]]

python3 --version
test "$(pwd -P)" = "/home/[[REPO]]"
python3 -c "import pytest, pytest_asyncio, pytest_timeout, pytest_cov, homeassistant, alexapy, wrapt, pytest_homeassistant_custom_component; from homeassistant.const import __version__ as ha_version; assert pytest.__version__ == '8.3.5', pytest.__version__; assert pytest_asyncio.__version__ == '0.26.0', pytest_asyncio.__version__; assert ha_version == '2025.4.4', ha_version; assert alexapy.__version__ == '1.29.7', alexapy.__version__; assert wrapt.__version__ == '1.17.2', wrapt.__version__; print('pytest', pytest.__version__, '| homeassistant', ha_version, '| alexapy', alexapy.__version__)"
python3 -m pytest -p no:cacheprovider --collect-only -q tests > /tmp/collect-gate.log 2>&1
test "$(grep -cE "^tests/\\S+\\.py::" /tmp/collect-gate.log)" -gt 0
echo DEPS_OK
""".replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
                .replace("[[PIP_PINS]]", PIP_PINS),
            ),
            File(
                ".",
                "run.sh",
                RUN_HEADER
                + """
[[TEST_COMMAND]]
""".replace("[[TEST_COMMAND]]", TEST_COMMAND),
            ),
            File(
                ".",
                "test-run.sh",
                RUN_HEADER
                + """
cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "PATCH_APPLY_FAILED: test.patch does not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", TEST_COMMAND),
            ),
            File(
                ".",
                "fix-run.sh",
                RUN_HEADER
                + """
cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "PATCH_APPLY_FAILED: test.patch + fix.patch do not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", TEST_COMMAND),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("alandtse", "alexa_media_player")
class AlandtseAlexaMediaPlayer(Instance):
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
        seen: dict[str, int] = {}
        last_base = ""
        last_name = ""

        cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        case_re = re.compile(
            r"^(?P<name>tests/\S+\.py::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\([^)]*\))?(?:\s+\[\s*\d+%\])?\s*$"
        )
        collect_re = re.compile(r"^ERROR\s+(?P<name>tests/\S+\.py)(?:\s+-\s+.*)?$")

        for line in cleaned.splitlines():
            line = line.rstrip()
            m = case_re.match(line)
            if m:
                base_name = m.group("name")
                status = m.group("status")
                if status == "ERROR" and base_name == last_base:
                    failed_tests.add(last_name)
                    continue
                n = seen.get(base_name, 0) + 1
                seen[base_name] = n
                name = base_name if n == 1 else f"{base_name} #{n}"
                last_base = base_name
                last_name = name
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            c = collect_re.match(line)
            if c:
                failed_tests.add(c.group("name"))

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
