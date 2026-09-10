import re
from typing import Optional, Union

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NEW_ERA_PR_MIN = 6713
_NEW_ERA_PR_MAX = 7385
_NEW_ERA_KEY = "scrapy_6713_to_7385"
_PY_IMAGE = "python:3.11-slim"
_BASE_APT = (
    "git ca-certificates libssl-dev libxml2-dev libxslt1-dev zlib1g-dev libffi-dev build-essential"
)

_PIP_STEPS = [
    "pip install --no-cache-dir --upgrade pip setuptools wheel",
    "pip install --no-cache-dir -e .",
    "pip install --no-cache-dir pytest pytest-cov pytest-xdist pytest-twisted pytest-timeout testfixtures sybil pexpect pyftpdlib pygments coverage attrs",
    "if grep -Pzoq 'parser\\.addoption\\s*\\(\\s*\"--reactor\"' /home/scrapy/conftest.py 2>/dev/null; then pip uninstall -y pytest-twisted 2>/dev/null || true; fi",
]

_TEST_CMD = "pytest tests -v --continue-on-collection-errors --timeout=120"


# Load-time monkey-patch: raw JSONL has empty number_interval on all scrapy/scrapy
# PRs in this bundle, so without this shim the harness would route them to
# "scrapy/scrapy" (Python 2.7.18 era) and mis-build. Guarded so repeat imports
# leave the classmethod intact.
if not getattr(_pull_request.PullRequest, "_scrapy_new_era_patched", False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _scrapy_new_era_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == "scrapy"
            and pr.repo == "scrapy"
            and _NEW_ERA_PR_MIN <= pr.number <= _NEW_ERA_PR_MAX
            and not pr.number_interval
        ):
            pr.number_interval = _NEW_ERA_KEY
        return pr

    _pull_request.PullRequest.from_json = classmethod(_scrapy_new_era_from_json)
    _pull_request.PullRequest._scrapy_new_era_patched = True


class ScrapyNewEraImageBase(Image):
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
        return "base-6713_to_7385"

    def workdir(self) -> str:
        return "base-6713_to_7385"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        # Opens with the syntax opt-out so DockerfileEnhancer bails and does not
        # inject its own pin/hardening into the shared base. Base ends at git
        # clone + CMD per m0269 rule 1; hardening lives in the PR layer.
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


class ScrapyNewEraImageDefault(Image):
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
        return ScrapyNewEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        pip_block = "\n".join(_PIP_STEPS)

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        # prepare.sh has NO git ops per m0269 rule 2. Pin+scrub live in the PR
        # Dockerfile. STATUS_LOG captures HEAD + tree cleanliness + pip exit for
        # post-mortem when a build looks off.
        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"STATUS_LOG=/home/{repo}/prepare-status.log\n"
            ': > "$STATUS_LOG"\n'
            f"cd /home/{repo}\n"
            'echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"\n'
            'echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"\n'
            "set +e\n"
            f"{pip_block}\n"
            'PIP_EXIT=$?\n'
            'echo "PREPARE_PIP_EXIT=$PIP_EXIT" >> "$STATUS_LOG"\n'
            "set -e\n"
            'test "$PIP_EXIT" = "0"\n'
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            f"{_TEST_CMD}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_TEST_CMD}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_TEST_CMD}\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
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


@Instance.register("scrapy", "scrapy_6713_to_7385")
class SCRAPY_6713_TO_7385(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScrapyNewEraImageDefault(self.pr, self._config)

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
