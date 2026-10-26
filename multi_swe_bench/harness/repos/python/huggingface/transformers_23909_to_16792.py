import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL = "23909_to_16792"
_BASE_IMAGE = "python:3.8-slim"


def _strip_binary_diffs(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        section
        for section in sections
        if section
        and "Binary files " not in section
        and "GIT binary patch" not in section
    )


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

_TEST_BODY = r"""set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*\.py|[^/]*_test\.py)$' \
    | grep -v '^templates/' \
    | grep -v '{{' \
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

PYTEST_WORKERS=$(nproc 2>/dev/null || echo 1)
if [ "$PYTEST_WORKERS" -gt 8 ]; then
    PYTEST_WORKERS=8
fi
MEMORY_WORKERS=$(awk '/^MemTotal:/ {printf "%d", $2/2097152}' /proc/meminfo 2>/dev/null || echo 1)
if [ -z "$MEMORY_WORKERS" ] || [ "$MEMORY_WORKERS" -lt 1 ]; then
    MEMORY_WORKERS=1
fi
if [ "$PYTEST_WORKERS" -gt "$MEMORY_WORKERS" ]; then
    PYTEST_WORKERS="$MEMORY_WORKERS"
fi

echo "running pytest with $PYTEST_WORKERS worker(s) on:$TEST_TARGETS"
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider \
    -o log_cli=false --continue-on-collection-errors --timeout=900 \
    -n "$PYTEST_WORKERS" --dist loadfile $TEST_TARGETS
"""

_RUN_ENV = """export CI=true
export TRANSFORMERS_IS_CI=1
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_TELEMETRY=1
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

[[RUN_ENV]]
cd /home/[[REPO]]

"""

_TEST_RUN_HEADER = """#!/bin/bash
set -eo pipefail

[[RUN_ENV]]
cd /home/[[REPO]]

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "PATCH FAILURE: test.patch did not apply" >&2
    exit 1
fi

"""

