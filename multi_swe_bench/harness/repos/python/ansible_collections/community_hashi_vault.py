import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PIP_PINS = " ".join(
    [
        "ansible-core==2.11.6",
        "hvac==0.11.2",
        "pytest==6.2.5",
        "pytest-mock==3.6.1",
        "pytest-forked==1.3.0",
        "mock==4.0.3",
        "requests==2.26.0",
        "urllib3==1.26.7",
        "Jinja2==3.0.2",
        "MarkupSafe==2.0.1",
        "PyYAML==6.0",
        "cryptography==35.0.0",
        "cffi==1.15.0",
        "pycparser==2.20",
        "packaging==21.0",
        "pyparsing==2.4.7",
        "resolvelib==0.5.4",
        "certifi==2021.10.8",
        "charset-normalizer==2.0.7",
        "idna==3.3",
        "six==1.16.0",
        "attrs==21.2.0",
        "iniconfig==1.1.1",
        "pluggy==1.0.0",
        "py==1.10.0",
        "toml==0.10.2",
    ]
)

PYTEST_ENV = """AT_DATA="$(python -c 'import ansible_test, os; print(os.path.join(os.path.dirname(ansible_test.__file__), "_data"))')"
export ANSIBLE_COLLECTIONS_PATH=/home
export PYTHONPATH="${AT_DATA}/pytest/plugins:/home"
export PYTEST_PLUGINS=ansible_pytest_collections"""

TEST_COMMAND = """cd /home/[[REPO]]
[[PYTEST_ENV]]
python -m pytest -c "${AT_DATA}/pytest.ini" --rootdir . -p no:cacheprovider --forked --continue-on-collection-errors -v --no-header -rA --tb=short --color=no tests/unit 2>&1"""

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
        return "python:3.9-slim-bookworm"

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
        namespace, name = repo.split(".", 1)

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
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN mkdir -p /home/ansible_collections/{namespace}/{name} && \\
    ln -s /home/ansible_collections/{namespace}/{name} /home/{repo}

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
        namespace, name = repo.split(".", 1)
        test_command = TEST_COMMAND.replace("[[PYTEST_ENV]]", PYTEST_ENV).replace("[[REPO]]", repo)

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
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1
export PIP_ROOT_USER_ACTION=ignore

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
git clean -fdx
bash /home/check_git_changes.sh

python -m pip install -r tests/unit/requirements.txt [[PIP_PINS]]
python -m pip check

python --version
test "$(pwd -P)" = "/home/ansible_collections/[[NAMESPACE]]/[[NAME]]"
[[PYTEST_ENV]]
test -f "${AT_DATA}/pytest.ini"
test -f "${AT_DATA}/pytest/plugins/ansible_pytest_collections.py"
python -c "import ansible, ansible_test, hvac, pytest, pytest_forked, pytest_mock, mock, requests, urllib3; assert ansible.__version__ == '2.11.6', ansible.__version__; print('ansible-core', ansible.__version__, 'pytest', pytest.__version__)"
python -m pytest -c "${AT_DATA}/pytest.ini" --rootdir . -p no:cacheprovider --collect-only -q tests/unit > /tmp/collect-gate.log
grep -E "^[0-9]+ tests? collected" /tmp/collect-gate.log
echo DEPS_OK
""".replace("[[PYTEST_ENV]]", PYTEST_ENV)
                .replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
                .replace("[[PIP_PINS]]", PIP_PINS)
                .replace("[[NAMESPACE]]", namespace)
                .replace("[[NAME]]", name),
            ),
            File(
                ".",
                "run.sh",
                RUN_HEADER
                + """
[[TEST_COMMAND]]
""".replace("[[TEST_COMMAND]]", test_command),
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
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
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
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("ansible-collections", "community.hashi_vault")
class AnsibleCollectionsCommunityHashiVault(Instance):
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
            r"^(?P<name>tests/\S*?\.py::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
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
