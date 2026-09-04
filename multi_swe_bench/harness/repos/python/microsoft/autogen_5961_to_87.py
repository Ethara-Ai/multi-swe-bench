from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.11-slim"

UV_PIN = "uv==0.5.29"

PYTEST_FLAGS = (
    "--no-header -rA --tb=no -p no:cacheprovider -v --color=no "
    "--continue-on-collection-errors"
)

_TEST_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S*test\S*\.py) b/", re.M)

_V04_PREFIX = "python/packages/"


def target_test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch or "")))


def is_v04(pr: PullRequest) -> bool:
    return any(p.startswith(_V04_PREFIX) for p in target_test_files(pr))


def test_paths(pr: PullRequest) -> list[str]:
    strip = len("python/") * is_v04(pr)
    return [p[strip:] for p in target_test_files(pr)]


def workdir_subdir(pr: PullRequest) -> str:
    return "/python" * is_v04(pr)


V02_INSTALL = """pip install --upgrade pip "setuptools<70" wheel
pip install --use-deprecated=legacy-resolver -e ".[test]"
pip install "pytest<8" pytest-asyncio pytest-cov
python -c "import autogen; print('autogen ready')"
"""

V04_INSTALL = """pip install "[[UV_PIN]]"
cd /home/[[REPO]]/python
uv sync --dev
.venv/bin/python -c "import autogen_ext; print('autogen_ext ready')"
.venv/bin/python -m pytest --version
cd /home/[[REPO]]
"""

V02_PYTEST = 'python -m pytest [[TEST_PATHS]] [[PYTEST_FLAGS]] -m "not conda"'

V04_PYTEST = "python/.venv/bin/python -m pytest [[TEST_PATHS_ABS]] [[PYTEST_FLAGS]]"

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

PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

[[INSTALL]]

python --version

cd /home/[[REPO]]
git checkout -- .
git clean -fdq -e '*.egg-info' -e '*.egg-link' -e '.venv' -e 'python/.venv'
bash /home/check_git_changes.sh
"""

STAGE_SH = r"""#!/bin/bash
set -o pipefail
export CI=true
export TZ=UTC
export PYTHONUNBUFFERED=1
export OPENAI_API_KEY=dummy

cd /home/[[REPO]] || exit 1

git checkout -- . 2>/dev/null || true
git clean -fdq -e '*.egg-info' -e '*.egg-link' -e '.venv' -e 'python/.venv' 2>/dev/null || true

[[PATCH_STEP]]
EXISTING=""
for f in [[TEST_CHECK_PATHS]]; do
    test -f "$f" && EXISTING="$EXISTING $f"
done

test -n "$EXISTING" || {
    echo "test: none of the graded test files exist at this stage: [[TEST_CHECK_PATHS]]"
    exit 0
}

[[TEST_CMD]]
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


class AutogenImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

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
    PIP_NO_CACHE_DIR=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class AutogenImageDefault(Image):
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
        return AutogenImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _install(self) -> str:
        return [V02_INSTALL, V04_INSTALL][is_v04(self.pr)]

    def _test_cmd(self) -> str:
        return [V02_PYTEST, V04_PYTEST][is_v04(self.pr)]

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[INSTALL]]", self._install())
            .replace("[[TEST_CMD]]", self._test_cmd())
            .replace("[[TEST_PATHS]]", " ".join(test_paths(self.pr)))
            .replace("[[TEST_PATHS_ABS]]", " ".join(target_test_files(self.pr)))
            .replace("[[TEST_CHECK_PATHS]]", " ".join(target_test_files(self.pr)))
            .replace("[[PYTEST_FLAGS]]", PYTEST_FLAGS)
            .replace("[[UV_PIN]]", UV_PIN)
            .replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
        )

    def _stage(self, patch_step: str) -> str:
        return self._expand(STAGE_SH).replace("[[PATCH_STEP]]", patch_step)

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
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


DATASET_PR_NUMBERS = (87, 2304, 2596, 5352, 5961)


class Autogen87To5961(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AutogenImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        result_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)", re.M
        )

        outcome = {
            "PASSED": "PASSED",
            "XPASS": "PASSED",
            "FAILED": "FAILED",
            "ERROR": "FAILED",
            "SKIPPED": "SKIPPED",
            "XFAIL": "SKIPPED",
        }

        buckets: dict[str, set[str]] = {
            "PASSED": set(),
            "FAILED": set(),
            "SKIPPED": set(),
        }
        for status, name in result_re.findall(log):
            buckets[outcome[status]].add(name.strip())

        passed = buckets["PASSED"]
        failed = buckets["FAILED"]
        skipped = buckets["SKIPPED"]

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


for _n in DATASET_PR_NUMBERS:
    Instance.register("microsoft", f"autogen_{_n}_to_{_n}")(Autogen87To5961)
