import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.10-slim"
_BASE_TAG = "base-3320_to_1105"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
"""

_EMIT_TESTCASES_PY = """import os

_RANK = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
_RESULTS = {}


def pytest_runtest_logreport(report):
    if report.when == "call":
        if report.passed:
            status = "PASSED"
        elif report.skipped:
            status = "SKIPPED"
        else:
            status = "FAILED"
    elif report.skipped:
        status = "SKIPPED"
    elif report.failed:
        status = "FAILED"
    else:
        return
    if _RANK[status] > _RANK.get(_RESULTS.get(report.nodeid), -1):
        _RESULTS[report.nodeid] = status


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["TESTCASE_OUT"], "w") as fh:
        for nodeid, status in _RESULTS.items():
            fh.write("TESTCASE " + status + " " + nodeid + "\\n")
"""

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
python -m pip install --no-cache-dir pytest
python -c "import pygame, pytest, tuxemon; print('DEPS_OK')"
"""

_RUN_SH = """#!/bin/bash
set -uo pipefail

export PYTHONPATH=/home:/home/__REPO__
export TESTCASE_OUT=/home/testcases.txt
export SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy
export TZ=UTC LC_ALL=C.UTF-8 PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__
: > "$TESTCASE_OUT"

python -m pytest tests -p emit_testcases -p no:cacheprovider \\
    -q --continue-on-collection-errors

cat "$TESTCASE_OUT"
exit 0
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

bash /home/run.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
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
    ca-certificates git build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*?)\s*$")


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
    )


class TuxemonImageBase3320To1105(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _render(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency()), self.pr
        )


class TuxemonImageDefault3320To1105(Image):
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
        return TuxemonImageBase3320To1105(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_testcases.py", _EMIT_TESTCASES_PY),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        prepare_copy = "".join(f"COPY {f.name} /home/\n" for f in self.prepare_files())
        graded_copy = "".join(f"COPY {f.name} /home/\n" for f in self.graded_files())

        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("Tuxemon", "Tuxemon_3320_to_1105")
class TUXEMON_3320_TO_1105(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TuxemonImageDefault3320To1105(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in test_log.replace("\r", "").split("\n"):
            match = _TESTCASE_RE.match(line)
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed | passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("Tuxemon", "Tuxemon")(TUXEMON_3320_TO_1105)
