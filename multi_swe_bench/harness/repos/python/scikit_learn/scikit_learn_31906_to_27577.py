import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_HI = 31906
_ERA_LO = 27577

_PYTHON_IMAGE = "python:3.12-slim-bookworm"

_APT_PACKAGES = "build-essential ca-certificates git pkg-config"

_BOOTSTRAP_PINS = '"pip==24.2" "setuptools==69.5.1" "wheel==0.44.0"'

_STACK_PINS = (
    '"meson==1.5.2" "meson-python==0.17.1" "ninja==1.11.1.1" "Cython==3.0.11" \\\n'
    '    "numpy==1.26.4" "scipy==1.13.1" "joblib==1.4.2" "threadpoolctl==3.5.0" \\\n'
    '    "pandas==2.2.3" "Pillow==10.4.0" "pytest==7.4.4" "array-api-compat==1.5.1"'
)

_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_TORCH_PIN = '"torch==2.2.2"'

_PYTEST_WARNING_FILTER = "error::sklearn.exceptions.ConvergenceWarning"

_BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
_END_MARKER = "===== END TEST DETAIL ====="

_MESON_PROBE = (
    "grep -Eq '^[[:space:]]*build-backend[[:space:]]*=[[:space:]]*\"mesonpy\"' "
    "pyproject.toml 2>/dev/null"
)

_TEST_BODY = r"""
print_test_detail() {
    echo "__BEGIN__"
    python - <<'PY'
import os
import xml.etree.ElementTree as ET

path = "/home/results.xml"
root = None
if os.path.exists(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        root = None

if root is not None:
    for tc in root.iter("testcase"):
        classname = tc.get("classname") or ""
        name = (tc.get("name") or "").replace("\r", " ").replace("\n", " ")
        file_attr = (tc.get("file") or "").replace("\\", "/")
        cls = ""
        if file_attr:
            file_path = file_attr
            module = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
            if module and classname.startswith(module + "."):
                cls = classname[len(module) + 1:]
        else:
            parts = classname.split(".")
            if len(parts) > 1 and parts[-1][:1].isupper():
                cls = parts[-1]
                parts = parts[:-1]
            file_path = "/".join(parts) + ".py"

        status = "PASSED"
        for child in tc:
            if child.tag in ("failure", "error"):
                status = "FAILED"
                break
            if child.tag == "skipped":
                status = "SKIPPED"

        node_id = file_path + "::" + (cls + "::" if cls else "") + name
        print("TESTCASE " + node_id + " " + status)
PY
    echo "__END__"
}

rm -f /home/results.xml

set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*\.py|[^/]*_test\.py)$' \
    | sort -u)
CHANGED_SOURCES=$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \
    | grep -E '\.(pyx|pxd|pxi|tp|c|cc|cpp|h|hpp)$')
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if ! __MESON_PROBE__; then
    if [ -n "$CHANGED_SOURCES" ]; then
        python setup.py build_ext --inplace -j 4
    fi
fi

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    print_test_detail
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    pytest $TEST_TARGETS -v \
        -p no:cacheprovider \
        --color=no \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --import-mode=importlib \
        -W __WARNING_FILTER__ \
        --junitxml=/home/results.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
    print_test_detail
fi
"""


def _test_body() -> str:
    return (
        _TEST_BODY.replace("__BEGIN__", _BEGIN_MARKER)
        .replace("__END__", _END_MARKER)
        .replace("__MESON_PROBE__", _MESON_PROBE)
        .replace("__WARNING_FILTER__", _PYTEST_WARNING_FILTER)
    )


def _stage_header(repo: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "export OMP_NUM_THREADS=1\n"
        "export OPENBLAS_NUM_THREADS=1\n"
        "export MKL_NUM_THREADS=1\n"
        "\n"
        f"cd /home/{repo}\n"
    )


def _check_git_changes_sh() -> str:
    return (
        "#!/bin/bash\n"
        "set -e\n"
        "\n"
        "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
        '  echo "check_git_changes: Not inside a git repository"\n'
        "  exit 1\n"
        "fi\n"
        "\n"
        "if [[ -n $(git status --porcelain) ]]; then\n"
        '  echo "check_git_changes: Uncommitted changes"\n'
        "  git status --porcelain\n"
        "  exit 1\n"
        "fi\n"
        "\n"
        'echo "check_git_changes: No uncommitted changes"\n'
        "exit 0\n"
    )


def _prepare_sh(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

python --version

python -m pip install --no-cache-dir {_BOOTSTRAP_PINS}

python -m pip install --no-cache-dir \\
    {_STACK_PINS}

python -m pip install --no-cache-dir --index-url {_TORCH_INDEX} {_TORCH_PIN}

export SKLEARN_BUILD_PARALLEL=4

if {_MESON_PROBE}; then
    python -m pip install --no-cache-dir --no-build-isolation --no-deps \\
        --config-settings=editable-verbose=false -v -e .
else
    python -m pip install --no-cache-dir --no-build-isolation --no-deps \\
        --no-use-pep517 -v -e .
fi

cd /
python -c "import sklearn; print('sklearn', sklearn.__version__, sklearn.__file__)"
python -c "import sklearn.linear_model, sklearn.metrics, sklearn.model_selection; from sklearn.utils import _safe_indexing"
python -c "import numpy, scipy, pandas, joblib, threadpoolctl, array_api_compat, torch, pytest; print('deps ok')"
pytest --version

echo "DEPS_OK"
"""


class ScikitLearnBase31906To27577(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-{_ERA_HI}_to_{_ERA_LO}"

    def workdir(self) -> str:
        return f"base-{_ERA_HI}_to_{_ERA_LO}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            clone = f"COPY {repo} /home/{repo}"

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"{self.clear_env}\n\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

{env_block}ARG TARGETARCH
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

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    OMP_NUM_THREADS=1 \\
    OPENBLAS_NUM_THREADS=1 \\
    MKL_NUM_THREADS=1 \\
    CFLAGS="-fno-tree-vectorize" \\
    CXXFLAGS="-fno-tree-vectorize"

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
    {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class ScikitLearnPR31906To27577(Image):
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
        return ScikitLearnBase31906To27577(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        header = _stage_header(repo)
        body = _test_body()

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _check_git_changes_sh()),
            File(".", "prepare.sh", _prepare_sh(repo, sha)),
            File(".", "run.sh", header + body),
            File(
                ".",
                "test-run.sh",
                header
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + body,
            ),
            File(
                ".",
                "fix-run.sh",
                header
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + body,
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

        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{sha}"

{copy_commands}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach ${{BASE_COMMIT}}

RUN bash /home/prepare.sh

{hardening}
{clear_block}"""


@Instance.register("scikit-learn", f"scikit_learn_{_ERA_HI}_to_{_ERA_LO}")
class SCIKIT_LEARN_31906_TO_27577(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScikitLearnPR31906To27577(self.pr, self._config)

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

        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()

            if stripped.startswith(_BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(_END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue

            name, status = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
