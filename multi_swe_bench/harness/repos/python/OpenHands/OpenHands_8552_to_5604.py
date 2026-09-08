from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.12-bookworm"
NODE_MAJOR = "20"
POETRY_PIN = "poetry==1.8.5"

VITEST_OUT = "/home/vitest-report.json"

_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+) b/", re.M)
_FE_TEST_RE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")


def patch_files(patch: str) -> list[str]:
    return _DIFF_FILE_RE.findall(patch or "")


def python_test_files(pr: PullRequest) -> list[str]:
    files = patch_files(pr.test_patch)
    return sorted({f for f in files if f.startswith("tests/") and f.endswith(".py")})


def frontend_test_files(pr: PullRequest) -> list[str]:
    files = patch_files(pr.test_patch)
    return sorted(
        {f for f in files if f.startswith("frontend/") and _FE_TEST_RE.search(f)}
    )


def frontend_rel_test_files(pr: PullRequest) -> list[str]:
    return [f[len("frontend/") :] for f in frontend_test_files(pr)]


CHECKOUT = r"""RUN git reset --hard
RUN git checkout [[SHA]]"""

HARDENING = r"""RUN set -eux; \
    git checkout --detach "[[SHA]]"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "[[SHA]]")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN test -f .gitmodules && \
    git submodule foreach --recursive ' \
        git checkout --detach HEAD; \
        git remote remove origin 2>/dev/null || true; \
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
            | xargs -r -n1 git update-ref -d; \
        git reflog expire --expire=now --all; \
        git reflog expire --expire-unreachable=now --all; \
        git gc --prune=now --aggressive; \
        rm -f .git/objects/info/alternates; \
    ' || true"""

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PY_INSTALL = r"""poetry env use system
poetry install --only main,test --no-root
pip install -e . --no-deps
python -c "import openhands; print('openhands import ok')"
"""

FE_INSTALL = r"""cd /home/[[REPO]]/frontend
npm ci --no-audit --no-fund
node -e "console.log('node', process.version)"
cd /home/[[REPO]]
"""

PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

python --version

[[INSTALL]]

git checkout -- .
git clean -fdq -e '*.egg-info' -e '*.egg-link' -e 'frontend/node_modules' -e '.venv'
bash /home/check_git_changes.sh
"""

PY_TEST_STEP = r"""PY_TARGETS=""
for f in [[PY_FILES]]; do
    test -f "$f" && PY_TARGETS="$PY_TARGETS $f"
done
if [ -n "$PY_TARGETS" ]; then
    python -m pytest $PY_TARGETS --no-header -rA --tb=no -p no:cacheprovider \
        -v --color=no --continue-on-collection-errors
fi
"""

FE_TEST_STEP = r"""rm -f [[VITEST_OUT]]
cd /home/[[REPO]]/frontend
npm run make-i18n --silent || true
npx vitest run [[FE_FILES]] --reporter=json --outputFile=[[VITEST_OUT]] || true
cd /home/[[REPO]]
echo "###VITEST_JSON_START###"
test -f [[VITEST_OUT]] && cat [[VITEST_OUT]] || true
echo ""
echo "###VITEST_JSON_END###"
"""

STAGE_SH = r"""#!/bin/bash
set -o pipefail
export CI=true
export TZ=UTC
export PYTHONUNBUFFERED=1
export PATH="/root/.local/bin:$PATH"

cd /home/[[REPO]] || exit 1

git checkout -- . 2>/dev/null || true
git clean -fdq -e '*.egg-info' -e '*.egg-link' -e 'frontend/node_modules' -e '.venv' 2>/dev/null || true

[[PATCH_STEP]]
[[TEST_STEP]]
exit 0
"""

APPLY = r"""apply_patch() {
    test -s "$1" || { echo "apply_patch: $1 is empty or missing"; return 0; }
    git apply --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied cleanly"; return 0; }
    git apply --3way --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied via 3-way merge (SUSPECT)"; return 0; }
    git apply -C1 --recount --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied with reduced context (SUSPECT)"; return 0; }
    patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch -r /dev/null -i "$1" >/dev/null 2>&1 && {
        echo "apply_patch: $1 -> applied with fuzz (SUSPECT)"; return 0; }
    echo "apply_patch: $1 -> DID NOT APPLY"
    return 0
}
"""


class OpenHandsImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
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
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    POETRY_NO_INTERACTION=1 \\
    POETRY_VIRTUALENVS_CREATE=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl gnupg \\
    libgl1 libglib2.0-0 \\
    && curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install "{POETRY_PIN}"

RUN git config --global http.version HTTP/1.1 && \
    git config --global http.postBuffer 524288000 && \
    git config --global http.lowSpeedLimit 1000 && \
    git config --global http.lowSpeedTime 600 && \
    git config --global core.compression 0 && \
    for attempt in 1 2 3 4 5; do \
        rm -rf /home/{repo}; \
        git clone "${{REPO_URL}}" /home/{repo} && break; \
        echo "clone attempt $attempt failed, retrying"; \
        sleep 20; \
    done && \
    test -d /home/{repo}/.git

CMD ["/bin/bash"]
"""


class OpenHandsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return OpenHandsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[VITEST_OUT]]", VITEST_OUT)
        )

    def _install_block(self) -> str:
        blocks = []
        if python_test_files(self.pr):
            blocks.append(PY_INSTALL)
        if frontend_test_files(self.pr):
            blocks.append(FE_INSTALL)
        if not blocks:
            blocks.append(PY_INSTALL)
        return "\n".join(blocks)

    def _test_step(self) -> str:
        steps = []
        py = python_test_files(self.pr)
        if py:
            steps.append(PY_TEST_STEP.replace("[[PY_FILES]]", " ".join(py)))
        fe = frontend_rel_test_files(self.pr)
        if fe:
            steps.append(FE_TEST_STEP.replace("[[FE_FILES]]", " ".join(fe)))
        return "\n".join(steps)

    def _stage(self, patch_step: str) -> str:
        body = STAGE_SH.replace("[[PATCH_STEP]]", patch_step)
        body = body.replace("[[TEST_STEP]]", self._test_step())
        return self._expand(body)

    def install_files(self) -> list[File]:
        prepare = PREPARE_SH.replace("[[INSTALL]]", self._install_block())
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(prepare)),
        ]

    def grading_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", self._stage("")),
            File(
                ".",
                "test-run.sh",
                self._stage(APPLY + "apply_patch /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._stage(
                    APPLY
                    + "apply_patch /home/test.patch\napply_patch /home/fix.patch\n"
                ),
            ),
        ]

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

{self._expand(CHECKOUT)}

{copies(self.install_files())}
{copies(self.grading_files())}
RUN bash /home/prepare.sh

{self._expand(HARDENING)}

{self.clear_env}
"""


class OpenHandsPython(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenHandsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        pytest_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)", re.M
        )
        bucket = {
            "PASSED": passed,
            "XPASS": passed,
            "FAILED": failed,
            "ERROR": failed,
            "SKIPPED": skipped,
            "XFAIL": skipped,
        }
        for status, name in pytest_re.findall(log):
            bucket[status].add(name.strip())

        for chunk in re.findall(
            r"###VITEST_JSON_START###\s*(.*?)\s*###VITEST_JSON_END###", log, re.S
        ):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                report = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            for suite in report.get("testResults", []) or []:
                for case in suite.get("assertionResults", []) or []:
                    name = (case.get("fullName") or case.get("title") or "").strip()
                    if not name:
                        continue
                    status = (case.get("status") or "").lower()
                    if status == "passed":
                        passed.add(name)
                    elif status in ("failed", "broken"):
                        failed.add(name)
                    else:
                        skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


PYTHON_PR_NUMBERS = (5604, 5950, 6239, 7655, 8310, 8383, 8552)

for _number in PYTHON_PR_NUMBERS:
    Instance.register("OpenHands", f"OpenHands_{_number}_to_{_number}")(OpenHandsPython)


_SINGLE_RE = re.compile(r"^OpenHands/OpenHands_(?P<lo>\d+)_to_(?P<hi>\d+)$")


def _single_pr_registry() -> dict[int, type]:
    owners: dict[int, type] = {}
    for key, impl in Instance._registry.items():
        match = _SINGLE_RE.match(key)
        if match and match.group("lo") == match.group("hi"):
            owners[int(match.group("lo"))] = impl
    return owners


@Instance.register("OpenHands", "OpenHands")
class OpenHandsDispatch(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        owners = _single_pr_registry()
        impl = owners.get(pr.number)
        if impl is None:
            raise ValueError(
                f"OpenHands/OpenHands#{pr.number} has no single-PR adapter; "
                f"registered numbers: {sorted(owners)}"
            )
        return impl(pr, config, *args, **kwargs)
