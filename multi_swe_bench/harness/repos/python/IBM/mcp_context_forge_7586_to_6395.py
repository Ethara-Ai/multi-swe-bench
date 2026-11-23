from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: IBM/mcp-context-forge PRs 6395..7586 (53 PRs, main).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
#
# Era analysis (all 53 base SHAs checked, not just the endpoints):
#   build backend      setuptools (build_py hook only builds UI assets when
#                      BUILD_UI_ASSETS=true, so no node is needed)
#   dependency tool    uv + uv.lock at every commit; lock revision 3 (50 PRs) or
#                      5 (#7182, #7235, #7333)
#   requires-python    ">=3.12,<3.14" (31 PRs) or ">=3.12,<3.15" (22 PRs)
#   test runner        pytest>=9.1.1 with the same [tool.pytest.ini_options]
#                      addopts at every commit (upstream CI wraps it as `rstest`
#                      from #7354 on; plain pytest runs the same suite)
# The PR set interleaves both requires-python ranges, so it is one era with one
# interpreter that satisfies both: 3.13.
BASE_TAG = "base-7586_to_6395"

PYTHON_IMAGE = "python:3.13-bookworm"

# Reads lock revisions 3 and 5. Upstream's own dev group pins uv>=0.13.4 at the
# latest commit.
UV_VERSION = "0.13.7"

REPO_DIR = "/home/mcp-context-forge"
VENV_DIR = "/opt/venv"

# Directories pyproject's addopts already --ignore (plus tests/fuzz, which CI
# also skips). Their suites need a live gateway, a browser or a database server.
IGNORED_TEST_DIRS = (
    "tests/playwright/",
    "tests/migration/",
    "tests/performance/",
    "tests/compliance/",
    "tests/live_gateway/",
    "tests/fuzz/",
)

# -vv: pyproject's addopts carries -q, so one -v would only cancel it and leave
# dot output. -rA adds a `PASSED/FAILED <nodeid>` summary line per test, which
# survives any test that writes to the terminal mid-line. Collection errors must
# not abort the run: a test patch that imports a symbol only the fix adds breaks
# collection of that one file, and every other file must still report.
PYTEST_FLAGS = (
    "-p no:cacheprovider -vv -rA --color=no --tb=short "
    "-o console_output_style=classic "
    "--continue-on-collection-errors --timeout=900"
)

# Key emitted for a file pytest could not collect.
COLLECTION_TEST_NAME = "<collection>"

# Both sides of the diff header: a renamed test file is named only on the b/ side.
_DIFF_PATHS_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_TEST_FILE_RE = re.compile(r"^(test_[^/]*|[^/]*_test)\.py$")


def target_test_files(pr: PullRequest) -> list[str]:
    """Graded pytest files, derived from this PR's own test patch.

    Scoped rather than the whole suite: tests/ holds ~40k tests and takes well
    over an hour per stage. JS (vitest), Rust and mcp-servers/* suites the patch
    may also touch belong to other toolchains and are not graded here.
    """
    pairs = _DIFF_PATHS_RE.findall(pr.test_patch or "")
    paths = {p for pair in pairs for p in pair}
    return sorted(
        p
        for p in paths
        if p.startswith("tests/")
        and _TEST_FILE_RE.match(p.rsplit("/", 1)[-1])
        and not p.startswith(IGNORED_TEST_DIRS)
    )


class McpContextForgeImageBase(Image):
    """Shared base: interpreter + uv + full-history clone. No pin, no scrub."""

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

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming
        # it is what would pin this shared image to one PR's commit.
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

# The project venv lives outside the repo so `git clean -fdx` in prepare.sh and
# the patches never touch it. uv must use the image's interpreter, never fetch one.
ENV UV_PROJECT_ENVIRONMENT={VENV_DIR} \\
    UV_PYTHON=/usr/local/bin/python3.13 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_NO_PROGRESS=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv=={UV_VERSION}" && uv --version

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

