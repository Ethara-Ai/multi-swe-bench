from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: PRs 5005..5054 (quantumlib/Cirq, Feb-Mar 2022 target-gateset work).
#
# One runtime for every graded commit here, and therefore exactly ONE base image
# -- there is no version conflict to fork over. Verified rather than assumed:
# `pip install -e ./cirq-core[contrib]` succeeds on python:3.10-slim at these
# commits in ~1m46s, cirq-google installs on top of it, and `import cirq` yields
# 0.14.0.dev.
PYTHON_IMAGE = "python:3.10-slim"

# --continue-on-collection-errors is the single most important flag in this file.
# A PR's test patch normally adds a *_test.py for a module that does not exist
# until the fix patch lands, so that file raises a collection error in the `test`
# stage. Without this flag pytest prints "Interrupted: 1 error during collection"
# and runs NOTHING ELSE -- measured on 4 of these 5 PRs, where the whole test
# stage collapsed to a single line. Everything the fix stage passed then fell
# into n2p by default and p2p came out ZERO:
#
#     PR 5025 without the flag: run=3002 test=1 fix=3033 -> n2p=3033, p2p=0
#     PR 5005 without the flag: run=9    test=1 fix=15   -> n2p=15,   p2p=0
#     PR 5005 with the flag:    run=9    test=10 fix=15  -> f2p=2, p2p=8
#
# The flag lets the collectible files run and report normally, so the tests that
# were already passing stay visible as the regression set.
PYTEST_FLAGS = (
    "--no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors"
)


# ---------------------------------------------------------------------------
# What does this PR test, and which packages must be installed to run it?
# ---------------------------------------------------------------------------
# Both answers are DERIVED from the PR's own test patch. Nothing here is a
# per-PR lookup table: add a sixth PR touching a seventh cirq package and this
# keeps working untouched.
#
# The predecessor adapter (cirq_5650_to_4103.py) carried a hand-maintained
# 24-entry {pr_number: [test files]} dict. That dict is exactly "files in
# test_patch whose name ends in _test.py" -- confirmed against every one of
# these 5 PRs, so deriving it is behaviour-preserving, not a guess.
_TEST_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+_test\.py) b/", re.M)

# Cirq is a multi-package repo (cirq-core, cirq-google, cirq-ionq, ...). Every
# other package imports cirq-core, so it is always installed; the rest are
# installed only when this PR's tests actually live in them.
_ROOT_PACKAGE = "cirq-core"


def target_test_files(pr: PullRequest) -> list[str]:
    """The graded test files, taken from the PR's own test patch."""
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch or "")))


def target_packages(pr: PullRequest) -> list[str]:
    """Workspace packages that must be pip-installed for those tests to import."""
    owners = {path.split("/")[0] for path in target_test_files(pr)}
    return sorted(owners | {_ROOT_PACKAGE})


