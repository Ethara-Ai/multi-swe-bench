import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.pandas_dev.pandas import Pandas

_ERA_HI = 71337
_ERA_LO = 67925

_PYTHON_IMAGE = "python:3.12-slim-bookworm"

_BUILD_DIR = "/home/pandas_build"

_APT_PACKAGES = "build-essential ca-certificates git pkg-config tzdata"

_BOOTSTRAP_PINS = '"pip==26.2.1" "setuptools==84.0.0" "wheel==0.48.0"'

_STACK_PINS = (
    "annotated-types==0.8.0",
    "anyio==4.14.2",
    "asttokens==3.0.2",
    "beautifulsoup4==4.15.0",
    "blosc2==4.12.0",
    "bottleneck==1.6.0",
    "certifi==2026.7.22",
    "contourpy==1.3.3",
    "cycler==0.12.1",
    "cython==3.2.9",
    "defusedxml==0.7.1",
    "et-xmlfile==2.0.0",
    "executing==2.3.0",
    "fonttools==4.64.0",
    "fsspec==2026.7.0",
    "greenlet==3.5.5",
    "h11==0.16.0",
    "h2==4.4.1",
    "hpack==4.2.0",
    "html5lib==1.1",
    "httpcore==1.0.9",
    "httpx==0.28.1",
    "hyperframe==6.1.0",
    "idna==3.19",
    "iniconfig==2.3.0",
    "ipython==9.18.0",
    "ipython-pygments-lexers==1.1.1",
    "jedi==0.20.1",
    "jinja2==3.1.6",
    "kiwisolver==1.5.1",
    "lxml==6.1.2",
    "markdown-it-py==4.2.0",
    "markupsafe==3.0.3",
    "matplotlib==3.11.1",
    "matplotlib-inline==0.2.2",
    "mdurl==0.1.2",
    "meson==1.12.0",
    "meson-python==0.20.0",
    "msgpack==1.2.2",
    "narwhals==2.25.0",
    "ndindex==1.10.1",
    "ninja==1.13.2",
    "numexpr==2.14.2",
    "numpy==2.5.2",
    "odfpy==1.4.1",
    "openpyxl==3.1.5",
    "packaging==26.3",
    "parso==0.8.7",
    "pexpect==4.9.0",
    "pillow==12.3.0",
    "pluggy==1.6.0",
    "prompt-toolkit==3.0.53",
    "psutil==7.2.2",
    "ptyprocess==0.7.0",
    "pure-eval==0.2.4",
    "py-cpuinfo==9.0.0",
    "pyarrow==25.0.1",
    "pydantic==2.13.5",
    "pydantic-core==2.46.5",
    "pygments==2.21.0",
    "pyparsing==3.3.2",
    "pyproject-metadata==0.12.1",
    "pyreadstat==1.3.6",
    "pytest==9.1.1",
    "pytest-localserver==0.10.0",
    "python-calamine==0.8.2",
    "python-dateutil==2.9.0.post0",
    "pytz==2026.3.post1",
    "pyxlsb==1.0.10",
    "rich==15.0.0",
    "scipy==1.18.1",
    "six==1.17.0",
    "soupsieve==2.9.2",
    "sqlalchemy==2.0.52",
    "stack-data==0.6.3",
    "tables==3.11.1",
    "tabulate==0.10.0",
    "threadpoolctl==3.6.0",
    "traitlets==5.16.1",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.4",
    "tzdata==2026.3",
    "versioneer==0.29",
    "wcwidth==0.9.4",
    "webencodings==0.6.1",
    "werkzeug==3.1.8",
    "xarray==2026.7.0",
    "xlrd==2.0.2",
    "xlsxwriter==3.2.9",
    "zstandard==0.25.0",
)

_BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
_END_MARKER = "===== END TEST DETAIL ====="

_TEST_BODY = r"""
print_test_detail() {
    echo "__BEGIN__"
    python -I - <<'PY'
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
    | grep -E '(^|/)test_[^/]*\.py$' \
    | sort -u)
python -c "import pandas; print('pandas', pandas.__version__, pandas.__git_version__)"
BUILD_RC=$?
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ "$BUILD_RC" -ne 0 ]; then
    echo "pandas failed to build or import"
    print_test_detail
elif [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    print_test_detail
else
    echo "running pytest on:$TEST_TARGETS"
    set +e
    python -m pytest $TEST_TARGETS -q \
        -m "not network and not db" \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --junitxml=/home/results.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
    print_test_detail
fi
"""


def _test_body() -> str:
    return _TEST_BODY.replace("__BEGIN__", _BEGIN_MARKER).replace(
        "__END__", _END_MARKER
    )


def _stage_header(repo: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "export PANDAS_CI=1\n"
        "export PYTHONHASHSEED=0\n"
        "export TZ=UTC\n"
        "export LANG=C.UTF-8\n"
        "export LC_ALL=C.UTF-8\n"
        "export OMP_NUM_THREADS=1\n"
        "export OPENBLAS_NUM_THREADS=1\n"
        "export MKL_NUM_THREADS=1\n"
        "export NUMEXPR_MAX_THREADS=1\n"
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

rm -rf {_BUILD_DIR}
python -m pip install --no-cache-dir --no-build-isolation --no-deps \\
    --config-settings=builddir={_BUILD_DIR} \\
    --config-settings=editable-verbose=false \\
    -v -e .

bash /home/check_git_changes.sh

cd /
python -c "import pandas; print('pandas', pandas.__version__, pandas.__file__)"
python -c "import pandas._libs.lib, pandas._libs.tslibs, pandas.io.parsers, pandas.io.sas"
python -c "import pyarrow, numexpr, openpyxl, jinja2, pytest, IPython; print('deps ok')"
python -m pip check
pytest --version

echo "DEPS_OK"
"""


class PandasBase71337To67925(Image):
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
        pins = " \\\n    ".join(f'"{pin}"' for pin in _STACK_PINS)

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
    MKL_NUM_THREADS=1

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

RUN python -m pip install --no-cache-dir {_BOOTSTRAP_PINS}

RUN python -m pip install --no-cache-dir --no-deps \\
    {pins}

WORKDIR /home/

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class PandasPR71337To67925(Image):
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
        return PandasBase71337To67925(self.pr, self._config)

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

RUN rm -f .git/ORIG_HEAD .git/FETCH_HEAD .git/MERGE_HEAD

{hardening}

RUN test ! -e .git/ORIG_HEAD && \\
    test -z "$(git reflog)" && \\
    test -z "$(git fsck --unreachable --no-reflogs --no-progress 2>/dev/null)"
{clear_block}"""


@Instance.register("pandas-dev", f"pandas_{_ERA_HI}_to_{_ERA_LO}")
class PANDAS_71337_TO_67925(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PandasPR71337To67925(self.pr, self._config)

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


@Instance.register("pandas-dev", "pandas")
class PandasRouter71337To67925(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _ERA_LO <= pr.number <= _ERA_HI:
            return PANDAS_71337_TO_67925(pr, config, *args, **kwargs)
        return Pandas(pr, config, *args, **kwargs)
