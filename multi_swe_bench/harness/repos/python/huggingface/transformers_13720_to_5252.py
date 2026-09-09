"""huggingface/transformers, PRs #5252-#13720 (June 2020 - September 2021).

Registered under a number_interval key, `transformers_13720_to_5252`, NOT under
the plain `huggingface/transformers` key. Two generic configs for this repo
already exist (`transformers.py` and `transformers_44040_to_3323.py`); neither is
edited here, because re-registering a plain repo key silently overwrites whatever
config earlier phases used and invalidates PRs already processed against it.
Routing is by setting `number_interval` on each dataset row.

WHY NOT JUST REUSE transformers_44040_to_3323.py (which spans this range):

  1. Team Dockerfile rule #1 - the base must carry everything up to the GIT CLONE
     and then CMD. There the base only does apt + pip; the clone lives in
     prepare.sh, i.e. in the *PR* image, so the ~1GB history is re-cloned once
     per PR.
  2. Team Dockerfile rule #2 - hardening belongs in the PR image. Already true
     there, and kept here.
  3. Its dependency install does `pip install --no-deps -e .` and then installs a
     long unpinned list. That is what broke the first build of this interval:
     see DEPENDENCY RESOLUTION below.

DEPENDENCY RESOLUTION - the reason this file exists in its current shape.

The first attempt at this interval built fine and produced an unusable instance.
PR #5252 ran 10 passed / 13 failed / 15 skipped in ALL THREE stages, so nothing
transitioned and every bucket came back empty (valid: False, f2p=n2p=s2p=p2p=0).
The 13 failures were all environment drift, not the PR, e.g.

    TypeError: CharBPETokenizer.__init__() got an unexpected keyword argument 'vocab_file'

`tokenizers` was installed unpinned, so 2020 source ran against a 2026 library.
The same root cause inflated every image to 12.4GB: an unpinned `torchvision`
resolved the CUDA build of torch and dragged in the whole nvidia-* stack
(nvidia-cudnn, nvidia-cusolver, cuda-bindings, ...) into a CPU-only test box,
*before* the CPU-pinned torch line further down ever ran.

So the ordering here is deliberate:

  * `pip install -e .` WITH dependencies, so setup.py's own era-correct pins win
    (transformers pins tokenizers per release). This is the single most important
    line in the file.
  * torch/torchvision ONLY from the CPU wheel index, and only after the above.
  * test tooling and compat shims last, and nothing that would re-resolve a
    library the repo already pinned.

Installs still end in `|| true` where a failure is genuinely tolerable, but the
lines that determine test semantics are no longer among them.
"""

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
    """Drop binary sections; `git apply` cannot replay them from a text patch."""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(s for s in sections if s and "Binary files " not in s)


# Run only the test files the patch touches. transformers' full suite is not
# runnable here, and deriving targets from the patch keeps a stage to minutes.
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
    -o log_cli=false --continue-on-collection-errors $TEST_TARGETS
"""

# torch removed SAVE_STATE_WARNING from torch.optim.lr_scheduler in 2.0; every PR
# in this interval imports it unconditionally.
_SAVE_STATE_PATCH = """if grep -rq 'SAVE_STATE_WARNING' src/transformers/ 2>/dev/null; then
    find src/transformers/ -name '*.py' -exec sed -i \\
        's/from torch.optim.lr_scheduler import SAVE_STATE_WARNING/SAVE_STATE_WARNING = ""/' {} + || true
fi"""

_PREPARE = """#!/bin/bash
set -e

# The PR Dockerfile has already done `git checkout --detach $BASE_COMMIT` and the
# full history scrub in its own RUN layers, so this script does dependencies only.
# It runs LAST, which also means pip sees exactly the graded tree.
cd /home/[[REPO]]
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# 1. CPU-only torch FIRST, from the CPU wheel index.
#    Doing this before anything that depends on torch stops pip from resolving
#    the default (CUDA) build later: torchvision unpinned was pulling
#    nvidia-cudnn / nvidia-cusolver / cuda-bindings, several GB of GPU runtime
#    into a box with no GPU, and that alone accounted for most of a 12.4GB image.
# ---------------------------------------------------------------------------
PIP_CPU="--index-url https://download.pytorch.org/whl/cpu"
pip install --no-cache-dir $PIP_CPU 'torch==2.3.1' 'torchvision==0.18.1' \\
    || pip install --no-cache-dir $PIP_CPU 'torch' 'torchvision' \\
    || true