class CirqImageBase(Image):
    """The single shared base image: a runtime plus the graded commit.

    All five PRs build from THIS class, out of ONE build-context folder
    ("base"), from a Dockerfile whose text is byte-identical for every one of
    them -- BASE_COMMIT is a bare ARG, so the only thing that varies between PRs
    is a build argument. Docker reuses every layer up to the clone; only the
    checkout/scrub layers, whose cache key includes ${BASE_COMMIT}, are rebuilt.
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # NOT image_tag(). The tag separates the PRs; the workdir is the build
        # CONTEXT folder, and holding it constant is what makes the five builds
        # share one Dockerfile and one cached layer chain.
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        # The rendered Dockerfile deliberately carries NO comments beyond the
        # BuildKit syntax directive on line 1 (a parser directive, not a comment
        # -- removing it changes how the file is built). Reasoning lives here:
        #
        #   * The syntax directive also makes DockerfileEnhancer a no-op, so what
        #     is written below is exactly what gets built. The predecessor adapter
        #     omitted it and relied on the enhancer to inject the ARGs, proxy
        #     block, CA farm, clone, checkout and scrub; doing it explicitly means
        #     the file can be read and QC'd as-is.
        #   * python:3.10-slim ships neither git nor a C toolchain, and both are
        #     required: git to clone/checkout, build-essential because several of
        #     cirq's dependencies build C extensions from source on this image.
        #   * The scrub RUN leaves ONLY the base commit's history in the image.
        #     Its four `test` assertions are the point of the block -- they turn a
        #     mispinned or leaky image into a hard build failure rather than a
        #     dataset that silently leaks the fix.
        #   * The submodule RUN is written `test -f .gitmodules && ... || true`
        #     rather than with an `if`, so the instruction stays one flat
        #     expression; the trailing `|| true` makes it a no-op for a repo with
        #     no .gitmodules (Cirq has none, so it is insurance only).
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
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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

RUN test -f .gitmodules && \\
    git submodule foreach --recursive ' \\
        git checkout --detach HEAD; \\
        git remote remove origin 2>/dev/null || true; \\
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d; \\
        git reflog expire --expire=now --all; \\
        git reflog expire --expire-unreachable=now --all; \\
        git gc --prune=now --aggressive; \\
        rm -f .git/objects/info/alternates; \\
    ' || true

{self.clear_env}

CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the Dockerfile's HEAD/refs
# assertions only prove WHICH commit is checked out -- a dirty tree satisfies all
# of them. The *.egg-info / *.egg-link directories that `pip install -e` creates
# are .gitignored in this repo, so they are invisible to `git status --porcelain`
# and correctly ignored here.
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


APPLY_PATCH_SH = r"""#!/bin/bash
# Apply one patch as completely as possible, then ALWAYS exit 0. The caller must
# reach pytest no matter how patching went: a stage that dies while patching
# reports zero tests, which the harness cannot tell apart from "the fix does not
# work".
#
# The cascade below is tried strictest-first and leaves a marker the run scripts
# turn into a loud banner whenever anything short of the fast path was needed.
patch_file="$1"

test -s "$patch_file" || {
  echo "apply_patch: $patch_file is empty or missing; nothing to apply"
  exit 0
}

git apply --whitespace=nowarn "$patch_file" 2>/dev/null && {
  echo "apply_patch: $patch_file -> applied cleanly"
  exit 0
}

git apply --3way --whitespace=nowarn "$patch_file" 2>/dev/null && {
  echo "apply_patch: $patch_file -> applied via 3-way merge"
  echo "3way $patch_file" >> /tmp/apply_patch_rejects
  exit 0
}

git apply -C1 --recount --whitespace=nowarn "$patch_file" 2>/dev/null && {
  echo "apply_patch: $patch_file -> applied with reduced context"
  echo "reduced-context $patch_file" >> /tmp/apply_patch_rejects
  exit 0
}

patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch -r /dev/null \
    -i "$patch_file" >/dev/null 2>&1 && {
  echo "apply_patch: $patch_file -> applied with fuzz"
  echo "fuzz $patch_file" >> /tmp/apply_patch_rejects
  exit 0
}

echo "apply_patch: $patch_file -> DID NOT APPLY"
echo "rejected $patch_file" >> /tmp/apply_patch_rejects
exit 0
"""


PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# setuptools is pinned below 70 because these 2022-era setup.py files still use
# APIs that setuptools 70 removed; without the pin the editable install aborts
# before a single dependency is fetched.
pip install --upgrade pip "setuptools<70" wheel

# Only the packages this PR's graded tests actually live in, plus cirq-core,
# which every other cirq package imports. Derived from the test patch -- see
# target_packages(). Installing the whole repo would drag in cirq-ionq,
# cirq-rigetti, cirq-aqt and friends for no benefit.
[[INSTALL_PACKAGES]]

pip install pytest

python --version
python -c "import cirq; print('cirq', cirq.__version__)"

# The editable installs only write .egg-info / .egg-link, both .gitignored, so
# the graded tree is byte-identical to base.sha. Prove it rather than assume it.
git checkout -- .
git clean -fdq -e '*.egg-info' -e '*.egg-link'
bash /home/check_git_changes.sh
"""