# Upstream rewrote main and deleted some base branches, so 7 base commits (#6715,
# #6808, #7155, #7219, #7244, #7264, #7415) are reachable only from GitHub's
# refs/pull/*/head and never arrive with a plain clone. Fetch exactly this commit
# when it is missing; a failed fetch fails the build. FETCH_HEAD is dropped so it
# holds nothing past the pin.
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    git fetch --no-tags origin [[SHA]]
    rm -f .git/FETCH_HEAD
fi

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
# From here the tree may be modified on purpose; no clean-tree assertion below.
export UV_PROJECT_ENVIRONMENT=[[VENV_DIR]]
export UV_PYTHON=/usr/local/bin/python3.13
export UV_PYTHON_DOWNLOADS=never

# --frozen installs exactly this commit's uv.lock and never re-resolves, so the
# relative `exclude-newer = "10 days"` in pyproject cannot pull in newer packages.
# The dev group comes with it by default. If an optional extra has no wheel for
# this platform, fall back to the extras upstream CI installs; the gate below
# checks the result either way.
uv sync --frozen --all-extras \
    || uv sync --frozen --extra plugins --extra llmchat --extra observability --extra redis

# ========== Section 3: GATE (non-tolerant, last) ==========
[[VENV_DIR]]/bin/python --version
[[VENV_DIR]]/bin/python -c "import mcpgateway, pytest, pytest_asyncio, pytest_timeout"

# Only files that already exist at this commit can be collected: a PR may CREATE
# the test file it grades.
_gate_files=""
for _f in [[TEST_FILES]]; do
    if [ -f "$_f" ]; then
        _gate_files="${_gate_files} ${_f}"
    fi
done
if [ -n "$_gate_files" ]; then
    [[VENV_DIR]]/bin/python -m pytest --collect-only -q -p no:cacheprovider $_gate_files \
        >/tmp/pytest_collect.log 2>&1 || { tail -50 /tmp/pytest_collect.log >&2; exit 1; }
    tail -1 /tmp/pytest_collect.log
    rm -f /tmp/pytest_collect.log
fi
echo DEPS_OK
"""


# The one test command every stage runs.
TEST_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

cd [[REPO_DIR]]

# Ten fix patches here change pyproject.toml/uv.lock (e.g. #6485 adds httpx2,
# #6846 moves a plugin into the cpex-output-length-guard package). The venv was
# synced at the base commit, so without a re-sync the fix stage cannot import its
# own new dependency. Re-sync from the patched lock only when it differs from
# HEAD; the baseline stage never does. A failed sync fails the stage.
if ! git diff --quiet HEAD -- pyproject.toml uv.lock; then
    export UV_PROJECT_ENVIRONMENT=[[VENV_DIR]]
    export UV_PYTHON=/usr/local/bin/python3.13
    export UV_PYTHON_DOWNLOADS=never
    uv sync --frozen --all-extras \
        || uv sync --frozen --extra plugins --extra llmchat --extra observability --extra redis
fi

# A file the test patch creates exists only in the test and fix stages.
_FILES=""
for _f in [[TEST_FILES]]; do
    if [ -f "$_f" ]; then
        _FILES="${_FILES} ${_f}"
    fi
done
test -n "$_FILES" || {
    echo "test: none of this PR's graded test files exist in this stage"
    exit 0
}

[[VENV_DIR]]/bin/python -m pytest [[PYTEST_FLAGS]] $_FILES 2>&1
"""


