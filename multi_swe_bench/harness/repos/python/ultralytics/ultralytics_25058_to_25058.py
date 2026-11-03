import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "ultralytics_25058_to_25058"
_PYTHON_IMAGE = "python:3.11.10-slim-bookworm"
_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_TORCH = "torch==2.7.1"
_TORCHVISION = "torchvision==0.22.1"
_CLIP = "git+https://github.com/ultralytics/CLIP.git@7ffa84b3bfa40c42ecc1c77147a855e69cb2dd40"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_TEST_FILE = re.compile(r"^tests/(?:[^/]+/)*test_[^/]+\.py$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NODE = r"\S+\.py::[^\s\[]+(?:\[[^\]]*\])?"
_VERBOSE = re.compile(
    rf"^(?P<name>{_NODE})\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_SUMMARY = re.compile(
    rf"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>{_NODE})(?:\s+-\s.*)?$"
)
_PASS = {"PASSED", "XPASS"}
_FAIL = {"FAILED", "ERROR"}
_SKIP = {"SKIPPED", "XFAIL"}

_RUN_ENV = """export CI=true
export YOLO_AUTOINSTALL=false
export PIP_DISABLE_PIP_VERSION_CHECK=1
"""

_RUN_TESTS = """TARGETS=""
for f in {test_files}; do
    if [ -f "$f" ]; then
        TARGETS="$TARGETS $f"
    fi
done
if [ -z "$TARGETS" ]; then
    exit 0
fi
python -m pytest -v -rA --color=no -p no:cacheprovider $TARGETS
"""

_WARM_ASSETS = """import re
import shutil
import sys
from pathlib import Path

from tests import MODEL, SOLUTION_ASSETS
from ultralytics.utils import ASSETS_URL, WEIGHTS_DIR
from ultralytics.utils.downloads import attempt_download_asset, safe_download

names = set([MODEL.name])
for path in sys.argv[1:]:
    names.update(re.findall(r"[\\"']([A-Za-z0-9_.-]+\\.pt)[\\"']", Path(path).read_text()))
for name in sorted(names):
    try:
        attempt_download_asset(WEIGHTS_DIR / name)
    except Exception as e:
        print("warm-up skipped", name, e)
if not MODEL.exists() and (WEIGHTS_DIR / MODEL.name).exists():
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WEIGHTS_DIR / MODEL.name, MODEL)
cache_dir = WEIGHTS_DIR / "solution_assets"
cache_dir.mkdir(parents=True, exist_ok=True)
for asset in SOLUTION_ASSETS.values():
    if not (cache_dir / asset).exists():
        try:
            safe_download(url=ASSETS_URL + "/" + asset, dir=cache_dir)
        except Exception as e:
            print("warm-up skipped", asset, e)
try:
    from ultralytics.nn.text_model import build_text_model

    build_text_model("clip:ViT-B/32", device="cpu")
except Exception as e:
    print("warm-up skipped clip", e)
"""


def _test_files(pr: PullRequest) -> str:
    files = []
    for path in _DIFF_FILE.findall(pr.test_patch or ""):
        if _TEST_FILE.match(path) and path not in files:
            files.append(path)
    return " ".join(files)


class Ultralytics25058To25058ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential libgl1 libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Ultralytics25058To25058ImageDefault(Image):
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
        return Ultralytics25058To25058ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_files = _test_files(self.pr)
        run_tests = _RUN_TESTS.format(test_files=test_files)
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
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

{run_env}export PIP_NO_CACHE_DIR=1
python -m pip install --index-url {torch_index} "{torch}" "{torchvision}"
python -m pip install -e ".[solutions]" pytest "{clip}"

python - {test_files} <<'PY' || true
{warm_assets}PY

python -c "import torch, torchvision, cv2, shapely, lap, clip, streamlit, flask, pytest, ultralytics, tests; from ultralytics import solutions; print('DEPS_OK', ultralytics.__version__, torch.__version__)"
""".format(
                    repo=repo,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    torch_index=_TORCH_INDEX,
                    torch=_TORCH,
                    torchvision=_TORCHVISION,
                    clip=_CLIP,
                    test_files=test_files,
                    warm_assets=_WARM_ASSETS,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch

{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{run_tests}""".format(repo=repo, run_env=_RUN_ENV, run_tests=run_tests),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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
"""


@Instance.register("ultralytics", "ultralytics")
@Instance.register("ultralytics", _INTERVAL)
class ULTRALYTICS_25058_TO_25058(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Ultralytics25058To25058ImageDefault(self.pr, self._config)

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

        for raw in _ANSI.sub("", log).splitlines():
            line = raw.strip()
            match = _VERBOSE.match(line) or _SUMMARY.match(line)
            if not match:
                continue
            name = match.group("name")
            status = match.group("status")
            if status in _FAIL:
                failed_tests.add(name)
            elif status in _PASS:
                passed_tests.add(name)
            elif status in _SKIP:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
