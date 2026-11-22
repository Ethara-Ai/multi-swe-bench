import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.12-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo_name = self.pr.repo

        return r"""# syntax=docker/dockerfile:1.6

FROM python:3.12-bookworm

ARG TARGETARCH
ARG REPO_URL="https://github.com/[[ORG]]/[[REPO_NAME]].git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="[[ORG]]/[[REPO_NAME]]" \
      org.opencontainers.image.description="[[ORG]]/[[REPO_NAME]] Docker image" \
      org.opencontainers.image.source="https://github.com/[[ORG]]/[[REPO_NAME]]" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    pkg-config \
    sudo \
    wget \
    libbz2-dev \
    libffi-dev \
    libhdf5-dev \
    liblz4-dev \
    libpq-dev \
    libsnappy-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/[[REPO_NAME]]

WORKDIR /home/[[REPO_NAME]]

CMD ["/bin/bash"]
""".replace(
            "[[ORG]]", org
        ).replace(
            "[[REPO_NAME]]", repo_name
        )


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

    def dependency(self) -> Union[str, Image]:
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        base_sha = self.pr.base.sha
        # PR 7639's fail-to-pass module is gated on Backend.get() == "Pandas"
        # (modin/tests/pandas/native_df_interoperability). MODIN_BACKEND and
        # MODIN_ENGINE/MODIN_STORAGE_FORMAT are mutually exclusive in modin.
        _forced = {
            7436: (
                "modin/tests/pandas/test_repartition.py modin/tests/test_utils.py",
                "test_repartition_not_valid_on_native_execution or "
                "test_warns_that_defaulting_to_pandas_if_false or "
                "test_warns_that_defaulting_to_pandas_if_non_bool or "
                "test_warns_that_defaulting_to_pandas_if_true",
            ),
            7639: (
                "modin/tests/pandas/native_df_interoperability/"
                "test_default_to_pandas_without_warnings.py",
                "test_crosstab_no_warning or test_json_normalize_no_warning",
            ),
            # The default selector harvests test names from diff context and
            # pytest -k matches them as substrings, so a patch touching modin's
            # mega-parametrized suites selects the whole suite: PR 7297 collected
            # 34417 tests (19% of stage 1 in 10h) and PR 6887 33390 (20% in 6h).
            # Restricted to the files that actually carry the fixed behaviour:
            # 41 and 96 tests respectively.
            7297: (
                "modin/tests/core/storage_formats/pandas/test_internals.py",
                "test_binary_op_preserve_dtypes or "
                "test_setitem_bool_preserve_dtypes or "
                "test_reindex_preserve_dtypes or "
                "test_concat_axis_1 or "
                "test_update_parent or "
                "test_set_index_with_dupl_labels or "
                "test_concat_mi or "
                "test_preserve_dtypes_setitem or "
                "test_preserve_dtypes_insert or "
                "test_get_dummies_case or "
                "test_preserve_dtypes_reset_index",
            ),
            # PR 7137 relocates modin/pandas/test -> modin/tests; the content
            # changes are import-path rewrites, so the harvested names come from
            # whichever test happened to enclose a rewritten import, and the
            # selection reached only 60% of stage 2 in 33min.  Its fix patch
            # touches no source file at all (MANIFEST.in, mypy.ini, setup.cfg
            # flake8/coverage only), so no f2p is possible either way.
            7137: (
                "modin/tests/config/test_envvars.py "
                "modin/tests/numpy/test_array.py "
                "modin/tests/test_utils.py",
                "test_doc_module or "
                "test_to_df or "
                "test_to_series or "
                "test_warns_that_defaulting_to_pandas",
            ),
            # PR 7275 removes the HDK engine; its test patch only strips HDK
            # guards/branches from 17 shared suites, and -k class-name matching
            # (TestCSV, TestConcat, ...) then selects those suites whole: 0% of
            # stage 1 after 20min.  Narrowed to the modified names in the small
            # files -> 81 tests, test stage == fix stage (80 passed, 1 skipped).
            7275: (
                "modin/tests/config/test_envvars.py "
                "modin/tests/test_utils.py "
                "modin/tests/pandas/dataframe/test_binary.py "
                "modin/tests/pandas/dataframe/test_reduce.py "
                "modin/tests/pandas/test_concat.py",
                "test_ray_cluster_resources or "
                "test_execute or "
                "test_math_alias or "
                "test_non_commutative_multiply or "
                "test_count_specific or "
                "test_reduce_specific or "
                "test_value_counts_categorical or "
                "test_concat_empty_df_series",
            ),
            6887: (
                "modin/config/test/test_envvars.py "
                "modin/test/test_partition_api.py "
                "modin/pandas/test/dataframe/test_binary.py "
                "modin/pandas/test/test_general.py "
                "modin/pandas/test/dataframe/test_indexing.py",
                "test_hdk_envvar or "
                "test_from_partitions or "
                "test_mismatched_row_partitions or "
                "test_merge or "
                "test_default_to_pandas_warning_message or "
                "test_indexing_duplicate_axis or "
                "test__getitem_bool_with_empty_partition or "
                "test_setitem_2d_insertion or "
                "test_empty_dataframe",
            ),
        }
        if self.pr.number in _forced:
            _t, _f = _forced[self.pr.number]
            modin_force_selection = (
                'export MSB_FORCE_TARGETS="%s"\nexport MSB_FORCE_FILTER="%s"' % (_t, _f)
            )
        else:
            modin_force_selection = ""

        # PR 7436's only new test is skipif(not current_execution_is_native()),
        # and its fix patch is what adds "Native" to Engine/StorageFormat.choices,
        # so the native execution mode is the only one that runs it post-fix.
        if self.pr.number in (7436,):
            _exec = "export MODIN_ENGINE=Native\nexport MODIN_STORAGE_FORMAT=Native"
        elif self.pr.number in (7639,):
            _exec = "export MODIN_BACKEND=Pandas"
        else:
            _exec = "export MODIN_ENGINE=dask\nexport MODIN_STORAGE_FORMAT=pandas"
        # PR 6673's new test pickles a Modin frame inside a dask task, whose
        # __reduce__ opens a nested Client and submits more work. With only two
        # workers both are blocked by the outer apply and the cluster deadlocks
        # (900s pytest-timeout); four workers leave room for the nested tasks.
        _cpus = "4" if self.pr.number in (6673,) else "2"
        # PR 6907 IS the pandas 2.1 -> 2.2 upgrade: its fix patch bumps the pin
        # in setup.py/requirements-dev.txt and rewrites modin for the 2.2 API
        # (ListAccessor, StructAccessor, "ME" freq alias, Series.case_when).
        # Built against the base pin (pandas 2.1) the fixed tree cannot import
        # at all, so the PR's own post-fix era is the correct one to install.
        _pins = {
            6907: '"pandas>=2.2,<2.3" "fsspec>=2022.11.0"',
        }
        if self.pr.number in _pins:
            modin_extra_pins = (
                "python -m pip install --no-cache-dir " + _pins[self.pr.number]
            )
        else:
            modin_extra_pins = ""
        modin_exec_env = _exec + "\nexport MODIN_CPUS=" + _cpus
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not a git repository"
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: the working tree is not clean"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: the working tree is clean"
""".replace(
                    "[[REPO_NAME]]", repo_name
                ).replace(
                    "[[MODIN_EXEC_ENV]]", modin_exec_env
                ).replace(
                    "[[MODIN_FORCE_SELECTION]]", modin_force_selection
                ),
            ),
            File(
                ".",
                "resolve_python.py",
                r"""import configparser
