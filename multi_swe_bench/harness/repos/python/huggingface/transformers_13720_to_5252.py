import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.huggingface.transformers_44040_to_3323 import (
    parse_pytest_verbose_log,
)
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL = "13720_to_5252"


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
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider -p no:rich \\
    -o log_cli=false --continue-on-collection-errors[[DESELECT]] $TEST_TARGETS
"""

_DESELECT: dict[int, list[str]] = {
    5302: ["tests/test_tokenization_fast.py::TransfoXLFastTokenizerTest::test_all_tokenizers"],
}


def _test_body(number: int) -> str:
    args = "".join(f" \\\n    --deselect {t}" for t in _DESELECT.get(number, []))
    return _TEST_BODY.replace("[[DESELECT]]", args)

_SAVE_STATE_PATCH = """if grep -rq 'SAVE_STATE_WARNING' src/transformers/ 2>/dev/null; then
    find src/transformers/ -name '*.py' -exec sed -i \\
        's/from torch.optim.lr_scheduler import SAVE_STATE_WARNING/SAVE_STATE_WARNING = ""/' {} + || true
fi"""

_PREPARE = """#!/bin/bash
set -e

cd /home/[[REPO]]

PHASE="${1:-all}"
run_phase() { [ "$PHASE" = all ] || [ "$PHASE" = "$1" ]; }

pip() {
    local n
    for n in 1 2 3 4 5; do
        command pip "$@" && return 0
        echo "pip $* failed (attempt $n/5), retrying in 15s" >&2
        sleep 15
    done
    return 1
}

if run_phase torch; then
bash /home/check_git_changes.sh

PIP_CPU="--index-url https://download.pytorch.org/whl/cpu"
pip install --no-cache-dir $PIP_CPU 'torch==2.3.1' 'torchvision==0.18.1' \\
    || pip install --no-cache-dir $PIP_CPU 'torch' 'torchvision' \\
    || true
python -c "import torch; print('torch', torch.__version__)" || true
fi

