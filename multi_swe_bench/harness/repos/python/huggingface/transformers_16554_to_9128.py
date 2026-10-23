import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL = "16554_to_9128"

_TEST_BODY = """set +e
TEST_FILES=$(grep -E '^\\+\\+\\+ b/' /home/test.patch \\
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \\
    | grep -E '(^|/)(test_[^/]*\\.py|[^/]*_test\\.py)$' \\
    | grep -v '^templates/adding_a_new_model/' \\
    | grep -v '{{' \\
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
    -o log_cli=false --continue-on-collection-errors $TEST_TARGETS
"""

_PREPARE = """#!/bin/bash
set -e

export PIP_DEFAULT_TIMEOUT=120
export PIP_RETRIES=10

cd /home/[[REPO]]

git reset --hard
bash /home/check_git_changes.sh

git checkout [[SHA]]
bash /home/check_git_changes.sh

sed -i -E 's/"tokenizers==0\\.10\\.1rc[0-9]+"/"tokenizers==0.10.2"/' setup.py || true
sed -i -E 's/"tokenizers==0\\.9\\.[0-9]+"/"tokenizers==0.9.4"/' setup.py || true

TOK=$(python - <<'PYTOK'
import re
import pathlib

try:
    text = pathlib.Path("setup.py").read_text(encoding="utf-8", errors="replace")
except Exception:
    print("")
    raise SystemExit

match = re.search(r'"tokenizers([^"]*)"', text)
spec = match.group(1) if match else ""


def as_tuple(value):
    parts = []
    for chunk in re.split(r"[.-]", value.strip()):
        digits = re.match(r"^\\d+", chunk)
        parts.append(int(digits.group(0)) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def satisfies(candidate, expression):
    if not expression.strip():
        return True
    for clause in expression.split(","):
        clause = clause.strip()
        if not clause:
            continue
        bits = re.match(r"^(==|!=|>=|<=|>|<|~=)\\s*([0-9A-Za-z.\\-*]+)$", clause)
        if not bits:
            return False
        operator = bits.group(1)
        wanted = bits.group(2).rstrip("*").rstrip(".")
        left = as_tuple(candidate)
        right = as_tuple(wanted)
        if operator == "==" and left != right:
            return False
        if operator == "!=" and left == right:
            return False
        if operator == ">=" and not left >= right:
            return False
        if operator == "<=" and not left <= right:
            return False
        if operator == ">" and not left > right:
            return False
        if operator == "<" and not left < right:
            return False
        if operator == "~=" and not left >= right:
            return False
    return True


for candidate in ["0.10.2", "0.11.6", "0.12.1", "0.9.4"]:
    if satisfies(candidate, spec):
        print(candidate)
        raise SystemExit

print("")
PYTOK
)
TOK=$(printf '%s' "$TOK" | tail -1 | tr -dc '0-9.')

if [ -n "$TOK" ]; then
    echo "TOK_PIN=$TOK"
    printf '%s\\n' "tokenizers==$TOK" > /tmp/tok-constraint.txt
    export PIP_CONSTRAINT=/tmp/tok-constraint.txt
    pip install "tokenizers==$TOK" || true
else
    echo "TOK_PIN=unresolved"
fi

pip install --index-url https://download.pytorch.org/whl/cpu \\
    "torch==1.13.1" "torchvision==0.14.1" || true

pip install -e . || true

pip install 'pytest<8.0' pytest-timeout pytest-xdist \\
    timeout-decorator psutil parameterized || true
pip install 'pytest-asyncio<0.22' || true
pip install 'numpy<1.24' 'protobuf<3.21' 'Pillow<10' \\
    sacremoses importlib_metadata || true
pip install 'pandas<2' gitpython scikit-learn fire \\
    rouge-score nltk 'sacrebleu<2' || true
pip install 'pyarrow<9' 'fsspec<2023' aiohttp xxhash \\
    'dill<0.3.6' 'multiprocess<0.70.14' || true
pip install sentencepiece onnx onnxruntime \\
    soundfile 'librosa==0.9.2' jiwer ftfy || true
pip install --no-deps 'datasets==1.18.4' 'evaluate==0.2.2' || true

python -c "import datasets" >/dev/null 2>&1 || {
    for DSVER in 1.11.0 1.5.0 1.1.3; do
        pip install --no-deps "datasets==$DSVER" || true
        if python -c "import datasets" >/dev/null 2>&1; then
            break
        fi
    done
}

python - <<'PYDEPS'
import pathlib

path = pathlib.Path("src/transformers/dependency_versions_check.py")
if path.is_file():
    text = path.read_text(encoding="utf-8", errors="replace")
    replaced = text.replace("require_version_core(deps[pkg])", "pass")
    if replaced != text:
        path.write_text(replaced, encoding="utf-8")
        print("DEPCHECK_NEUTERED=1")
    else:
        print("DEPCHECK_NEUTERED=0")
else:
    print("DEPCHECK_ABSENT=1")
PYDEPS

python -c "import torch; print('torch', torch.__version__)" || true
python -c "import tokenizers; print('tokenizers', tokenizers.__version__)" || true
python -c "import huggingface_hub as h; print('hub', h.__version__)" || true
python -c "import datasets; print('datasets', datasets.__version__)" || true
python -c "import pytest; print('pytest', pytest.__version__)" || true
python -c "import transformers; print('transformers', transformers.__version__)" || true

echo "DEPS_OK"
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

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO]]

"""

