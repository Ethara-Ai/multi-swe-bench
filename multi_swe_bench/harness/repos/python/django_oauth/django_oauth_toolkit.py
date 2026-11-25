from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# django-oauth/django-oauth-toolkit -- single era, PRs 1244..1947 (18 PRs).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# every PR. The base keeps FULL history and never references BASE_COMMIT; the pin
# lives in prepare.sh and the history scrub in the PR layer. The `# syntax`
# directive on line 1 of the base Dockerfile keeps DockerfileEnhancer from
# rewriting the clone into a pinned, scrubbed single commit.
#
# Era analysis (all 18 base SHAs checked, not just the endpoints):
#   packaging        setup.cfg (#1244) -> pyproject.toml (#1450..#1947); uv.lock
#                    appears at #1626 but tox -- upstream's test entry point at every
#                    commit -- installs with pip, so pip is the installer here too
#   python           tox matrix py37-311 (#1244), py38-312 (#1450), >=3.10
#                    (#1626..#1925), >=3.11 (#1947). 3.11 is in every PR's matrix.
#   django           whatever tox's matrix allows for py311 at that commit. pip is
#                    run with --uploaded-prior-to <commit date>, so it resolves the
#                    newest Django released by then that supports 3.11: 4.1 (#1244),
#                    5.1 (#1450), 5.2 (#1626..#1947; Django 6.x needs 3.12) -- each
#                    one a cell of that commit's own tox envlist
#   test runner      pytest + pytest-django, DJANGO_SETTINGS_MODULE=tests.settings,
#                    PYTHONPATH=<repo> at every commit (tox [testenv] setenv)
#   test deps        tox deps list (#1244, #1450) / the `test` extra (#1626..);
#                    both sets are installed so the union covers every PR
# No fix patch changes packaging metadata, so one provisioning pass per PR holds.
BASE_TAG = "base"

PYTHON_IMAGE = "python:3.11.15-slim-bookworm"
PIP_VERSION = "26.2.2"

REPO_DIR = "/home/django-oauth-toolkit"

# tox's deps list before the `test` extra existed (#1244, #1450).
TEST_DEPS = (
    "djangorestframework jwcrypto coverage pytest pytest-cov pytest-django "
    "pytest-xdist pytest-mock requests pytest-timeout"
)

# -o addopts= drops pyproject/tox addopts: coverage, `-s` (which interleaves test
# output with the verbose status lines) and, from #1925 on, `-n auto` (serial runs
# keep every module's process-global settings swaps in collection order, which is
# what upstream's `--dist loadfile` exists to preserve). tests/e2e boots a live
# server and a browser and is run separately upstream (`tox -e e2e`); tests/app is
# the sample IdP/RP project, not a test package.
PYTEST_FLAGS = (
    '-p no:cacheprovider -vv -rA --color=no --tb=short -o addopts="" '
    "-o console_output_style=classic --continue-on-collection-errors "
    "--ignore=tests/e2e --ignore=tests/app --timeout=600"
)

COLLECTION_TEST_NAME = "<collection>"


class DjangoOauthToolkitImageBase(Image):
    """Shared base: interpreter + pinned pip + full-history clone. No pin, no scrub."""

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
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build) but
        # never REFERENCED below. Consuming it would pin this shared image.
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
    LC_ALL=C.UTF-8 \\
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

ENV PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "pip=={PIP_VERSION}" && python -m pip --version

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {REPO_DIR} && \\
    cd {REPO_DIR} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


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


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

# ========== Section 1: PIN ==========
cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# A base commit that no branch reaches any more does not arrive with a plain
# clone; fetch exactly that commit. A failed fetch fails the build.
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    git fetch --no-tags origin [[SHA]]
    rm -f .git/FETCH_HEAD
fi

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
# From here the tree may be modified on purpose; no clean-tree assertion below.
# Resolve against the index as it was on the day of the base commit, so a PR from
# 2023 gets 2023's Django/oauthlib rather than today's.
cutoff="$(git show -s --format=%cI HEAD)"

python -m pip install --uploaded-prior-to "${cutoff}" setuptools wheel