python -c "import torch; print('torch', torch.__version__)" || true

# ---------------------------------------------------------------------------
# 2. The repo WITH its own dependencies. setup.py pins tokenizers (and friends)
#    per release, so this is what keeps 2020 source on a 2020-era tokenizers.
#    The previous config used --no-deps here and then installed `tokenizers`
#    unpinned, which is what produced
#      TypeError: CharBPETokenizer.__init__() got an unexpected keyword 'vocab_file'
#    in all three stages and left every reward bucket empty.
# ---------------------------------------------------------------------------
# --- tokenizers must be satisfied BEFORE the editable install resolves ---
# Most pins in this interval publish no aarch64 wheel (0.8.1rc2, 0.9.0rc2, 0.10.3
# have none; only 0.9.4 does), so on arm64 pip falls back to the sdist and has to
# compile the Rust extension. That sdist ships a `rust-toolchain` pinning
# nightly-2020-05-14 - which rustup obeys over any --default-toolchain - but ships
# NO Cargo.lock, so 2020 cargo resolves today's deps and dies on the first
# edition-2021 crate:
#     error: failed to parse manifest at itoa-1.0.18/Cargo.toml
#     this version of Cargo is older than the `2021` edition
# Supplying the upstream era Cargo.lock restores the 2020 toolchain + 2020 deps
# combination that actually worked. Wheel is still tried first, so amd64 keeps the
# fast path and stays identical to the tars already verified.
TOK=$(python - <<'PYTOK'
import re, pathlib
m = re.search(r"tokenizers==([0-9A-Za-z.-]+)", pathlib.Path("setup.py").read_text())
print(m.group(1) if m else '')
PYTOK
)
if [ -n "$TOK" ] && ! pip install --no-cache-dir "tokenizers==$TOK"; then
    echo "no wheel for tokenizers==$TOK on $(uname -m); building from sdist"
    rm -rf /tmp/tk && mkdir -p /tmp/tk
    pip download --no-binary :all: --no-deps "tokenizers==$TOK" -d /tmp/tk
    tar xf /tmp/tk/tokenizers-*.tar.gz -C /tmp/tk
    cd /tmp/tk/tokenizers-*/
    # Release candidates were never tagged upstream, so fall back to the nearest tag.
    for TAG in "python-v$TOK" "python-v${TOK%%rc*}" "python-v0.9.1" "rust-v0.9.0" "rust-v0.8.0"; do
        if curl -fsSL -o Cargo.lock \
            "https://raw.githubusercontent.com/huggingface/tokenizers/$TAG/bindings/python/Cargo.lock"; then
            echo "using Cargo.lock from $TAG"; break
        fi
    done
    test -s Cargo.lock
    # A `rust-toolchain` saying literally `stable` means "today's stable" to rustup,
    # i.e. >= 1.53, which cannot compile lexical-core 0.7.4: Rust 1.53 stabilised a
    # built-in `BITS` const of type u32 on the integer primitives, colliding with that
    # crate's own usize `Limb::BITS` ->
    #     error[E0277]: cannot divide `usize` by `u32`
    # Pin the newest stable that predates it. Dated nightlies (the 0.8.x sdists pin
    # nightly-2020-05-14) are already era-correct and are left untouched.
    if [ -f rust-toolchain ] && grep -qx 'stable' rust-toolchain; then
        echo '1.52.1' > rust-toolchain
        echo "pinned rust-toolchain 1.52.1 (was 'stable')"
    fi
    pip install --no-cache-dir .
    cd /home/[[REPO]]
fi

# NO `|| --no-deps` fallback here, deliberately. That fallback is what turned a
# failed dependency resolution into a hollow image that still reported success:
# the build "passed" with no requests/tokenizers installed and every instance
# collected 0 tests. If this cannot resolve, the build must fail loudly.
pip install --no-cache-dir -e .
python -c "import tokenizers; print('tokenizers', tokenizers.__version__)" || true

