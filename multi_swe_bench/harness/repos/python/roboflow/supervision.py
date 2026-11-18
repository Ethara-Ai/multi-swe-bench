"""roboflow/supervision config.

One shared base image plus one image per PR. The 29 rows span May-Oct 2026 on
``develop`` (one on ``release/latest``); every patch applies at ``base.sha``,
which is also the merge base with the PR head, so no anchor rewriting is
needed. The design is driven by three properties of the repo.

1. THE HISTORY IS 4.6 GB, A CHECKOUT IS ~120 MB.
   Notebook outputs and media committed over the years make a full clone
   impractical in every per-PR, per-arch image. The base holds only the
   toolchain; each PR image fetches its own ``base.sha`` at depth 1. The
   resulting repo has exactly one commit, so the fix cannot be read back out of
   history, and the hardening pass only has to drop the remote and refs.

2. DEPENDENCIES MOVE ACROSS THE RANGE.
   ``requires-python`` goes from >=3.9 to >=3.10, ``av`` is added and
   ``opencv-python`` is dropped (#2443), and ``geotiff``/``metrics`` extras come
   and go. Python 3.11 satisfies every row. The base pre-installs the common
   runtime and test dependencies; ``prepare.sh`` then installs the tree itself
   editable, once at the baseline and once with both patches applied, so a
   dependency the fix adds is already present when the graded stages run
   offline-ish. OpenCV is deliberately NOT in the base: rows before #2443 get it
   from their own ``pyproject.toml``, later rows test the OpenCV-free fallback.

3. TEST SELECTION.
   Only the ``test_*.py`` files the test patch touches are run. Helpers,
   fixtures, CI workflows and generator scripts (``_generate_*.py``) are
   skipped. A file the test patch creates does not exist at the baseline stage
   and is dropped there instead of being passed to pytest, which would abort
   the whole session over one missing path.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.11-bookworm"

# Runtime deps shared by every row plus the test-only imports seen across the
# range (pycocotools/sklearn for metric parity, rasterio for geotiff, pandas
# for the metrics extra). Pinned the way the repo's dev group pins them.
_BASE_PACKAGES = (
    "'pytest>=7.2.2,<10'",
    "'pytest-cov>=4,<8'",
    "'numpy>=1.21.2'",
    "'scipy>=1.10'",
    "'matplotlib>=3.6'",
    "'pillow>=9.4'",
    "'av>=14.2'",
    "'defusedxml>=0.7.1'",
    "'pydeprecate>=0.9,<0.14'",
    "'pyyaml>=5.3'",
    "'requests>=2.26'",
    "'tqdm>=4.62.3'",
    "'pandas>=2'",
    "'scikit-learn>=1.7'",
    "pycocotools",
    "'rasterio>=1.3'",
    "packaging",
)


def _split_diffs(patch: str) -> list[str]:
    """Split a patch into its per-file ``diff --git`` sections."""
    if not patch:
        return []
    return re.findall(r"(?:^diff --git .*?)(?=^diff --git |\Z)", patch, re.M | re.S)


def _diff_paths(diff: str) -> set[str]:
    """Every path a single file-diff refers to."""
    out: set[str] = set()
    for m in re.finditer(r'^(?:---|\+\+\+) (?:"?[ab]/)?(.+?)"?$', diff, re.M):
        path = m.group(1).strip()
        if path and path != "/dev/null":
            out.add(path)
    header = diff.split("\n", 1)[0]
    m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', header)
    if m:
        out.add(m.group(1))
        out.add(m.group(2))
    return out


def _test_files(test_patch: str) -> list[str]:
    """The ``tests/**/test_*.py`` files the test patch touches."""
    files: set[str] = set()
    for diff in _split_diffs(test_patch):
        for path in _diff_paths(diff):
            name = path.rsplit("/", 1)[-1]
            if path.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
                files.add(path)
    return sorted(files)


class SupervisionImageBase(Image):
    """Shared toolchain image: Python 3.11 plus pre-installed dependencies.

    Holds no repository, so it is valid for every PR. Emitting the BuildKit
    syntax directive opts out of ``DockerfileEnhancer``, which would otherwise
    add a full clone pinned to one PR's ``BASE_COMMIT``; this file therefore
    emits the proxy, cert and label infrastructure itself.
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

    def dependency(self) -> Union[str, "Image"]:
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        packages = " \\\n    ".join(_BASE_PACKAGES)

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    MPLBACKEND=Agg

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# libgl1/libglib2.0-0: runtime libs for opencv-python on the pre-#2443 rows.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    libgl1 \\
    libglib2.0-0 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        pip install --upgrade pip setuptools wheel && break; \\
        [ "$i" -lt 5 ] || exit 1; sleep 15; \\
    done; \\
    for i in 1 2 3 4 5; do \\
        pip install \\
    {packages} && break; \\
        [ "$i" -lt 5 ] || exit 1; sleep 15; \\
    done; \\
    python -c "import numpy, scipy, matplotlib, PIL, av, sklearn, pycocotools, rasterio, pandas, pytest"

