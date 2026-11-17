from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.11-slim-bookworm"

_APT_PACKAGES = "build-essential ca-certificates gfortran git"

_BOOTSTRAP_PINS = (
    '"pip==26.3" "setuptools==84.2.1" "setuptools-scm==10.3.4" "wheel==0.48.0"'
)

_STACK_PINS = (
    '"mpmath==1.3.0" "pytest==8.4.2" "pytest-timeout==2.4.0" '
    '"hypothesis==6.141.1"'
)

_BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
_END_MARKER = "===== END TEST DETAIL ====="

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

python --version
python -m pip install --no-cache-dir --no-build-isolation --no-deps -e .

# From sympy 1.15.dev on, sympy/release.py derives __version__ from
# `git describe --tags --match sympy-*` whenever sympy/_version.py is absent,
# and raises ImportError when that fails. The PR layer prunes every ref, so
# write the static module a wheel ships while the tags are still readable.
printf '__commit_id__ = "g%s"\\n__version__ = "%s"\\n' \\
    "$(git rev-parse --short=10 HEAD)" \\
    "$(python -c 'import sympy; print(sympy.__version__)')" \\
    > sympy/_version.py

cd /
python -c "import sympy; print('sympy', sympy.__version__, sympy.__file__)"
python -c "import sympy.core, sympy.polys, sympy.matrices, sympy.printing, sympy.solvers"
python -c "import mpmath, hypothesis; print('mpmath', mpmath.__version__)"
cc --version | head -1
gfortran --version | head -1
python -m pytest --version
echo "DEPS_OK"
"""

_STAGE_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONHASHSEED=0

cd /home/{repo}
"""

_APPLY_TEST_PATCH = "git apply --whitespace=nowarn /home/test.patch\n"

_APPLY_BOTH_PATCHES = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"

_TEST_BODY = r"""
printf '<testsuites/>' > /home/results.xml

set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)test_[^/]*\.py$' \
    | sort -u)
TEST_TARGETS=$(test -n "$TEST_FILES" && ls -d $TEST_FILES 2>/dev/null)
set -e

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch exists in this tree"
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    python -m pytest $TEST_TARGETS \
        -m "not slow and not tooslow" \
        --timeout=600 \
        -p no:cacheprovider \
        --color=no \
        --tb=line \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --junitxml=/home/results.xml
    echo "TEST_EXIT_CODE=$?"
    set -e
fi

python -I /home/report.py
"""

_REPORT_PY = f'''import xml.etree.ElementTree as ET

print("{_BEGIN_MARKER}")
for tc in ET.parse("/home/results.xml").getroot().iter("testcase"):
    status = ("PASSED", "SKIPPED", "FAILED", "FAILED")[
        2 * (tc.find("failure") is not None or tc.find("error") is not None)
        + (tc.find("skipped") is not None)
    ]
    name = (tc.get("name") or "").replace("\\r", " ").replace("\\n", " ")
    print("TESTCASE " + (tc.get("classname") or "") + "::" + name + " " + status)
print("{_END_MARKER}")
'''


class SympyBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            clone = f"COPY {repo} /home/{repo}"

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"{self.clear_env}\n\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

{env_block}ARG TARGETARCH
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN python -m pip install --no-cache-dir {_BOOTSTRAP_PINS}

RUN python -m pip install --no-cache-dir {_STACK_PINS}

WORKDIR /home/

{clone}

WORKDIR /home/{repo}

{clear_block}CMD ["/bin/bash"]
"""


class SympyDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return SympyBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        header = _STAGE_HEADER.format(repo=self.pr.repo)
        body = _TEST_BODY

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "report.py", _REPORT_PY),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(".", "run.sh", header + body),
            File(".", "test-run.sh", header + _APPLY_TEST_PATCH + body),
            File(".", "fix-run.sh", header + _APPLY_BOTH_PATCHES + body),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {base.image_name()}:{base.image_tag()}

{env_block}ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
{clear_block}"""



@Instance.register("sympy", "sympy")
class SYMPY_31021_TO_19463(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SympyDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")
        status_map = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()
            if stripped.startswith(_BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(_END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if match:
                status_map[match.group(2)].add(match.group(1))

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