_FIX_RUN_HEADER = """#!/bin/bash
set -eo pipefail

[[RUN_ENV]]
cd /home/[[REPO]]

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "PATCH FAILURE: test.patch and fix.patch did not apply" >&2
    exit 1
fi

"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

python - <<'PYEOF'
import pathlib
import re

init_text = pathlib.Path("src/transformers/__init__.py").read_text(
    encoding="utf-8", errors="replace"
)
version = re.search(r'__version__\s*=\s*"([^"]+)"', init_text).group(1)
minor = int(re.match(r"\d+\.(\d+)", version).group(1))

table_path = pathlib.Path("src/transformers/dependency_versions_table.py")
deps = {}
if table_path.is_file():
    deps = {
        name.lower(): spec
        for name, spec in re.findall(
            r'"([^"]+)":\s*"([^"]+)"',
            table_path.read_text(encoding="utf-8", errors="replace"),
        )
    }

bands = (
    (21, "0.9.1", "0.12.0", "0.3.1", "2.4.0", "1.23.5", "1.13.1", "0.14.1"),
    (25, "0.11.1", "0.15.0", "0.3.1", "2.7.1", "1.23.5", "1.13.1", "0.14.1"),
    (28, "0.14.1", "0.19.0", "0.3.1", "2.11.0", "1.24.4", "2.0.1", "0.15.2"),
    (32, "0.16.4", "0.21.0", "0.3.3", "2.13.1", "1.24.4", "2.0.1", "0.15.2"),
    (36, "0.20.3", "0.26.1", "0.4.1", "2.16.1", "1.24.4", "2.1.2", "0.16.2"),
    (999, "0.22.2", "0.29.3", "0.4.3", "2.18.0", "1.24.4", "2.2.2", "0.17.2"),
)
for ceiling, hub, accelerate, safetensors, datasets, numpy, torch, vision in bands:
    if minor <= ceiling:
        break


def version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", value)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def satisfies(candidate, spec):
    expression = re.sub(r"^[A-Za-z0-9._\-]+", "", spec).strip()
    for clause in expression.split(","):
        clause = clause.strip()
        matched = re.match(r"^(==|!=|>=|<=|>|<|~=)\s*([0-9A-Za-z.*\-]+)$", clause)
        if not matched:
            continue
        operator = matched.group(1)
        wanted = version_tuple(matched.group(2).rstrip("*").rstrip("."))
        found = version_tuple(candidate)
        if operator == "==" and found != wanted:
            return False
        if operator == "!=" and found == wanted:
            return False
        if operator == ">=" and not found >= wanted:
            return False
        if operator == "<=" and not found <= wanted:
            return False
        if operator == ">" and not found > wanted:
            return False
        if operator == "<" and not found < wanted:
            return False
        if operator == "~=" and not found >= wanted:
            return False
    return True


runtimes = (
    ("2.2.2", "0.17.2"),
    ("2.1.2", "0.16.2"),
    ("2.0.1", "0.15.2"),
    ("1.13.1", "0.14.1"),
    ("1.12.1", "0.13.1"),
    ("1.11.0", "0.12.0"),
)
torch_spec = deps.get("torch", "torch")
for candidate, candidate_vision in runtimes:
    if version_tuple(candidate) > version_tuple(torch):
        continue
    if satisfies(candidate, torch_spec):
        torch, vision = candidate, candidate_vision
        break

pillow = "10.4.0" if re.search(r">=\s*10", deps.get("pillow", "")) else "9.5.0"

protobuf = deps.get("protobuf", "protobuf")
if "<" not in protobuf:
    protobuf = "protobuf<=3.20.3"

# huggingface_hub names imported by this tree (and its test patch) that the
# band's hub release does not export: the old `login` is absent from
# 0.8.x-0.10.x, and `set_access_token` was removed in 0.14.0.
hub_sources = [
    source.read_text(encoding="utf-8", errors="replace")
    for root in ("src/transformers", "tests")
    for source in pathlib.Path(root).rglob("*.py")
]
test_patch = pathlib.Path("/home/test.patch")
if test_patch.is_file():
    hub_sources.append(
        "\n".join(
            line[1:]
            for line in test_patch.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    )
hub_names = set()
for imported in re.findall(
    r"^\s*from\s+huggingface_hub\s+import\s+(\([^)]*\)|[^\n]+)",
    "\n".join(hub_sources),
    re.MULTILINE,
):
    for name in re.split(r"[,\n]", imported.strip("()")):
        name = name.split("#")[0].strip()
        if name:
            hub_names.add(re.split(r"\s+as\s+", name)[0].strip())
if "login" in hub_names and (0, 8, 0) <= version_tuple(hub) < (0, 11, 0):
    hub = "0.7.0"
if "set_access_token" in hub_names and version_tuple(hub) >= (0, 14, 0):
    hub = "0.13.4"

required = [
    deps.get("tokenizers", "tokenizers"),
    protobuf,
    "huggingface-hub==" + hub,
    "safetensors==" + safetensors,
    "accelerate==" + accelerate,
    "datasets==" + datasets,
    "numpy==" + numpy,
    "Pillow==" + pillow,
    "sentencepiece==0.1.99",
    "sacremoses==0.0.53",
    "pyyaml==6.0.1",
    "regex==2023.12.25",
    "requests==2.31.0",
    "tqdm==4.67.1",
    "filelock==3.12.4",
    "packaging==23.2",
    "psutil==5.9.8",
    "pytest==7.4.4",
    "pytest-timeout==2.1.0",
    "pytest-xdist==3.5.0",
    "parameterized==0.9.0",
    "timeout-decorator==0.5.0",
]

optional = [
    "evaluate==0.4.0",
    "scikit-learn==1.3.2",
    "soundfile==0.12.1",
    "librosa==0.10.1",
    "ftfy==6.1.3",
    "jiwer==3.0.3",
    "timm==0.9.16",
    "onnx==1.14.1",
    "onnxruntime==1.16.3",
]

pathlib.Path("/home/era_pins.txt").write_text(
    "\n".join(required) + "\n", encoding="utf-8"
)
pathlib.Path("/home/era_optional.txt").write_text(
    "\n".join(optional) + "\n", encoding="utf-8"
)
pathlib.Path("/home/era_constraints.txt").write_text(
    "\n".join(required + ["torch==" + torch, "torchvision==" + vision]) + "\n",
    encoding="utf-8",
)
pathlib.Path("/home/era_runtime.txt").write_text(
    torch + " " + vision + "\n", encoding="utf-8"
)
print("ERA_VERSION=" + version)
PYEOF

read -r TORCH_VERSION TORCHVISION_VERSION < /home/era_runtime.txt
echo "ERA_TORCH=${TORCH_VERSION} ERA_TORCHVISION=${TORCHVISION_VERSION}"
cat /home/era_pins.txt

pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"

export PIP_CONSTRAINT=/home/era_constraints.txt

pip install --no-cache-dir -r /home/era_pins.txt

while read -r package; do
    [ -n "${package}" ] || continue
    pip install --no-cache-dir "${package}" || true
done < /home/era_optional.txt

pip install --no-deps --no-cache-dir -e .

python -c "import transformers, torch, torchvision, tokenizers, huggingface_hub, numpy, PIL, datasets, pytest, parameterized; print('DEPS_OK', transformers.__version__, torch.__version__, tokenizers.__version__, huggingface_hub.__version__)"
python -c "import xdist, pytest_timeout, timeout_decorator, psutil; print('PLUGINS_OK')"
python -m pytest --version
"""