WORKDIR /home/

CMD ["/bin/bash"]
"""


class SupervisionImageDefault(Image):
    """Per-PR image: fetches ``base.sha`` at depth 1 and installs the tree."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return SupervisionImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        tests = " ".join(_test_files(self.pr.test_patch))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
# Shared test driver for all three stages: the test files the test patch
# touches, minus any that do not exist in this tree yet.
set -o pipefail

FILES=()
for f in [[TESTS]]; do
    if [ -f "$f" ]; then
        FILES+=("$f")
    fi
done

if [ ${#FILES[@]} -eq 0 ]; then
    echo "run_tests: none of the target test files exist in this tree, no tests to run"
    exit 0
fi

echo "run_tests: ${FILES[*]}"
python -m pytest -v --color=no -p no:cacheprovider "${FILES[@]}" || true
""".replace("[[TESTS]]", tests),
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

# Depth-1 fetch of the one commit this instance is anchored to; see point 1
# of the config module docstring. Retried: DNS to github.com flakes mid-build.
rm -rf /home/[[REPO]]
git init -q /home/[[REPO]]
cd /home/[[REPO]]
git remote add origin https://github.com/[[ORG]]/[[REPO]].git
for i in 1 2 3 4 5; do
    git fetch --no-tags --depth 1 origin [[SHA]] && break
    echo "prepare: fetch failed (attempt $i), retrying" >&2
    [ "$i" -lt 5 ] || exit 1
    sleep 15
done
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "[[SHA]]"
bash /home/check_git_changes.sh

install() {
    for i in 1 2 3 4 5; do
        pip install -e ".[geotiff,metrics]" && return 0
        echo "prepare: pip install failed (attempt $i), retrying" >&2
        sleep 15
    done
    return 1
}

# Install the baseline tree, then the fully patched one, so any dependency
# the fix adds is already present for the graded stages. The tree is then
# restored; the editable install keeps pointing at src/. Reverse-applied rather
# than cleaned: some added files match .gitignore (#2435 adds
# src/supervision/_cv2/data/*), which `git clean -fd` leaves behind and
# `git status` does not report, so the graded apply would hit "already exists".
install
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
install
git apply -R --whitespace=nowarn /home/test.patch /home/fix.patch
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh
git apply --check /home/test.patch /home/fix.patch
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The repo already holds a single commit (depth-1 fetch); this pass
        # removes the remote and every ref so nothing can be fetched or read
        # beyond base.sha, and asserts that state.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    rm -f .git/shallow.lock .git/FETCH_HEAD; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "1"

CMD ["/bin/bash"]
"""


@Instance.register("roboflow", "supervision")
class Supervision(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SupervisionImageDefault(self.pr, self._config)

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

        # pytest -v progress lines: "<nodeid> STATUS [ NN%]". Node ids may hold
        # spaces inside parametrize brackets, so match lazily up to the status.
        line_re = re.compile(
            r"^(\S.*?::.*?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
        )
        for raw in test_log.splitlines():
            m = line_re.match(raw.strip())
            if not m:
                continue
            tid, status = m.group(1), m.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(tid)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
