import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The graded test command, identical in all three run scripts (QC item P7).
# `--continue-on-collection-errors`: a test.patch that imports a module the fix
# has not created yet raises a collection error, and pytest otherwise aborts the
# ENTIRE suite -- destroying the test-stage signal for all ~500 tests.
# `--deselect .../test_timing.py::test_timer`: that test sleeps 0.2s and asserts
# elapsed wall-clock within tolerance. It is unrelated to every patch in this era
# and flakes on loaded hosts. Left in, it both invalidated good instances (951,
# 729) and FABRICATED a spurious f2p for 1005, whose patches never touch it.
TEST_CMD = (
    "pytest -v --continue-on-collection-errors "
    "--deselect tests/ignite/handlers/test_timing.py::test_timer tests/"
)


class ImageBase(Image):
    """Environment-only base image.

    Per project convention this image stops at the `git clone`: it establishes
    the runtime, the proxy/TLS trust and the toolchain, clones the repo, and
    ends. It does NOT check out the base commit and does NOT strip git history --
    both of those are PR-specific and live in ImageDefault's Dockerfile.

    The Dockerfile is emitted complete, including the BuildKit syntax directive.
    That is deliberate: DockerfileEnhancer.enhance() returns the Dockerfile
    untouched when it already carries the directive (image.py:317), which is what
    keeps the enhancer from appending its own history-scrub block here. Because
    the enhancer is bypassed, this method must itself provide everything the
    enhancer would normally add -- ARGs, the proxy/TLS ENV block, OCI labels and
    the CA-cert symlink farm -- and it does.
    """

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
        # Pinned to the Debian release, not just the Python minor: the bare
        # `python:3.9-slim` tag floated to Debian 13 "trixie" during development,
        # which dropped `libgl1-mesa-glx` and broke the build.
        return "python:3.9-slim-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    # One base per ERA, not per PR. ImageBase no longer checks out BASE_COMMIT
    # (that moved to ImageDefault), so every PR in 564..1104 renders an identical
    # base Dockerfile. Tagging per-PR built and stored 5 copies of one image;
    # Image.__eq__/__hash__ key on image_full_name(), so a shared tag collapses
    # them to a single build.
    ERA_TAG = "base-1104-to-564"

    def image_tag(self) -> str:
        return self.ERA_TAG

    def workdir(self) -> str:
        return self.ERA_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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

WORKDIR /home/

# python:3.9-slim-bookworm ships no git and no toolchain; ignite's test
# dependencies build C extensions, so build-essential is required here.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential libgl1 libglib2.0-0 libgomp1 \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific layer: patches, run scripts, commit pin and git hardening.

    The base image deliberately stops at the clone, so everything that is
    specific to THIS pull request happens here: recovering the base commit if
    upstream deleted its branch, pinning the tree to that commit, and stripping
    the git history down to it with integrity asserts.

    The hardening is expressed as Dockerfile RUN layers -- not inside
    prepare.sh -- so it is auditable in the image recipe itself.

    Note the commit is interpolated literally rather than read from
    ${BASE_COMMIT}: build_dataset.py only passes REPO_URL/BASE_COMMIT as build
    args when dependency() is a string, i.e. for base images (build_dataset.py
    :623-629). A PR image receives no build args, so ${BASE_COMMIT} would expand
    to the empty string here.
    """

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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                # Plain string, never .format()'ed -- the `|| { ...; }` brace
                # group would otherwise be read as a format replacement field.
                """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the HEAD/refs asserts only prove
# WHICH commit is checked out -- a dirty tree satisfies all of them.
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 \\
    || { echo "check_git_changes: Not inside a git repository"; exit 1; }

test -z "$(git status --porcelain)" || {
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain | head -20
    exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                # Dependency setup only. The commit pin and the history strip are
                # Dockerfile RUN layers above, not part of this script.
                """set -e
###ACTION_DELIMITER###
cd /home/{pr.repo} && git reset --hard && bash /home/check_git_changes.sh
###ACTION_DELIMITER###
cd /home/{pr.repo} && (pip install torch==1.13.1 torchvision==0.14.1 --index-url https://download.pytorch.org/whl/cpu || pip install torch==1.13.1 torchvision==0.14.1) || true
###ACTION_DELIMITER###
cd /home/{pr.repo} && pip install -r requirements-dev.txt || true
###ACTION_DELIMITER###
cd /home/{pr.repo} && pip install numpy==1.23.5 mock pytest pytest-cov scikit-learn tqdm tensorboardX matplotlib pandas neptune-client visdom==0.1.8.9 || true
###ACTION_DELIMITER###
cd /home/{pr.repo} && pip install -e . || true
###ACTION_DELIMITER###
cd /home/{pr.repo} && bash /home/check_git_changes.sh || true""".format(
                    pr=self.pr
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
{cmd}

""".format(repo=self.pr.repo, cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}

""".format(repo=self.pr.repo, cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}

""".format(repo=self.pr.repo, cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        num = self.pr.number
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {self.dependency().image_full_name()}

{copy_commands}
WORKDIR /home/{repo}

# Recover the base commit when upstream deleted the branch it lived on
# (ignite #1005 is based on `idist`, since removed). GitHub still serves such a
# commit by SHA and via refs/pull/<N>/head. The cat-file guard makes this a
# no-op when the clone already contains it.
RUN git cat-file -e {sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {sha} \\
    || git fetch --no-tags origin "+refs/pull/{num}/head:refs/remotes/origin/pr-{num}"

# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh
"""


@Instance.register("pytorch", "ignite_1104_to_564")
class IGNITE_1104_TO_564(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        # Colour codes must be stripped before matching: pytest emits them
        # whenever stdout is a TTY, and the swe-rex session is a pty.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # `pytest -v` progress lines carry every verdict, so read them rather
        # than the short summary (which `-r` configuration can suppress).
        # `[^\n]*?` absorbs a skip reason (`SKIPPED (no cuda) [ 50%]`), and
        # horizontal-only whitespace keeps every match on a single line.
        execution_pattern = re.compile(
            r"^(tests/\S+)[^\S\n]+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b[^\n]*?\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
        # Short-summary lines, as a fallback for verdicts the progress line missed.
        summary_pattern = re.compile(
            r"^(FAILED|ERROR)[^\S\n]+(tests/\S+?)(?:[^\S\n]+-.*)?$", re.MULTILINE
        )

        for match in execution_pattern.finditer(log):
            test_name, status = match.group(1), match.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        for match in summary_pattern.finditer(log):
            failed_tests.add(match.group(2))

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A rerun/flaky test reported twice would otherwise raise
        # ValueError and abort the whole instance.
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
