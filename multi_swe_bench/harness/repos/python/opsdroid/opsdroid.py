import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "opsdroid"
REPO = "opsdroid"

# Era A runs natively on this image; Era B provisions uv-managed 3.11 on top.
_PY38_IMAGE = "python:3.8-slim-bookworm"
_BASE_TAG = "base-py38-uv311"

_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "ffmpeg",
    "git",
    "gnupg",
    "make",
    "patch",
    "sudo",
    "wget",
]


def _era(pr_number: int) -> str:
    if pr_number >= 2058:
        return "era_b"
    if pr_number == 1781:
        return "era_a_modern"
    return "era_a_old"


# --------------------------------------------------------------------- scripts

CHECK_GIT_CHANGES = """#!/bin/bash
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


# --------------------------------------------------------------------- images

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
        return _PY38_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # The leading syntax directive keeps DockerfileEnhancer from rewriting
        # the clone or pinning this shared image to one PR's BASE_COMMIT, so
        # the infrastructure block it would otherwise contribute is written
        # out here. BASE_COMMIT is declared but deliberately unused:
        # build_dataset passes it to every image whose dependency() is a str.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"
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
    CI=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
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

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
{global_env}
WORKDIR /home/

{apt_command}

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN python -m pip install --no-cache-dir uv && uv python install 3.11

RUN git clone "${{REPO_URL}}" /home/{REPO}
{clear_env}
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
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _env_setup(self) -> str:
        # Era B code requires python>=3.11 while the shared base boots from
        # python:3.8; its interpreter is the uv-managed 3.11 provisioned there.
        if _era(self.pr.number) == "era_b":
            return "uv venv --python 3.11 --seed /venv311\n. /venv311/bin/activate"
        return ""

    def _env_activate(self) -> str:
        if _era(self.pr.number) == "era_b":
            return ". /venv311/bin/activate"
        return ""

    def _install_commands(self) -> str:
        era = _era(self.pr.number)
        if era == "era_b":
            return """python -m pip install --no-cache-dir -e ".[test,connector_mattermost]" "aiohttp==3.11.18"
python -m pip install --no-cache-dir "setuptools<81\""""
        if era == "era_a_modern":
            return """python -m pip install --no-cache-dir "pytest==8.3.5" "pytest-asyncio==0.24.0"
python -m pip install --no-cache-dir -e ".[test,connector_slack,connector_matrix,database_sqlite]\""""
        return """python -m pip install --no-cache-dir "pytest==6.2.5" "pytest-asyncio==0.16.0" "pytest-mock==3.6.1" "pytest-timeout==1.4.2" "pytest-cov==2.12.1"
python -m pip install --no-cache-dir -e ".[test,connector_slack,connector_matrix,database_sqlite]\""""

    def _test_command(self) -> str:
        # R3: identical command in run.sh / test-run.sh / fix-run.sh.
        if _era(self.pr.number) == "era_b":
            return "python -m pytest opsdroid/connector/mattermost/tests -v --no-header -rA --tb=no -p no:cacheprovider"
        return "python -m pytest opsdroid tests --ignore=opsdroid/connector/matrix --ignore=tests/test_connector_matrix.py --ignore=tests/test_database_matrix.py --ignore=tests/test_connector_webexteams.py -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors"

    def files(self) -> list[File]:
        env_setup = self._env_setup()
        activate = self._env_activate()
        activate_run = f"{activate}\n" if activate else ""
        install_commands = self._install_commands()
        test_command = self._test_command()

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
{env_setup}
{install_commands}

{test_command} --collect-only -q || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
{activate_run}{test_command}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
# GNU patch with fuzz: some upstream fix patches carry drifted context that
# strict `git apply` rejects entirely; partial application is accepted.
patch -p1 --fuzz=3 < /home/test.patch || echo "WARN: test.patch applied with rejects"
{activate_run}{test_command}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
patch -p1 --fuzz=3 < /home/test.patch || echo "WARN: test.patch applied with rejects"
patch -p1 --fuzz=3 < /home/fix.patch || echo "WARN: fix.patch applied with rejects"
{activate_run}{test_command}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Chains to a base Image rather than a str, so DockerfileEnhancer
        # returns this verbatim and injects nothing; the hardening block is
        # applied by hand. It opens with `git checkout --detach
        # "${BASE_COMMIT}"`, so it performs this PR's checkout as well as
        # pruning the full history inherited from the shared base.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
{copy_commands}
WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


# ------------------------------------------------------------------- instance

@Instance.register(ORG, REPO)
class Opsdroid(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # opsdroid keeps tests in two trees: root tests/ and nested
        # opsdroid/**/tests/. pytest -v --no-header -rA emits each test twice:
        #   opsdroid/connector/slack/tests/test_connector.py::test_x PASSED
        #   PASSED opsdroid/connector/slack/tests/test_connector.py::test_x
        # Collection errors ("ERROR collecting tests/test_x.py") carry no "::"
        # node id and deliberately do not match (they stay NONE status).
        path = r"(?P<node>(?:opsdroid|tests)/[^\s:]+(?:::[^\s:]+)+)"
        re_pass = [
            re.compile(rf"^{path}\s+(?:PASSED|XPASS)"),
            re.compile(rf"^(?:PASSED|XPASS)\s+{path}"),
        ]
        re_fail = [
            re.compile(rf"^{path}\s+(?:FAILED|ERROR)"),
            re.compile(rf"^(?:FAILED|ERROR)\s+{path}"),
        ]
        re_skip = [
            re.compile(rf"^{path}\s+(?:SKIPPED|XFAIL)"),
            re.compile(rf"^(?:SKIPPED|XFAIL)\s+{path}"),
        ]

        for line in test_log.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            if not line:
                continue

            for re_p in re_pass:
                match = re_p.match(line)
                if match:
                    passed_tests.add(match.group("node"))

            for re_f in re_fail:
                match = re_f.match(line)
                if match:
                    failed_tests.add(match.group("node"))

            for re_s in re_skip:
                match = re_s.match(line)
                if match:
                    skipped_tests.add(match.group("node"))

        # R2: the three sets must be disjoint. Failure wins.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