import glob
import os
import re
import tomllib

minimum = (3, 8)
maximum = (3, 13)
fallback = (3, 11)

spec = ""
lower = None
upper = None

if os.path.exists("pyproject.toml"):
    data = {}
    try:
        with open("pyproject.toml", "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        data = {}
    spec = (data.get("project") or {}).get("requires-python") or ""

if not spec and os.path.exists("setup.cfg"):
    parser = configparser.ConfigParser()
    try:
        parser.read("setup.cfg")
        spec = parser.get("options", "python_requires", fallback="")
    except Exception:
        spec = ""

if not spec and os.path.exists("setup.py"):
    body = open("setup.py", encoding="utf-8", errors="replace").read()
    direct = re.search(r'python_requires\s*=\s*[\'"]([^\'"]+)[\'"]', body)
    if direct:
        spec = direct.group(1)
    else:
        named = re.search(r"python_requires\s*=\s*([A-Za-z_][A-Za-z_0-9]*)", body)
        if named:
            pattern = r"^" + re.escape(named.group(1)) + r"\s*=\s*[\'\"]([^\'\"]+)[\'\"]"
            indirect = re.search(pattern, body, re.M)
            if indirect:
                spec = indirect.group(1)

if not spec and os.path.exists("setup.py"):
    body = open("setup.py", encoding="utf-8", errors="replace").read()
    holder = re.search(
        r"^(?:SUPPORTED_PYTHON|REQUIRES_PYTHON|PYTHON_REQUIRES)\s*=\s*[\'\"]([^\'\"]+)[\'\"]",
        body,
        re.M,
    )
    if holder:
        spec = holder.group(1)

for clause in spec.replace(" ", "").split(","):
    floor = re.match(r"^(?:>=|>|==|~=)3\.(\d+)", clause)
    if floor and lower is None:
        lower = (3, int(floor.group(1)))
    ceiling = re.match(r"^(<=?)3\.(\d+)", clause)
    if ceiling and upper is None:
        bound = int(ceiling.group(2))
        upper = (3, bound if ceiling.group(1) == "<=" else bound - 1)

chosen = lower

if chosen is None:
    tagged = []
    for name in ("setup.py", "setup.cfg", "pyproject.toml"):
        if os.path.exists(name):
            body = open(name, encoding="utf-8", errors="replace").read()
            tagged += re.findall(r"Programming Language :: Python :: 3\.(\d+)", body)
    if tagged:
        chosen = (3, max(int(value) for value in tagged))

if chosen is None:
    tested = []
    names = sorted(glob.glob(".github/workflows/*.yml"))
    names += sorted(glob.glob(".github/workflows/*.yaml"))
    names += ["tox.ini", ".travis.yml", ".appveyor.yml"]
    for name in names:
        if os.path.exists(name):
            body = open(name, encoding="utf-8", errors="replace").read()
            tested += re.findall(r"(?<![\d.])3\.(\d+)(?![\d.])", body)
    candidates = [int(value) for value in tested if int(value) <= maximum[1]]
    if candidates:
        chosen = (3, max(candidates))

if chosen is None:
    chosen = fallback
if upper is not None and chosen > upper:
    chosen = upper
if chosen < minimum:
    chosen = minimum
if chosen > maximum:
    chosen = maximum

print("%d.%d" % chosen)
""",
            ),
            File(
                ".",
                "filter_requirements.py",
                r"""import os
import re

sources = ("requirements-dev.txt", "requirements/requirements-dev.txt")
dropped = (
    "ray",
    "pydantic",
    "pymssql",
    "connectorx",
    "pandas-gbq",
    "pygit2",
    "pygithub",
    "fuzzydata",
    "numpydoc",
    "black",
    "flake8",
    "flake8-no-implicit-concat",
    "flake8-print",
    "mypy",
    "pandas-stubs",
    "isort",
    "modin-spreadsheet",
    "unidist",
)
added = (
    "beautifulsoup4",
    "html5lib",
    "tabulate",
    "xlsxwriter",
    "msgpack",
    "pytest-timeout",
)

lines = []
for source in sources:
    if os.path.exists(source):
        lines = open(source, encoding="utf-8", errors="replace").read().splitlines()
        break

kept = []
for line in lines:
    entry = line.split("#")[0].strip()
    if not entry:
        continue
    if entry.startswith("git+") or entry.startswith("-"):
        continue
    name = re.split(r"[\[<>=!~;]", entry)[0].strip().lower()
    if name in dropped:
        continue
    kept.append(entry)

for entry in added:
    kept.append(entry)

for entry in kept:
    print(entry)
""",
            ),
            File(
                ".",
                "select_tests.py",
                r"""import os
import re

# Per-PR override: a huge refactor patch yields hundreds of context-harvested
# names, which makes the selected run unbounded (PR 7436 reached 34% of stage 1
# in 24h). PR 7639 needs its f2p file collected ALONE, because its module-level
# skipif(Backend.get() != "Pandas") is evaluated at import time and another
# target file's conftest flips the backend first.
_ft = os.environ.get("MSB_FORCE_TARGETS")
if _ft:
    print(_ft)
    print(os.environ.get("MSB_FORCE_FILTER", ""))
    raise SystemExit(0)

patch = ""
if os.path.exists("/home/test.patch"):
    patch = open("/home/test.patch", encoding="utf-8", errors="replace").read()

targets = []
seen = set()
for match in re.finditer(r"^\+\+\+ b/(\S+)", patch, re.M):
    path = match.group(1)
    if path in seen:
        continue
    seen.add(path)
    if not re.search(r"(^|/)(test_[^/]*|[^/]*_test|tests)\.py$", path):
        continue
    if os.path.isfile(path):
        targets.append(path)

names = set()
for line in patch.splitlines():
    if line.startswith("+++") or line.startswith("---"):
        continue
    if line.startswith("@@"):
        tail = line.split("@@")[-1]
    elif line[:1] in ("+", "-", " "):
        tail = line[1:]
    else:
        continue
    found = re.search(
        r"(?:^|\s)(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z_0-9]*)", tail
    )
    if not found:
        continue
    name = found.group(1)
    if re.match(r"^(?:test_\w|Test\w)", name):
        names.add(name)

print(" ".join(targets))
print(" or ".join(sorted(names)))
""",
            ),
            File(
                ".",
                "report_tests.py",
                r"""import glob
import os
import xml.etree.ElementTree as ET

for path in sorted(glob.glob("/home/results-*.xml")):
    if not os.path.exists(path):
        continue
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue

    for testcase in root.iter("testcase"):
        classname = testcase.get("classname") or ""
        name = (testcase.get("name") or "").replace("\r", " ").replace("\n", " ")
        located = (testcase.get("file") or "").replace("\\", "/")
        holder = ""
        if located:
            file_path = located
            module = located[:-3].replace("/", ".") if located.endswith(".py") else ""
            if module and classname.startswith(module + "."):
                holder = classname[len(module) + 1:]
        else:
            parts = classname.split(".")
            if len(parts) > 1 and parts[-1][:1].isupper():
                holder = parts[-1]
                parts = parts[:-1]
            file_path = "/".join(parts) + ".py"

        status = "PASSED"
        for child in testcase:
            if child.tag in ("failure", "error"):
                status = "FAILED"
                break
            if child.tag == "skipped":
                status = "SKIPPED"

        node_id = file_path + "::" + (holder + "::" if holder else "") + name
        print("TESTCASE " + node_id + " " + status)
""",
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[BASE_SHA]]
bash /home/check_git_changes.sh

PYTHON_VERSION=$(/usr/local/bin/python3 -I /home/resolve_python.py)
echo "resolved interpreter: $PYTHON_VERSION"
uv python install "$PYTHON_VERSION"
uv venv --seed --python "$PYTHON_VERSION" /opt/venv

export VIRTUAL_ENV=/opt/venv
export PATH=/opt/venv/bin:$PATH

python --version
python -m pip install --no-cache-dir --upgrade pip setuptools wheel

python -m pip install --no-cache-dir -e ".[dask]"

/usr/local/bin/python3 -I /home/filter_requirements.py > /home/requirements-filtered.txt
echo "----- filtered requirements -----"
cat /home/requirements-filtered.txt
echo "---------------------------------"

if ! python -m pip install --no-cache-dir -r /home/requirements-filtered.txt; then
    echo "bulk requirement install failed, falling back to per-entry install"
    while IFS= read -r entry; do
        if [ -n "$entry" ]; then
            python -m pip install --no-cache-dir "$entry" || echo "skipped: $entry"
        fi
    done < /home/requirements-filtered.txt
fi

[[MODIN_EXTRA_PINS]]
python -m pip install --no-cache-dir "numpy<2"

cd /
python -c "import modin; print('modin', modin.__version__)"
python -c "import modin.pandas as mpd; print('modin.pandas ok')"
python -c "import pandas; print('pandas', pandas.__version__)"
python -c "import numpy; print('numpy', numpy.__version__)"
python -c "import dask, distributed; print('dask', dask.__version__, 'distributed', distributed.__version__)"
python -c "import fsspec, psutil, packaging; print('core extras ok')"
python -c "import matplotlib; matplotlib.use('Agg'); print('matplotlib', matplotlib.__version__)"
python -c "import pyarrow; print('pyarrow', pyarrow.__version__)"
python -c "import scipy; print('scipy', scipy.__version__)"
python -c "import sqlalchemy; print('sqlalchemy', sqlalchemy.__version__)"
python -c "import tqdm; print('tqdm', tqdm.__version__)"
python -c "import fastparquet; print('fastparquet', fastparquet.__version__)"
python -m pytest --version
python -c "import xdist, pytest_timeout; print('pytest plugins ok')"
echo "DEPS_OK"
""".replace(
                    "[[REPO_NAME]]", repo_name
                ).replace(
                    "[[BASE_SHA]]", base_sha
                ).replace(
                    "[[MODIN_EXTRA_PINS]]", modin_extra_pins
                ),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail

export VIRTUAL_ENV=/opt/venv
export PATH=/opt/venv/bin:$PATH

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export MPLBACKEND=Agg
[[MODIN_EXEC_ENV]]
[[MODIN_FORCE_SELECTION]]
export DASK_DISTRIBUTED__SCHEDULER__WORKER_TTL="3600s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__CONNECT="300s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__TCP="300s"
export DASK_DISTRIBUTED__WORKER__MEMORY__TERMINATE=False
export DASK_DISTRIBUTED__ADMIN__TICK__LIMIT="1h"
export OMP_NUM_THREADS=1
export NUMEXPR_MAX_THREADS=2

cd /home/[[REPO_NAME]]

rm -f /home/results-selected.xml /home/results-whole.xml

SELECTION=$(python -I /home/select_tests.py)
TEST_TARGETS=$(printf '%s\n' "$SELECTION" | sed -n '1p')
TEST_FILTER=$(printf '%s\n' "$SELECTION" | sed -n '2p')

echo "test targets: $TEST_TARGETS"
echo "test filter: $TEST_FILTER"

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
elif [ -n "$TEST_FILTER" ]; then
    set +e
    python -m pytest $TEST_TARGETS -q -k "$TEST_FILTER" \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-selected.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
else
    set +e
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-whole.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace(
                    "[[REPO_NAME]]", repo_name
                ).replace(
                    "[[MODIN_EXEC_ENV]]", modin_exec_env
                ).replace(
                    "[[MODIN_FORCE_SELECTION]]", modin_force_selection
                ),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail

export VIRTUAL_ENV=/opt/venv
export PATH=/opt/venv/bin:$PATH

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export MPLBACKEND=Agg
[[MODIN_EXEC_ENV]]
[[MODIN_FORCE_SELECTION]]
export DASK_DISTRIBUTED__SCHEDULER__WORKER_TTL="3600s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__CONNECT="300s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__TCP="300s"
export DASK_DISTRIBUTED__WORKER__MEMORY__TERMINATE=False
export DASK_DISTRIBUTED__ADMIN__TICK__LIMIT="1h"
export OMP_NUM_THREADS=1
export NUMEXPR_MAX_THREADS=2

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch

rm -f /home/results-selected.xml /home/results-whole.xml

SELECTION=$(python -I /home/select_tests.py)
TEST_TARGETS=$(printf '%s\n' "$SELECTION" | sed -n '1p')
TEST_FILTER=$(printf '%s\n' "$SELECTION" | sed -n '2p')

echo "test targets: $TEST_TARGETS"
echo "test filter: $TEST_FILTER"

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
elif [ -n "$TEST_FILTER" ]; then
    set +e
    python -m pytest $TEST_TARGETS -q -k "$TEST_FILTER" \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-selected.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
else
    set +e
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-whole.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace(
                    "[[REPO_NAME]]", repo_name
                ).replace(
                    "[[MODIN_EXEC_ENV]]", modin_exec_env
                ).replace(
                    "[[MODIN_FORCE_SELECTION]]", modin_force_selection
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail

export VIRTUAL_ENV=/opt/venv
export PATH=/opt/venv/bin:$PATH

export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export PYTHONHASHSEED=0
export PYTHONUNBUFFERED=1
export MPLBACKEND=Agg
[[MODIN_EXEC_ENV]]
[[MODIN_FORCE_SELECTION]]
export DASK_DISTRIBUTED__SCHEDULER__WORKER_TTL="3600s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__CONNECT="300s"
export DASK_DISTRIBUTED__COMM__TIMEOUTS__TCP="300s"
export DASK_DISTRIBUTED__WORKER__MEMORY__TERMINATE=False
export DASK_DISTRIBUTED__ADMIN__TICK__LIMIT="1h"
export OMP_NUM_THREADS=1
export NUMEXPR_MAX_THREADS=2

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

rm -f /home/results-selected.xml /home/results-whole.xml

SELECTION=$(python -I /home/select_tests.py)
TEST_TARGETS=$(printf '%s\n' "$SELECTION" | sed -n '1p')
TEST_FILTER=$(printf '%s\n' "$SELECTION" | sed -n '2p')

echo "test targets: $TEST_TARGETS"
echo "test filter: $TEST_FILTER"

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
elif [ -n "$TEST_FILTER" ]; then
    set +e
    python -m pytest $TEST_TARGETS -q -k "$TEST_FILTER" \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-selected.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
else
    set +e
    python -m pytest $TEST_TARGETS -q \
        -p no:cacheprovider \
        --color=no \
        --tb=short \
        --timeout=900 \
        --continue-on-collection-errors \
        --override-ini=addopts= \
        --override-ini=junit_family=xunit1 \
        --override-ini='python_files=test_*.py *_test.py tests.py' \
        --junitxml=/home/results-whole.xml
    RC=$?
    set -e
    echo "TEST_EXIT_CODE=$RC"
fi

echo "===== BEGIN TEST DETAIL ====="
python -I /home/report_tests.py
echo "===== END TEST DETAIL ====="
""".replace(
                    "[[REPO_NAME]]", repo_name
                ).replace(
                    "[[MODIN_EXEC_ENV]]", modin_exec_env
                ).replace(
                    "[[MODIN_FORCE_SELECTION]]", modin_force_selection
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency().image_full_name()
        base_sha = self.pr.base.sha

        copy_commands = "COPY " + " ".join(f.name for f in self.files()) + " /home/\n"

        return r"""FROM [[BASE_IMAGE]]

ARG BASE_COMMIT="[[BASE_SHA]]"

[[COPY_COMMANDS]]
RUN bash /home/prepare.sh

RUN git reset --hard && git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
""".replace(
            "[[BASE_IMAGE]]", base_image
        ).replace(
            "[[BASE_SHA]]", base_sha
        ).replace(
            "[[COPY_COMMANDS]]", copy_commands
        )


@Instance.register("modin-project", "modin")
class MODIN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        pattern = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")
        in_detail = False

        for line in log.splitlines():
            stripped = line.strip()
            if stripped.startswith("===== BEGIN TEST DETAIL ====="):
                in_detail = True
                continue
            if stripped.startswith("===== END TEST DETAIL ====="):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = pattern.match(stripped)
            if not match:
                continue

            if match.group(2) == "PASSED":
                passed_tests.add(match.group(1))
            elif match.group(2) == "FAILED":
                failed_tests.add(match.group(1))
            else:
                skipped_tests.add(match.group(1))

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
