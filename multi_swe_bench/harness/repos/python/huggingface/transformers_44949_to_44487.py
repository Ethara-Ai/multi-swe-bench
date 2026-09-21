import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


def _strip_binary_diffs(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(s for s in sections if s and "Binary files " not in s)


_TEST_BODY = """
set +e
TEST_FILES=$(grep -E '^\\+\\+\\+ b/' /home/test.patch \\
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \\
    | grep -E '(^|/)(test_[^/]*\\.py|[^/]*_test\\.py)$' \\
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

echo "running pytest on:$TEST_TARGETS"
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider -p no:rich -o log_cli=false --continue-on-collection-errors $TEST_TARGETS
"""

_OFFLINE_ENV = """export HF_HOME=/home/hf_cache
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export HTTP_PROXY=http://127.0.0.1:9
export HTTPS_PROXY=http://127.0.0.1:9
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export NO_PROXY=localhost,127.0.0.1,::1
export no_proxy=localhost,127.0.0.1,::1
"""

_PREPARE = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "[[SHA]]"
bash /home/check_git_changes.sh

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cat > /tmp/era-constraints.txt <<'EOF'
torch<2.12
torchvision<0.27
torchaudio<2.12
accelerate<1.14
huggingface-hub<1.8
safetensors<0.8
tokenizers<0.23
datasets<4.8.5
numpy<2.4.4
ipython<9.12
EOF
export PIP_CONSTRAINT=/tmp/era-constraints.txt

pip install --no-cache-dir 'torch>=2.4' torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu \\
    || pip install --no-cache-dir 'torch>=2.4' torchvision torchaudio \\
    || true
pip install --no-cache-dir -e '.[testing,torch]' || pip install --no-cache-dir -e '.[torch]' || true
pip install --no-cache-dir 'pytest>=7.2.0,<9.0.0' 'pytest-asyncio>=1.2.0' pytest-xdist pytest-timeout pytest-order pytest-env pytest-random-order 'pytest-rerunfailures<16.0' timeout-decorator parameterized psutil || true
pip install --no-cache-dir ipython || true
export HF_HOME=/home/hf_cache
(
[[TEST_BODY]]
) || true
python - <<'EOF' || true
import pathlib
import re
from huggingface_hub import HfApi, hf_hub_download
text = pathlib.Path("/home/test.patch").read_text(errors="replace")
added = chr(10).join(line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
quote = "[" + chr(34) + chr(39) + "]"
repo_ids = sorted(set(re.findall(quote + "([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+)" + quote, added)))
api = HfApi()
for repo_id in repo_ids:
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except Exception:
        continue
    total = 0
    for sibling in info.siblings:
        size = sibling.size or 0
        if size > 52428800 or total + size > 314572800:
            continue
        try:
            hf_hub_download(repo_id, sibling.rfilename, revision=info.sha)
            total += size
        except Exception:
            pass
    print("prefetched", repo_id, total)
EOF
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1

python -c "import torch; print('torch', torch.__version__)"
python -c "import pytest; print('pytest', pytest.__version__)"
python -c "import transformers; print('transformers', transformers.__version__)"
python -c "import accelerate; print('accelerate', accelerate.__version__)"
python -c "import IPython; print('IPython', IPython.__version__)"
echo "DEPS_OK"
"""


class HuggingFaceTransformersImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-44949_to_44487"

    def workdir(self) -> str:
        return "base-44949_to_44487"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

{self.global_env}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore \\
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \\
    HF_HUB_DISABLE_TELEMETRY=1 \\
    TOKENIZERS_PARALLELISM=false

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        bash build-essential ca-certificates curl git libffi-dev libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return HuggingFaceTransformersImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_script(self) -> str:
        return (
            _PREPARE.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_BODY]]", _TEST_BODY)
        )

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
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
            File(".", "prepare.sh", self._prepare_script()),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
[[OFFLINE_ENV]]
cd /home/[[REPO]]
""".replace("[[REPO]]", repo_name)
                .replace("[[OFFLINE_ENV]]", _OFFLINE_ENV)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
[[OFFLINE_ENV]]
cd /home/[[REPO]]
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo_name)
                .replace("[[OFFLINE_ENV]]", _OFFLINE_ENV)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
[[OFFLINE_ENV]]
cd /home/[[REPO]]
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo_name)
                .replace("[[OFFLINE_ENV]]", _OFFLINE_ENV)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

RUN set -eux; \\
    git checkout --detach "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
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
"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_VERBOSE_LINE = re.compile(
    r"^(\S.*?\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)"
)

_PASSED_OUTCOMES = {"PASSED", "XPASS"}
_FAILED_OUTCOMES = {"FAILED", "ERROR"}
_SKIPPED_OUTCOMES = {"SKIPPED", "XFAIL"}


def parse_pytest_verbose_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        match = _VERBOSE_LINE.match(line.rstrip())
        if not match:
            continue

        name, outcome = match.group(1), match.group(2)
        if outcome in _PASSED_OUTCOMES:
            passed_tests.add(name)
        elif outcome in _FAILED_OUTCOMES:
            failed_tests.add(name)
        elif outcome in _SKIPPED_OUTCOMES:
            skipped_tests.add(name)

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("huggingface", "transformers_44949_to_44487")
class TRANSFORMERS_44949_TO_44487(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
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
        return parse_pytest_verbose_log(log)