_TEST_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch

"""

_FIX_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

"""


def _strip_binary_diffs(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s
        for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )


class ImageBase(Image):
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
        org = self.pr.org
        repo = self.pr.repo

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

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore \\
    PIP_CACHE_DIR=/opt/pipcache \\
    PIP_DEFAULT_TIMEOUT=120 \\
    PIP_RETRIES=10 \\
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \\
    HF_HUB_DISABLE_TELEMETRY=1 \\
    TOKENIZERS_PARALLELISM=false \\
    CI=true

LABEL org.opencontainers.image.title="{org}/{repo}"
LABEL org.opencontainers.image.description="{org}/{repo} Docker image"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \\
    && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

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

RUN python -m pip install --upgrade pip setuptools wheel

RUN mkdir -p /opt/pipcache /opt/wheels \\
    && pip download -d /opt/wheels --index-url https://download.pytorch.org/whl/cpu \\
        "torch==1.13.1" "torchvision==0.14.1" || true \\
    && pip download -d /opt/wheels "tokenizers==0.9.4" || true \\
    && pip download -d /opt/wheels "tokenizers==0.10.2" || true \\
    && pip download -d /opt/wheels "tokenizers==0.11.6" || true \\
    && pip download -d /opt/wheels "tokenizers==0.12.1" || true \\
    && pip download -d /opt/wheels requests regex filelock tqdm pyyaml \\
        packaging huggingface-hub || true \\
    && pip download -d /opt/wheels 'pytest<8.0' pytest-timeout pytest-xdist \\
        timeout-decorator psutil parameterized || true \\
    && pip download -d /opt/wheels 'pytest-asyncio<0.22' || true \\
    && pip download -d /opt/wheels 'numpy<1.24' 'protobuf<3.21' 'Pillow<10' \\
        sacremoses importlib_metadata || true \\
    && pip download -d /opt/wheels 'pandas<2' gitpython scikit-learn fire \\
        rouge-score nltk 'sacrebleu<2' || true \\
    && pip download -d /opt/wheels 'pyarrow<9' 'fsspec<2023' aiohttp xxhash \\
        'dill<0.3.6' 'multiprocess<0.70.14' || true \\
    && pip download -d /opt/wheels sentencepiece onnx onnxruntime \\
        soundfile 'librosa==0.9.2' jiwer ftfy || true \\
    && pip download -d /opt/wheels --no-deps 'datasets==1.18.4' \\
        'datasets==1.11.0' 'datasets==1.5.0' 'datasets==1.1.3' \\
        'evaluate==0.2.2' || true \\
    && ls -1 /opt/wheels | wc -l

ENV PIP_FIND_LINKS=/opt/wheels

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

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

    def dependency(self) -> str | Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = (
            _PREPARE.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
        )

        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "check_git_changes.sh",
                _CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                prepare,
            ),
            File(
                ".",
                "run.sh",
                _RUN_HEADER.replace("[[REPO]]", self.pr.repo) + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_HEADER.replace("[[REPO]]", self.pr.repo) + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_HEADER.replace("[[REPO]]", self.pr.repo) + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


@Instance.register("huggingface", f"transformers_{_INTERVAL}")
class TRANSFORMERS_16554_TO_9128(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_with_collection(test_log)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VERBOSE_LINE = re.compile(
    r"^(\S.*?\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)"
)
_COLLECT_ERROR_RE = re.compile(r"^_+ ERROR collecting (\S+?) _+$", re.M)
_MODULE_RAN_RE = re.compile(r"^(\S+?\.py)::", re.M)

_PASSED_OUTCOMES = {"PASSED", "XPASS"}
_FAILED_OUTCOMES = {"FAILED", "ERROR"}
_SKIPPED_OUTCOMES = {"SKIPPED", "XFAIL"}


def _parse_pytest_verbose_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = _ANSI_ESCAPE.sub("", log)

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


def _parse_with_collection(log: str) -> TestResult:
    clean = _ANSI_ESCAPE.sub("", log)
    base = _parse_pytest_verbose_log(clean)

    failed_modules = set(_COLLECT_ERROR_RE.findall(clean))
    ran_modules = set(_MODULE_RAN_RE.findall(clean)) - failed_modules

    passed_tests = set(base.passed_tests)
    failed_tests = set(base.failed_tests)
    skipped_tests = set(base.skipped_tests)

    for module in failed_modules:
        failed_tests.add(f"{module}::<collection>")
    for module in ran_modules:
        passed_tests.add(f"{module}::<collection>")

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