if run_phase tokenizers; then
TOK=$(python - <<'PYTOK'
import re, pathlib
m = re.search(r"tokenizers\\s*==\\s*([0-9A-Za-z.-]+)", pathlib.Path("setup.py").read_text())
print(m.group(1) if m else '')
PYTOK
)
if [ -n "$TOK" ] && ! pip install --no-cache-dir --only-binary :all: "tokenizers==$TOK"; then
    echo "no wheel for tokenizers==$TOK on $(uname -m); building from sdist"
    rm -rf /tmp/tk && mkdir -p /tmp/tk
    pip download --no-binary :all: --no-deps "tokenizers==$TOK" -d /tmp/tk
    tar xf /tmp/tk/tokenizers-*.tar.gz -C /tmp/tk
    cd /tmp/tk/tokenizers-*/
    for TAG in "python-v$TOK" "python-v${TOK%%[.-]rc*}" "python-v${TOK%%rc*}" "python-v0.9.1" "rust-v0.9.0" "rust-v0.8.0"; do
        if curl -fsSL -o Cargo.lock \
            "https://raw.githubusercontent.com/huggingface/tokenizers/$TAG/bindings/python/Cargo.lock"; then
            echo "using Cargo.lock from $TAG"; break
        fi
    done
    test -s Cargo.lock
    if [ -f rust-toolchain ] && grep -qx 'stable' rust-toolchain; then
        echo '1.52.1' > rust-toolchain
        echo "pinned rust-toolchain 1.52.1 (was 'stable')"
    fi
    if [ -f rust-toolchain ] && grep -qx 'nightly' rust-toolchain; then
        echo 'nightly-2020-05-14' > rust-toolchain
        echo "pinned rust-toolchain nightly-2020-05-14 (was 'nightly')"
    fi
    if grep -A1 '^name = "pyo3"$' Cargo.lock | grep -qx 'version = "0.12.0"'; then
        for c in pyo3 pyo3cls pyo3-derive-backend; do
            sed -i -e "/^name = \\"$c\\"\\$/{n;s/^version = \\"0\\.12\\.0\\"\\$/version = \\"0.12.1\\"/}" \\
                   -e "s/\\"$c 0\\.12\\.0 (registry+/\\"$c 0.12.1 (registry+/" \\
                   -e "/^\\"checksum $c 0\\.12\\.0 /d" Cargo.lock
        done
        echo "bumped pyo3 0.12.0 -> 0.12.1 (c_char is u8 on aarch64)"
    fi
    for i in 1 2 3 4; do
        pip install --no-cache-dir . && break
        if [ "$i" = 4 ]; then echo "tokenizers build failed after $i attempts" >&2; exit 1; fi
        echo "tokenizers build attempt $i failed, retrying"; sleep 20
    done
    cd /home/[[REPO]]
fi
fi

if run_phase repo; then
pip install --no-cache-dir -e .
python -c "import tokenizers; print('tokenizers', tokenizers.__version__)" || true
fi

if run_phase extras; then
pip install --no-cache-dir setuptools-rust || true
pip install --no-cache-dir 'pytest<8.0' pytest-xdist timeout-decorator psutil parameterized || true
pip install --no-cache-dir 'pytest-asyncio<0.22' || true

pip install --no-cache-dir boto3 sentencepiece importlib_metadata sacremoses || true

pip install --no-cache-dir pandas gitpython onnx || true
pip install --no-cache-dir pytorch-lightning rouge-score sacrebleu nltk || true
pip install --no-cache-dir fire timm onnxruntime pydantic || true
pip install --no-cache-dir pyarrow multiprocess dill xxhash fsspec aiohttp || true
pip install --no-deps --no-cache-dir datasets evaluate || true
pip install --no-cache-dir scikit-learn || true
fi

if run_phase compat; then
cp /home/hub_compat.py "$(python -c 'import site; print(site.getsitepackages()[0])')/sitecustomize.py" || true

find src/ tests/ -name '*.py' -exec sed -i \\
    -e 's/from collections import Sequence/from collections.abc import Sequence/g' \\
    -e 's/from collections import Mapping/from collections.abc import Mapping/g' \\
    -e 's/from collections import MutableMapping/from collections.abc import MutableMapping/g' \\
    {} + || true

if [ -f src/transformers/dependency_versions_check.py ]; then
    python -c "import pathlib; p=pathlib.Path('src/transformers/dependency_versions_check.py'); t=p.read_text(); p.write_text(t.replace('require_version_core(deps[pkg])', 'pass  # require_version_core(deps[pkg])'))" || true
fi

[[SAVE_STATE_PATCH]]

python -c "import transformers; print('transformers', transformers.__version__)" || true
fi
"""

_HARDENING_RUN = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
"""


_SUBMODULE_HARDENING_RUN = """RUN if [ -f .gitmodules ]; then \\
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


_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

_HUB_COMPAT = """import os

try:
    import huggingface_hub
except Exception:
    huggingface_hub = None

if huggingface_hub is not None:
    if not hasattr(huggingface_hub, "HfFolder"):
        class _HfFolder:
            @staticmethod
            def get_token():
                return os.environ.get("HF_TOKEN", None)

            @staticmethod
            def save_token(token):
                pass

        huggingface_hub.HfFolder = _HfFolder

    if not hasattr(huggingface_hub, "Repository"):
        huggingface_hub.Repository = type(
            "Repository", (), {"__init__": lambda self, *a, **kw: None}
        )

    if not hasattr(huggingface_hub, "set_access_token"):
        huggingface_hub.set_access_token = lambda *a, **kw: None

    if not hasattr(huggingface_hub, "delete_repo"):
        huggingface_hub.delete_repo = lambda *a, **kw: None

    if not hasattr(huggingface_hub, "HfFileSystem"):
        huggingface_hub.HfFileSystem = type(
            "HfFileSystem", (), {"__init__": lambda self, *a, **kw: None}
        )

    if not hasattr(huggingface_hub, "HfApi"):
        huggingface_hub.HfApi = type(
            "HfApi", (), {"__init__": lambda self, *a, **kw: None}
        )

    if hasattr(huggingface_hub, "constants"):
        if not hasattr(huggingface_hub.constants, "HF_HUB_CACHE"):
            huggingface_hub.constants.HF_HUB_CACHE = os.path.expanduser(
                "~/.cache/huggingface/hub"
            )

    try:
        from huggingface_hub import utils as _hub_utils

        if not hasattr(_hub_utils, "OfflineModeIsEnabled"):
            class _OfflineModeIsEnabled(ConnectionError):
                pass

            _hub_utils.OfflineModeIsEnabled = _OfflineModeIsEnabled
    except Exception:
        pass
"""


class TransformersImageBase(Image):

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
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
ENV HF_HUB_DISABLE_TELEMETRY=1
ENV TOKENIZERS_PARALLELISM=false

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

RUN printf 'Acquire::Retries "10";\\nAcquire::http::Timeout "60";\\nAcquire::https::Timeout "60";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    libffi-dev \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

ARG RUST_VERSION=1.72.0
ENV RUSTUP_MAX_RETRIES=10
RUN for i in 1 2 3 4 5; do \\
        curl --proto '=https' --tlsv1.2 -sSf --retry 5 --retry-all-errors https://sh.rustup.rs \\
            | sh -s -- -y --profile minimal --default-toolchain ${{RUST_VERSION}} && break; \\
        echo "rustup attempt $i failed, retrying"; sleep 15; \\
    done; \\
    /root/.cargo/bin/rustc --version
ENV PATH=/root/.cargo/bin:${{PATH}}

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

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


class TransformersImageDefault(Image):
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
        return TransformersImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_script(self) -> str:
        return (
            _PREPARE.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[NUMBER]]", str(self.pr.number))
            .replace("[[SAVE_STATE_PATCH]]", _SAVE_STATE_PATCH)
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        header = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
""".replace("[[REPO]]", repo)
        body = _test_body(self.pr.number)

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_script()),
            File(".", "hub_compat.py", _HUB_COMPAT),
            File(".", "run.sh", header + body),
            File(
                ".",
                "test-run.sh",
                header
                + """if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo)
                + body,
            ),
            File(
                ".",
                "fix-run.sh",
                header
                + """if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo)
                + body,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("TransformersImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

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
RUN bash /home/prepare.sh tokenizers
RUN bash /home/prepare.sh repo
RUN bash /home/prepare.sh extras
RUN bash /home/prepare.sh compat

{self.clear_env}

"""


_COLLECT_ERROR_RE = re.compile(r"^_+ ERROR collecting (\S+?) _+$", re.M)
_MODULE_RAN_RE = re.compile(r"^(\S+?\.py)::", re.M)


def _parse_with_collection(log: str) -> TestResult:
    base = parse_pytest_verbose_log(log)

    failed_modules = set(_COLLECT_ERROR_RE.findall(log))
    ran_modules = set(_MODULE_RAN_RE.findall(log)) - failed_modules

    passed = set(base.passed_tests)
    failed = set(base.failed_tests)
    skipped = set(base.skipped_tests)

    for mod in failed_modules:
        failed.add(f"{mod}::<collection>")
    for mod in ran_modules:
        passed.add(f"{mod}::<collection>")

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("huggingface", f"transformers_{_INTERVAL}")
class TransformersEra(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TransformersImageDefault(self.pr, self._config)

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
        return _parse_with_collection(test_log)