class Transformers23909To16792ImageBase(Image):
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
        return _BASE_IMAGE

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

FROM {_BASE_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore \\
    PIP_DEFAULT_TIMEOUT=120 \\
    PIP_RETRIES=10 \\
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \\
    HF_HOME=/home/hf_cache \\
    HF_HUB_DISABLE_TELEMETRY=1 \\
    TOKENIZERS_PARALLELISM=false \\
    TRANSFORMERS_IS_CI=1 \\
    CI=true

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
        bash build-essential ca-certificates cmake curl ffmpeg git \\
        libffi-dev libsndfile1 libssl-dev make pkg-config \\
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class Transformers23909To16792ImageDefault(Image):
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
        return Transformers23909To16792ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                _PREPARE.replace("[[REPO]]", repo).replace(
                    "[[SHA]]", self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_HEADER.replace("[[REPO]]", repo).replace("[[RUN_ENV]]", _RUN_ENV)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_HEADER.replace("[[REPO]]", repo).replace(
                    "[[RUN_ENV]]", _RUN_ENV
                )
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_HEADER.replace("[[REPO]]", repo).replace(
                    "[[RUN_ENV]]", _RUN_ENV
                )
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUS = r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
_INLINE_RESULT = re.compile(
    rf"^(?P<id>\S+\.py::\S+?)\s+{_STATUS}\b",
    re.MULTILINE,
)
_PREFIXED_RESULT = re.compile(
    rf"^(?:\[gw\d+\]\s*)?(?:\[\s*\d+%\]\s*)?{_STATUS}\s+"
    rf"(?P<id>\S+\.py::\S+?)(?:\s+-\s.*)?\s*$",
    re.MULTILINE,
)
_FILE_ERROR = re.compile(r"^ERROR\s+(?P<id>\S+\.py)\s*(?:-.*)?$", re.MULTILINE)

_PASSED_OUTCOMES = {"PASSED", "XPASS"}
_FAILED_OUTCOMES = {"FAILED", "ERROR"}


def _parse_pytest_log(log: str) -> TestResult:
    clean = _ANSI_ESCAPE.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    matches = list(_INLINE_RESULT.finditer(clean)) + list(
        _PREFIXED_RESULT.finditer(clean)
    )
    for match in matches:
        name = match.group("id")
        status = match.group("status")
        if status in _PASSED_OUTCOMES:
            passed_tests.add(name)
        elif status in _FAILED_OUTCOMES:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    for match in _FILE_ERROR.finditer(clean):
        failed_tests.add(match.group("id"))

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


@Instance.register("huggingface", "transformers_23909_to_16792")
class TRANSFORMERS_23909_TO_16792(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Transformers23909To16792ImageDefault(self.pr, self._config)

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
        return _parse_pytest_log(test_log)