class McpContextForgeImageDefault(Image):
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
        return McpContextForgeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[VENV_DIR]]", VENV_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_FILES]]", " ".join(target_test_files(self.pr)))
            .replace("[[PYTEST_FLAGS]]", PYTEST_FLAGS)
        )

    def install_files(self) -> list[File]:
        """Files the provisioning layer depends on.

        Kept separate from the grading files so that re-cutting a patch does not
        invalidate the dependency-install layer.
        """
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        # --exclude='*.gif': #7264's fix_patch carries docs/docs/images/mcpgateway.gif
        # as a binary diff without a full index line, which `git apply` rejects
        # ("cannot apply binary patch ... without full index line"), aborting the
        # whole fix stage. It is the only binary file across all 53 PRs; docs
        # images are never read by the tests, so excluding them is lossless.
        stage_patches = {
            "run.sh": "",
            "test-run.sh": (
                "git apply --whitespace=nowarn --exclude='*.gif' /home/test.patch\n"
            ),
            "fix-run.sh": (
                "git apply --whitespace=nowarn --exclude='*.gif' "
                "/home/test.patch /home/fix.patch\n"
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

        # PR images are never touched by DockerfileEnhancer (it returns raw the
        # moment dependency() is an Image), so everything this layer needs is
        # written out here. The scrub lives here rather than in the shared base:
        # the base must keep full history so every PR of the shard can reach its
        # own commit.
        #
        # The scrub deliberately contains no `git reset`, no `git clean` and no
        # path-scoped checkout -- prepare.sh is allowed to have modified tracked
        # files, and any of those would silently revert that work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
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


PR_LO, PR_HI = 6395, 7586


@Instance.register("IBM", "mcp-context-forge_7586_to_6395")
class MCP_CONTEXT_FORGE_7586_TO_6395(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return McpContextForgeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Keys are pytest node ids (`tests/.../test_x.py::Class::test[param]`),
        # which are unique per test and carry no timing or counts.
        statuses = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        # Verbose line: `<nodeid> STATUS` plus an optional `(reason)`.
        verbose_re = re.compile(
            r"^(\S+\.py::.+?) (" + statuses + r")(?: \(.*\))?\s*$"
        )
        # -rA summary line: `STATUS <nodeid>` plus an optional ` - message`.
        summary_re = re.compile(r"^(" + statuses + r") (\S+\.py::.+?)(?: - .*)?\s*$")
        # A file that failed to import: `ERROR <path>` with no `::`.
        collect_re = re.compile(r"^ERROR (\S+\.py)(?: - .*)?\s*$")

        buckets: dict[str, set[str]] = {
            "PASSED": set(),
            "FAILED": set(),
            "SKIPPED": set(),
        }
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
                name, status = verbose_match.group(1), verbose_match.group(2)
                buckets[outcome[status]].add(name)
                continue

            summary_match = summary_re.match(line)
            if summary_match:
                status, name = summary_match.group(1), summary_match.group(2)
                buckets[outcome[status]].add(name)
                continue

            collect_match = collect_re.match(line)
            if collect_match:
                buckets["FAILED"].add(f"{collect_match.group(1)}::{COLLECTION_TEST_NAME}")

        passed = buckets["PASSED"]
        failed = buckets["FAILED"]
        skipped = buckets["SKIPPED"]

        # A name may live in only one bucket; failure wins over both others (a
        # test that passes and then errors in teardown is a failure).
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


# ---------------------------------------------------------------------------
# Routing for the bare "IBM/mcp-context-forge" key.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty. These records carry no
# number_interval, so they arrive on "IBM/mcp-context-forge". Nothing else
# registers that key today; the router still captures any earlier holder and
# hands every PR outside this interval back to it, so a future config keeps
# working.
_INCUMBENT = Instance._registry.get("IBM/mcp-context-forge")


@Instance.register("IBM", "mcp-context-forge")
class McpContextForgeBareName(Instance):
    """Not an implementation -- a router for the bare "IBM/mcp-context-forge" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return MCP_CONTEXT_FORGE_7586_TO_6395(pr, config, *args, **kwargs)

        if _INCUMBENT is None:
            raise ValueError(
                f"IBM/mcp-context-forge#{pr.number} falls outside the "
                f"{PR_LO}..{PR_HI} interval this config serves, and no other "
                f"config is registered for IBM/mcp-context-forge."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