# ---------------------------------------------------------------------------
# 3. Test tooling only. Nothing here may re-resolve a library the repo pinned,
#    so no bare `tokenizers` / `huggingface-hub` / `transformers` in this list.
# ---------------------------------------------------------------------------
# setuptools-rust is the build backend the old `tokenizers` sdists declare; it is
# only exercised on arm64, where no wheel exists and the crate must be compiled.
pip install --no-cache-dir setuptools-rust || true
pip install --no-cache-dir 'pytest<8.0' pytest-xdist timeout-decorator psutil parameterized || true
pip install --no-cache-dir 'pytest-asyncio<0.22' || true

# Optional extras some suites import. Kept `|| true` and deliberately unpinned-
# but-additive: none of these are pinned by setup.py, so they cannot clobber it.
pip install --no-cache-dir boto3 sentencepiece importlib_metadata sacremoses || true

# Optional extras that setup.py does NOT declare but individual test modules
# import. Each was found by an actual collection error in a first full run:
#   pandas            - tests/test_modeling_deberta.py and 7 others (via
#                       transformers.models.auto -> data loading helpers)
#   gitpython ("git") - examples/seq2seq/utils.py
#   pytorch-lightning - examples/seq2seq lightning trainers
#   onnx              - onnx export tests
# None of these are pinned by setup.py, so unlike `tokenizers` they cannot
# clobber an era pin; that is why they are safe to install unpinned here.
pip install --no-cache-dir pandas gitpython onnx || true
pip install --no-cache-dir pytorch-lightning rouge-score sacrebleu nltk || true
# `fire` is imported by examples/seq2seq/rouge_cli.py, which #7410's FIX patch
# creates - without it the fix stage still fails to collect and the fail-to-pass
# transition stays invisible.
pip install --no-cache-dir fire timm onnxruntime pydantic || true
# `datasets` is installed --no-deps above (to stop it re-resolving pinned libs),
# so its own hard requirement pyarrow has to come separately; without it
# tests/test_modeling_deberta.py and friends fail to collect.
# `datasets` is installed --no-deps above (to stop it re-resolving pinned libs),
# so ITS OWN runtime requirements have to be supplied by hand. Chasing these one
# build at a time cost three rebuilds (pandas -> pyarrow -> multiprocess), so the
# whole set is listed here rather than discovered incrementally.
pip install --no-cache-dir pyarrow multiprocess dill xxhash fsspec aiohttp || true
pip install --no-deps --no-cache-dir datasets evaluate || true
pip install --no-cache-dir scikit-learn || true

# Modern huggingface_hub drops symbols this era imports at module scope;
# sitecustomize restores them before transformers is imported.
cp /home/hub_compat.py "$(python -c 'import site; print(site.getsitepackages()[0])')/sitecustomize.py" || true

# Python 3.10 moved the ABCs out of `collections`.
# One -e per expression. The previous single spliced script embedded its own
# line-continuations in the sed program and failed every build with
#   sed: -e expression #1, char 247: unterminated address regex
# It ended in `|| true`, so it was a silent no-op rather than a failure.
find src/ tests/ -name '*.py' -exec sed -i \\
    -e 's/from collections import Sequence/from collections.abc import Sequence/g' \\
    -e 's/from collections import Mapping/from collections.abc import Mapping/g' \\
    -e 's/from collections import MutableMapping/from collections.abc import MutableMapping/g' \\
    {} + || true

# This era hard-fails at import when an installed version differs from its pin.
# We keep the pins (that is the point of step 2) but disable the assertion, since
# test tooling above may still shift a transitive version.
if [ -f src/transformers/dependency_versions_check.py ]; then
    python -c "import pathlib; p=pathlib.Path('src/transformers/dependency_versions_check.py'); t=p.read_text(); p.write_text(t.replace('require_version_core(deps[pkg])', 'pass  # require_version_core(deps[pkg])'))" || true
fi

[[SAVE_STATE_PATCH]]

