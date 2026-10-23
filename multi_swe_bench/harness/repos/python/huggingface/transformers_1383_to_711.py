"""huggingface/transformers, PRs #711-#1383 (June - October 2019).

Registered under the number_interval key `transformers_1383_to_711`; route rows
here by setting `number_interval` on each dataset row. No existing config fits
this era:

  * The package is not yet `transformers`. Bases ship `pytorch_pretrained_bert`
    (#711, #757, #776) or `pytorch_transformers` (#1139, #1203, #1374); several
    fix patches perform the rename, so the test-stage import fails and the
    fix-stage import succeeds. `_parse_with_collection` turns that into a
    `<path>::<collection>` fail-to-pass entry.
  * There is no `tokenizers` dependency yet, so no Rust toolchain is needed.
  * Tests are `*_test.py` modules and slow tests use `@pytest.mark.slow`, which
    is left disabled (no `--runslow`), so network-heavy checkpoints are skipped
    identically in every stage.

Environment: python 3.8 + torch 1.10.2. 1.10.2 is the newest torch that still
runs this era's uint8 attention masks (deprecated, not removed) and it ships
cp38 wheels for BOTH linux/amd64 (CPU index) and linux/arm64 (PyPI). numpy is
held below 1.24, which removed the `np.float` / `np.int` aliases this code uses.
TensorFlow is intentionally absent; TF test modules skip themselves via
`is_tf_available()`.

Team Dockerfile rules: the base stops at `git clone` + CMD; all git stripping
lives in the PR Dockerfile as its own RUN layers; prepare.sh is dependencies only.
"""

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.huggingface.transformers_13720_to_5252 import (
    _CHECK_GIT_CHANGES,
    _HARDENING_RUN,
    _SUBMODULE_HARDENING_RUN,
    _parse_with_collection,
    _strip_binary_diffs,
)
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL = "1383_to_711"

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
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider \\
    --timeout=1200 -o log_cli=false --continue-on-collection-errors $TEST_TARGETS
"""

_PREPARE = """#!/bin/bash
set -e

cd /home/[[REPO]]

# One Dockerfile RUN step per phase, so a failed build resumes from the last
# completed phase's cached layer. No argument runs every phase.
PHASE="${1:-all}"
run_phase() { [ "$PHASE" = all ] || [ "$PHASE" = "$1" ]; }

# pip's own PIP_RETRIES does not cover a read that times out mid-download
# (ReadTimeoutError from files.pythonhosted.org), so every pip call is retried here.
pip() {
    local n
    for n in 1 2 3 4 5; do
        command pip "$@" && return 0
        echo "pip $* failed (attempt $n/5), retrying in 15s" >&2
        sleep 15
    done
    return 1
}

ARCH=$(dpkg --print-architecture)
echo "target arch: $ARCH"

if run_phase torch; then
bash /home/check_git_changes.sh

# CPU-only torch. On amd64 the default PyPI wheel is the CUDA build, so it comes
# from the CPU index; on arm64 the PyPI wheel is already CPU-only.
if [ "$ARCH" = "amd64" ]; then
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu 'torch==1.10.2'
else
    pip install --no-cache-dir 'torch==1.10.2'
fi
python -c "import torch; print('torch', torch.__version__)"
fi

if run_phase repo; then
# The union of install_requires across every base in this interval, held to
# versions that still support these 2019 sources.
pip install --no-cache-dir 'numpy<1.24' boto3 requests tqdm regex \\
    'sentencepiece<0.2' sacremoses

# torch and every runtime dependency are already satisfied, so this only
# registers the repo; it must not fail silently.
pip install --no-cache-dir -e .
fi

if run_phase extras; then
# examples/test_examples.py imports run_glue / run_squad, which pull these in.
pip install --no-cache-dir 'scipy<1.8' 'scikit-learn<1.1' tensorboardX 'protobuf<4' || true

pip install --no-cache-dir 'pytest<8.0' 'pytest-timeout<2.3'
python -m pytest --version
fi
"""


class TransformersLegacyImageBase(Image):
    """Shared base: everything up to the git clone, then CMD.

    The `# syntax=` directive stops DockerfileEnhancer from appending its own
    checkout + hardening block to this shared base (see transformers_13720_to_5252).
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

    def dependency(self) -> str | Image:
        return "python:3.8-slim"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

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

ENV CI=true
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PIP_DEFAULT_TIMEOUT=120
ENV PIP_RETRIES=10

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
    bash \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Retried as a whole: a slow GitHub link dropped a full-history clone after 20 min
# (curl 56 GnuTLS recv error / early EOF) and failed the base. lowSpeed aborts a
# stalled transfer so the next attempt can start instead of hanging.
RUN for i in 1 2 3 4 5; do \\
        git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=300 \\
            clone "${{REPO_URL}}" /home/{self.pr.repo} && break; \\
        echo "git clone attempt $i failed, retrying"; rm -rf /home/{self.pr.repo}; sleep 30; \\
    done; \\
    test -d /home/{self.pr.repo}/.git

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class TransformersLegacyImageDefault(Image):
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
        return TransformersLegacyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        header = f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
"""
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _PREPARE.replace("[[REPO]]", repo)),
            File(".", "run.sh", header + _TEST_BODY),
            File(
                ".",
                "test-run.sh",
                header
                + f"""if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
"""
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                header
                + f"""if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi
"""
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("TransformersLegacyImageDefault dependency must be an Image")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
ARG TARGETARCH
ARG BUILDARCH
ENV TARGETARCH=${{TARGETARCH}}
ENV BUILDARCH=${{BUILDARCH}}

{self.global_env}

{_HARDENING_RUN}
{_SUBMODULE_HARDENING_RUN}
{copy_commands}
RUN bash /home/prepare.sh torch
RUN bash /home/prepare.sh repo
RUN bash /home/prepare.sh extras

{self.clear_env}

"""


@Instance.register("huggingface", f"transformers_{_INTERVAL}")
class TransformersLegacy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TransformersLegacyImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_with_collection(test_log)
