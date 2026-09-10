"""pytorch/ignite, PRs 2896-3239 (dataset PRs: 3203).

One era, one file. The dependency constants below are fixed for this range, so
there is no PR-number branching in this module at all -- the range lives in the
registration name and is matched by the enrichment pass.

Environment values are taken from the pre-existing ``ignite_3240_to_2896``, not invented.
``requirements-dev.txt`` is unpinned at every commit in range (bare ``numpy``,
``scikit-learn``, ``matplotlib``), so installing it alone resolves present-day
wheels against era source; TORCH_INSTALL and ERA_PINS below correct that.

Image split, following ``ignite_1104_to_564.py``: ``ImageBase`` establishes the
runtime and clones, then stops -- it does NOT check out and does NOT strip
history, because ``BASE_TAG`` is shared by every ignite era file. Pinning a
shared base to one PR's sha strips the other PRs' commits out of the clone. The
checkout and the history hardening both live in ``ImageDefault``.

Two ``DockerfileEnhancer`` interactions are load-bearing (``harness/image.py``):

* ``ImageBase.dependency()`` is a **str**, so its Dockerfile IS processed and
  ``enhance()`` would append a ``BASE_COMMIT``-pinned hardening block. Emitting
  ``# syntax=docker/dockerfile:1.6`` as line 1 makes ``enhance()`` return the
  file verbatim, which is why the ARG/ENV/label/CA-cert infrastructure it would
  otherwise contribute is spelled out here.
* ``ImageDefault.dependency()`` is an **Image**, so ``enhance()`` returns early.
  The hardening block is applied by hand and the base commit is interpolated
  literally: ``build_dataset`` passes BASE_COMMIT as a build arg only when
  ``dependency()`` is a string, so ``${BASE_COMMIT}`` would expand empty here.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "pytorch"
REPO = "ignite"

_INTERVAL_NAME = "ignite_3239_to_2896"

_PYTHON_IMAGE = "python:3.9-slim-bookworm"

BASE_TAG = "base-ignite-py39"

ESSENTIAL_DEPS = "pytest pytest-cov pytest-xdist pytest-timeout numpy scikit-learn"

OPTIONAL_INSTALL = (
    "grep -viE '^[[:space:]]*mkl([[:space:]]|$|[<>=!~])' requirements-dev.txt "
    "> /tmp/req-dev.txt && pip install -r /tmp/req-dev.txt"
)

DEPS_GATE = (
    "python -c \"import ignite, torch, pytest, numpy, sklearn, matplotlib; print('DEPS_OK')\""
)

_PY_TEST_FILE = re.compile(r"^(?:test_.+|.+_test)\.py$")


def test_targets(test_patch: str) -> str:
    """Space-joined repo-relative test files the gold test patch adds or modifies.

    Reads the ``+++ b/`` side so files the patch CREATES are included (the
    ``a/`` side drops them). Scoping the graded run to these files rather than
    the whole suite is what keeps a run finite: a full ``tests/`` run reaches
    ``tests/ignite/distributed/comp_models/test_native.py``, whose gloo spawn
    tests deadlock in-container -- measured on PR 2098, stuck at 44% for 44
    minutes with --timeout=600 never firing. It also drops the ~58 baseline
    failures from contrib logger deps (mlflow, clearml, neptune, polyaxon) that
    requirements-dev.txt fails to resolve on py3.9.

    Falls back to the whole suite only if a patch carries no recognisable test
    file at all, which no PR in this dataset does.
    """
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if path in ("/dev/null", ""):
            continue
        if not path.startswith("tests/"):
            continue
        if not _PY_TEST_FILE.match(path.rsplit("/", 1)[-1]):
            continue
        if path not in targets:
            targets.append(path)
    return " ".join(sorted(targets)) if targets else "tests/"



TORCH_INSTALL = (
    "pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || pip install torch torchvision"
)

ERA_PINS = ""

ERA_EXTRA = "visdom pytorch_fid scikit-learn scikit-image py-rouge scipy clearml matplotlib"


def prepare_script(pr: PullRequest) -> str:
    """Body of prepare.sh for one PR: pin the tree, install, then assert.

    Step order is load-bearing:
      1. recover the base commit (upstream may have deleted the branch it lived on)
      2. reset -> clean -> DETACHED checkout -> clean-tree assert
      3. optional bulk install (may fail)
      4. essential deps (may not fail)
      5. era pins, force-reinstalled over whatever step 3 resolved
      6. `pip install -e .`
      7. the hard gate

    `git clean -fdx` in step 2 matters because `git reset --hard` restores tracked
    files but leaves stray untracked ones behind (QC P5). Step 2 is also the ONLY
    place the clean-tree assert may run -- every step below it is permitted to
    dirty the tree, so a later assert would fail legitimate builds.
    """
    repo, sha, num = pr.repo, pr.base.sha, pr.number
    steps = [
        "set -e",
        f"cd /home/{repo} && (git cat-file -e {sha}^{{commit}} 2>/dev/null "
        f"|| git fetch --no-tags --depth=2147483647 origin {sha} "
        f'|| git fetch --no-tags origin "+refs/pull/{num}/head:refs/remotes/origin/pr-{num}")',
        f"cd /home/{repo} && git reset --hard && git clean -fdx "
        f"&& git checkout --detach {sha} && bash /home/check_git_changes.sh",
        'echo "setuptools<80" > /tmp/pip-constraints.txt',
        "export PIP_CONSTRAINT=/tmp/pip-constraints.txt",
        f"cd /home/{repo} && pip install -U pip wheel setuptools",
        f"cd /home/{repo} && ({TORCH_INSTALL})",
        'python -c "import torch; print(\'torch==\' + torch.__version__)" >> /tmp/pip-constraints.txt',
        'python -c "import torchvision; print(\'torchvision==\' + torchvision.__version__)" >> /tmp/pip-constraints.txt 2>/dev/null || true',
        f"cd /home/{repo} && {OPTIONAL_INSTALL} || true",
        f"cd /home/{repo} && pip install {ESSENTIAL_DEPS}",
    ]
    if ERA_EXTRA:
        steps.append(f"cd /home/{repo} && pip install {ERA_EXTRA} || true")
    if ERA_PINS:
        steps.append(f"cd /home/{repo} && pip install --force-reinstall {ERA_PINS}")
    steps.extend([
        f"cd /home/{repo} && pip install -e .",
        f"cd /home/{repo} && {DEPS_GATE}",
    ])
    return "\n###ACTION_DELIMITER###\n".join(steps)


def test_cmd() -> str:
    """The graded pytest prefix. Target files are appended by each run script.

    `--continue-on-collection-errors`: a test.patch importing a symbol the fix
    has not created yet raises a collection error, and pytest otherwise aborts
    the run -- destroying the signal for every other test in it.
    `--tb=no -rA`: no tracebacks, but a full verdict list, which is what
    parse_log reads.
    `--timeout=600 --timeout-method=thread`: the default signal method cannot
    interrupt a deadlocked `mp.spawn` child, which is how the full-suite run
    hung indefinitely; the thread method kills the process outright.
    """
    return (
        "pytest -v --no-header -rA --tb=no -p no:cacheprovider "
        "--continue-on-collection-errors --timeout=600 --timeout-method=thread"
    )


class ImageBase(Image):
    """Environment-only base image: runtime, TLS trust, toolchain, clone. Stops there.

    Shared by both gap intervals via the module-level BASE_TAG, so it must stay
    free of anything PR-specific.
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
        return _PYTHON_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = ORG, REPO
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential libgl1 libglib2.0-0 libgomp1 \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific layer: patches, run scripts, commit pin and git hardening."""

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
        targets = test_targets(self.pr.test_patch)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
                prepare_script(self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
TARGETS=""
for f in {targets}; do
    if [ -e "$f" ]; then
        TARGETS="$TARGETS $f"
    fi
done
if [ -z "$TARGETS" ]; then
    echo "run.sh: no gold test target exists at the base commit; nothing to run"
    exit 0
fi
{cmd} $TARGETS

""".format(repo=REPO, cmd=test_cmd(), targets=targets),
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
{cmd} {targets}

""".format(repo=REPO, cmd=test_cmd(), targets=targets),
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
{cmd} {targets}

""".format(repo=REPO, cmd=test_cmd(), targets=targets),
            ),
        ]

    def dockerfile(self) -> str:
        repo = REPO
        sha = self.pr.base.sha
        pre_copy = "".join(
            f"COPY {n} /home/\n" for n in ("check_git_changes.sh", "prepare.sh")
        )
        post_copy = "".join(
            f"COPY {n} /home/\n"
            for n in ("fix.patch", "test.patch", "run.sh", "test-run.sh", "fix-run.sh")
        )
        return f"""FROM {self.dependency().image_full_name()}

{pre_copy}
WORKDIR /home/{repo}

RUN bash /home/prepare.sh

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

{post_copy}"""


@Instance.register(ORG, _INTERVAL_NAME)
class IGNITE_3239_TO_2896(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        execution_pattern = re.compile(
            r"^(tests/\S+)[^\S\n]+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b[^\n]*?\[\s*\d+%\s*\]",
            re.MULTILINE,
        )
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