TEST_SH = r"""#!/bin/bash
# The one command every stage runs. No `set -e`: a non-zero pytest exit is the
# NORMAL outcome of a stage whose tests fail, and the log is the deliverable.
set -o pipefail
export CI=true
export TZ=UTC

cd /home/[[REPO]] || exit 1

# A test file named by the test patch does not exist until that patch is
# applied, so the `run` stage must skip it rather than hand pytest a path that
# makes it exit 4 before running anything.
EXISTING=""
for f in [[TEST_FILES]]; do
    test -f "$f" && EXISTING="$EXISTING $f"
done

test -n "$EXISTING" || {
    echo "test: none of the graded test files exist at this stage: [[TEST_FILES]]"
    exit 0
}

python -m pytest $EXISTING [[PYTEST_FLAGS]] 2>&1
exit 0
"""


PATCH_BANNER_SH = r"""
test -s /tmp/apply_patch_rejects && {
    echo "=================================================================="
    echo "WARNING: a patch did NOT apply by the fast path -- results suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
}
"""


class CirqImageDefault(Image):
    """The per-PR layer: patches, run scripts, and one editable install."""

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
        return CirqImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        installs = "\n".join(
            f'pip install -e "./{pkg}[contrib]"'
            if pkg == _ROOT_PACKAGE
            else f'pip install -e "./{pkg}"'
            for pkg in target_packages(self.pr)
        )
        return (
            template.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[INSTALL_PACKAGES]]", installs)
            .replace("[[TEST_FILES]]", " ".join(target_test_files(self.pr)))
            .replace("[[PYTEST_FLAGS]]", PYTEST_FLAGS)
        )

    def install_files(self) -> list[File]:
        """Files the editable-install layer depends on.

        Kept apart from the grading files so that editing a run script or
        re-cutting a patch does not invalidate the most expensive layer in the
        build. Docker keys a layer on the files COPYed before it.
        """
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        stage_scripts = {
            "run.sh": "",
            "test-run.sh": "bash /home/apply_patch.sh /home/test.patch\n",
            "fix-run.sh": (
                "bash /home/apply_patch.sh /home/test.patch\n"
                "bash /home/apply_patch.sh /home/fix.patch\n"
            ),
        }

        staged = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -o pipefail\n\n"
                    "cd /home/[[REPO]] || exit 1\n"
                    "rm -f /tmp/apply_patch_rejects\n"
                    "git checkout -- . 2>/dev/null || true\n"
                    + patch_step
                    + PATCH_BANNER_SH
                    + "\nbash /home/test.sh\nexit 0\n"
                ),
            )
            for name, patch_step in stage_scripts.items()
        ]

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "apply_patch.sh", APPLY_PATCH_SH),
            File(".", "test.sh", self._expand(TEST_SH)),
        ] + staged

    def files(self) -> list[File]:
        # The harness writes every File returned here into the build context;
        # dockerfile() below decides the COPY order.
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        # A PR layer is COPYs plus one `RUN bash /home/prepare.sh`, nothing else:
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes after checkout.
        #
        # The COPYs are split around that RUN on purpose. Everything the install
        # reads goes before it; the patches and run scripts go after, so changing
        # them re-copies a few kilobytes instead of re-running the install.
        return f"""FROM {name}:{tag}

{self.global_env}

{copies(self.install_files())}
RUN bash /home/prepare.sh

{copies(self.grading_files())}
{self.clear_env}

"""


@Instance.register("quantumlib", "Cirq_5054_to_5005")
class Cirq5005To5054(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CirqImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Parses pytest's `-rA` short summary, which prints one
        # `<STATUS> <nodeid>` line per test. Anchored at the start of the line so
        # a traceback or an assertion diff that happens to contain the word
        # PASSED cannot be mistaken for a result.
        result_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)", re.M
        )

        # Table dispatch rather than branching: pytest's XPASS is a pass, XFAIL a
        # skip, and a collection ERROR is a failure of that file.
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

        # A name may live in only one bucket; failure wins over both others.
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