# The `test` extra exists from #1626 on; before that the test deps lived only in
# tox.ini, so install that list too. --no-build-isolation keeps the build backend
# at the same cutoff.
target="."
if grep -qE '^test = \[' pyproject.toml 2>/dev/null; then
    target=".[test]"
fi
python -m pip install --uploaded-prior-to "${cutoff}" --no-build-isolation \
    -e "${target}" [[TEST_DEPS]]

# ========== Section 3: GATE (non-tolerant, last) ==========
export DJANGO_SETTINGS_MODULE=tests.settings
export PYTHONPATH=[[REPO_DIR]]
python -c "import django, oauth2_provider, pytest, pytest_django, pytest_timeout, rest_framework; assert oauth2_provider.__file__.startswith('[[REPO_DIR]]/'); print('django', django.get_version())"
python -m pytest --collect-only -q [[PYTEST_FLAGS]] tests >/tmp/pytest_collect.log 2>&1 \
    || { tail -50 /tmp/pytest_collect.log >&2; exit 1; }
tail -1 /tmp/pytest_collect.log
rm -f /tmp/pytest_collect.log
echo DEPS_OK
"""


# The one test command every stage runs.
TEST_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export DJANGO_SETTINGS_MODULE=tests.settings
export PYTHONPATH=[[REPO_DIR]]

cd [[REPO_DIR]]

python -m pytest [[PYTEST_FLAGS]] tests 2>&1
"""


class DjangoOauthToolkitImageDefault(Image):
    """Per-PR layer: patches, scripts, the pin (via prepare.sh), and the scrub."""

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
        return DjangoOauthToolkitImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_DEPS]]", TEST_DEPS)
            .replace("[[PYTEST_FLAGS]]", PYTEST_FLAGS)
        )

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
            "fix-run.sh": (
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            ),
        }

        staged = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\nbash /home/test.sh\n"
                ),
            )
            for name, patch_step in stage_patches.items()
        ]

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "test.sh", self._expand(TEST_SH)),
        ] + staged

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        # PR images are never touched by DockerfileEnhancer, so everything this
        # layer needs is written out here. The scrub lives here because the shared
        # base must keep full history for every PR to reach its own commit. It
        # holds no reset/clean, so prepare.sh's provisioning survives.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{copies(self.install_files())}
RUN bash /home/prepare.sh

{copies(self.grading_files())}
WORKDIR {REPO_DIR}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {REPO_DIR}/.gitmodules ]; then \\
        cd {REPO_DIR} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

"""


def parse_pytest_log(test_log: str) -> TestResult:
    log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    # Keys are pytest node ids, unique per test and free of timing or counts.
    statuses = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
    verbose_re = re.compile(r"^(\S+\.py::.+?) (" + statuses + r")(?: \(.*\))?\s*$")
    summary_re = re.compile(r"^(" + statuses + r") (\S+\.py::.+?)(?: - .*)?\s*$")
    collect_re = re.compile(r"^ERROR (\S+\.py)(?: - .*)?\s*$")

    buckets: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
    outcome = {
        "PASSED": "PASSED",
        "XPASS": "PASSED",
        "FAILED": "FAILED",
        "ERROR": "FAILED",
        "SKIPPED": "SKIPPED",
        "XFAIL": "SKIPPED",
    }

    for line in log.splitlines():
        line = line.rstrip()

        verbose_match = verbose_re.match(line)
        if verbose_match:
            buckets[outcome[verbose_match.group(2)]].add(verbose_match.group(1))
            continue

        summary_match = summary_re.match(line)
        if summary_match:
            buckets[outcome[summary_match.group(1)]].add(summary_match.group(2))
            continue

        collect_match = collect_re.match(line)
        if collect_match:
            buckets["FAILED"].add(f"{collect_match.group(1)}::{COLLECTION_TEST_NAME}")

    passed = buckets["PASSED"]
    failed = buckets["FAILED"]
    skipped = buckets["SKIPPED"]

    # Failure wins (a test that passes and then errors in teardown is a failure).
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


@Instance.register("django-oauth", "django-oauth-toolkit")
class DJANGO_OAUTH_TOOLKIT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DjangoOauthToolkitImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_pytest_log(test_log)