python -c "import transformers; print('transformers', transformers.__version__)" || true
"""

# Team Dockerfile rule #2: the git stripping / hardening lives in the PR-specific
# DOCKERFILE as its own RUN layer, not inside prepare.sh. Keeping it here makes
# the scrub auditable by reading the generated Dockerfile alone, which is how the
# bazel era config does it too. It must run AFTER prepare.sh, because prepare.sh
# still needs `origin` to fetch base commits that live only on refs/pull/<n>/head.
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


# Submodules get the same scrub. transformers has none today, but the layer is
# kept so the shipped image is provably clean if one is ever added.
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

# 2020-era transformers predates huggingface_hub entirely, so this must not be a
# hard import: an unguarded `import huggingface_hub` here made sitecustomize
# raise on every interpreter start.
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
    """Shared base for the interval.

    Per team rule #1 this stops at the GIT CLONE and then CMD. The clone keeps
    full history AND keeps `origin`; the PR layer needs the remote to fetch
    refs/pull/<n>/head, and removes it during hardening.

    The `# syntax=` directive is load-bearing: DockerfileEnhancer.enhance()
    (harness/image.py) rewrites any base whose dependency() is a plain string
    unless that exact directive is present. Its rewrite appends
    `git checkout $BASE_COMMIT` plus the full hardening block to the base, which
    strips `origin` and scrubs history to the single seed commit. For a shared
    base that is fatal: the first run of the previous config was seeded from the
    oldest PR, so only that PR built and the rest died with
    `fatal: 'origin' does not appear to be a git repository` (exit 128).
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
        # 3.8, NOT 3.10. This interval's transformers pins tokenizers 0.8.0-rc3
        # (2020) through 0.10.x (2021), and NONE of those publish a cp310 wheel -
        # only cp36/cp37/cp38(/cp39). On 3.10 pip therefore fell back to the
        # sdist and died with "error: can't find Rust compiler", which failed the
        # whole editable install and left the image with no requests/tokenizers
        # at all: every instance then collected 0 tests. cp38 wheels exist for
        # the entire range, so nothing has to be compiled. torch 2.3.1 and
        # torchvision 0.18.1 both still ship cp38.
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    libffi-dev \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain, needed ONLY on linux/arm64. Each PR's setup.py pins an exact
# `tokenizers`, and most of those versions never published an aarch64 wheel
# (0.8.1rc2, 0.9.0rc2, 0.10.3 have none; only 0.9.4 does). On arm64 pip therefore
# falls back to the sdist, which builds a native Rust extension - without cargo
# that fails with "Failed building wheel for tokenizers" and the whole build dies.
# Pinned via ARG so the version can be overridden without editing this file;
# a current rustc often cannot compile these 2020-era crates.
ARG RUST_VERSION=1.72.0
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \\
    | sh -s -- -y --profile minimal --default-toolchain ${{RUST_VERSION}}
ENV PATH=/root/.cargo/bin:${{PATH}}

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

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

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_script()),
            File(".", "hub_compat.py", _HUB_COMPAT),
            File(".", "run.sh", header + _TEST_BODY),
            File(
                ".",
                "test-run.sh",
                header
                + """if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo)
                + _TEST_BODY,
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
                + _TEST_BODY,
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

{copy_commands}

{_HARDENING_RUN}
{_SUBMODULE_HARDENING_RUN}
RUN bash /home/prepare.sh

{self.clear_env}

"""


_COLLECT_ERROR_RE = re.compile(r"^_+ ERROR collecting (\S+?) _+$", re.M)
_MODULE_RAN_RE = re.compile(r"^(\S+?\.py)::", re.M)


def _parse_with_collection(log: str) -> TestResult:
    """pytest's verbose parser, plus one synthetic result per test module.

    A module whose import fails produces NO `path::test PASSED/FAILED` lines at
    all - pytest reports it once as `ERROR collecting <path>` and moves on. The
    base parser therefore sees nothing, so a module that cannot be imported
    before the fix but imports fine after it scores zero buckets.

    #13720 is exactly that case: its test patch does
        from transformers import BlenderbotTokenizerFast
    which the FIX patch introduces, so the test stage logs

        E  ImportError: cannot import name 'BlenderbotTokenizerFast'
        ERROR collecting tests/test_tokenization_blenderbot.py

    and the fix stage collects it cleanly - a textbook fail-to-pass that was
    being silently dropped. Same failure mode as Bazel's `FAILED TO BUILD`,
    which needed the same treatment. #9691 and #7410 depend on this too.

    Each module therefore gets one extra entry, `<path>::<collection>`:
      * FAILED when pytest reported a collection error for it,
      * PASSED when the module produced at least one real test result.
    Both stages name it identically, which is what lets report.py see the
    transition.
    """
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
