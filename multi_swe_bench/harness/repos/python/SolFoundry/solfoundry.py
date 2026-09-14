import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.12-slim"
_BASE_TAG = "base-65_to_65"
_TEST_TARGET = "backend/tests"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
"""

_EMIT_RESULTS_PY = """import os
import sys
import xml.etree.ElementTree as ET

report = sys.argv[1]
expected = sys.argv[2] if len(sys.argv) > 2 else ""
results = {}

if os.path.exists(report):
    for case in ET.parse(report).getroot().iter("testcase"):
        cls = (case.get("classname") or "").strip()
        name = (case.get("name") or "").strip()
        if not cls or not name:
            continue
        status = "PASSED"
        for child in case:
            tag = child.tag.split("}")[-1].lower()
            if tag in ("failure", "error"):
                status = "FAILED"
                break
            if tag == "skipped":
                status = "SKIPPED"
                break
        key = cls + " > " + name
        if status == "FAILED" or key not in results:
            results[key] = status

if expected and os.path.exists(expected):
    for line in open(expected):
        key = line.strip()
        if key and key not in results:
            results[key] = "FAILED"

for key, status in results.items():
    print("TESTCASE " + status + " " + key)
sys.stderr.write("emit_results: %d test cases\\n" % len(results))
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

pip install --no-cache-dir -r backend/requirements.txt
pip install --no-cache-dir fastapi==0.141.1 pydantic==2.13.5 httpx==0.28.1 \\
    sqlalchemy==2.0.52 pytest==9.1.1

PYTHONPATH=/home/__REPO__/backend python -c "import app.main, sqlalchemy, pytest, httpx; print('DEPS_OK')"
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

export CI=true TZ=UTC LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/home/__REPO__/backend

cd /home/__REPO__

EXPECTED=""
while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -n "$(git status --porcelain -- "$f")" ]; then
        EXPECTED=/home/expected_tests.txt
        break
    fi
done < /home/patched_tests.txt

rm -f /home/report.xml
python -m pytest __TEST_TARGET__ --continue-on-collection-errors -p no:cacheprovider \\
    -q --junitxml=/home/report.xml

python /home/emit_results.py /home/report.xml "$EXPECTED"
exit 0
"""

_RUN_SH = """#!/bin/bash
set -e
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
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

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/.+?"? "?b/(.+?)"?$')
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.py$")
_CLASS_RE = re.compile(r"^class\s+([A-Za-z_]\w*)")
_FUNC_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")


def _test_patch_targets(pr: PullRequest) -> tuple[list[str], list[str]]:
    files: list[str] = []
    ids: list[str] = []
    path = None
    cls = None
    for line in (pr.test_patch or "").replace("\r", "").split("\n"):
        header = _DIFF_FILE_RE.match(line)
        if header:
            name = header.group(1)
            path = name if _TEST_FILE_RE.search(name) else None
            cls = None
            if path and path not in files:
                files.append(path)
            continue
        if not path or not line.startswith("+"):
            continue
        body = line[1:]
        klass = _CLASS_RE.match(body)
        if klass:
            cls = klass.group(1)
            continue
        func = _FUNC_RE.match(body)
        if func:
            module = path[:-3].replace("/", ".")
            prefix = module + "." + cls if (func.group(1) and cls) else module
            test_id = prefix + " > " + func.group(2)
            if test_id not in ids:
                ids.append(test_id)
    return files, ids


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__TEST_TARGET__", _TEST_TARGET)
    )


class SolfoundryImageBase(Image):
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


class SolfoundryImageDefault(Image):
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
        return SolfoundryImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        files, ids = _test_patch_targets(self.pr)
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "patched_tests.txt", "".join(f + "\n" for f in files)),
            File(".", "expected_tests.txt", "".join(i + "\n" for i in ids)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "run.sh", _RUN_SH),
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


@Instance.register("SolFoundry", "solfoundry")
class SOLFOUNDRY(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SolfoundryImageDefault(self.pr, self._config)

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
            match = _TESTCASE_RE.match(line.rstrip())
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


Instance.register("SolFoundry", "solfoundry_65")(SOLFOUNDRY)
