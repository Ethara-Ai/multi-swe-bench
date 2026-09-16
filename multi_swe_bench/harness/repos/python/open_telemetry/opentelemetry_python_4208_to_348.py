import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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


_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/opentelemetry-python
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


_CONFTEST_REPORT_PY = '''"""pytest plugin: write `<nodeid>\\t<outcome>` per test to $PYTEST_REPORT_FILE."""
import os

_STORE = {}


def pytest_runtest_logreport(report):
    nodeid = report.nodeid
    if report.when == "call":
        _STORE[nodeid] = "passed" if report.passed else ("skipped" if report.skipped else "failed")
    elif report.when in ("setup", "teardown"):
        if report.failed:
            _STORE[nodeid] = "failed"
        elif report.skipped and nodeid not in _STORE:
            _STORE[nodeid] = "skipped"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("PYTEST_REPORT_FILE", "/tmp/pytest_report.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        for nodeid, outcome in sorted(_STORE.items()):
            fh.write("%s\\t%s\\n" % (nodeid, outcome))
'''


_PYTEST_TEST_REPORT_PY = '''"""Turn the conftest plugin's TSV into per-test result lines.

usage: pytest_test_report.py <tsv>

    pytest:ext/.../tests/test_otcollector_metrics_exporter.py::Test::test_export PASSED

Exit 0 all passed, 1 at least one failed, 2 the run produced NO tests at all
(the runner never started -- the case FLOW GATE 0 exists to catch, and which
must never be confused with "zero tests passed").
"""
import sys

STATUS = {"passed": "PASSED", "failed": "FAILED", "skipped": "SKIPPED"}


def main():
    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rows = [l.rstrip("\\n").split("\\t") for l in fh if l.strip()]
    except OSError as exc:
        sys.stderr.write("pytest_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    if not rows:
        sys.stderr.write(
            "pytest_test_report: the run reported no tests; the runner never started\\n")
        return 2

    failed = False
    for row in rows:
        if len(row) != 2:
            continue
        nodeid, outcome = row
        status = STATUS.get(outcome, "FAILED")
        print("pytest:%s %s" % (nodeid, status))
        if status == "FAILED":
            failed = True

    sys.stderr.write("pytest_test_report: %d test(s) recorded\\n" % len(rows))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/opentelemetry-python
export PYTHONPATH=/home
export PYTEST_REPORT_FILE=/tmp/pytest_report.tsv
rm -f "$PYTEST_REPORT_FILE"

. /home/install_set.sh
msb_core_dirs
msb_constraints
msb_install > /dev/null 2>&1 || true


TARGETS=""
for d in $(cat /home/packages_under_test.txt 2>/dev/null); do
    [ -d "$d/tests" ] && TARGETS="$TARGETS $d/tests"
done
if [ -z "$TARGETS" ]; then
    for d in $(msb_install_dirs); do
        [ -d "$d/tests" ] && TARGETS="$TARGETS $d/tests"
    done
fi
echo "pytest targets: ${TARGETS}"

: > "$PYTEST_REPORT_FILE"
pytest_rc=0
for t in ${TARGETS}; do
    part="/tmp/pytest_part.tsv"
    rm -f "$part"
    PYTEST_REPORT_FILE="$part" python -m pytest "$t" -p conftest_report --import-mode=importlib --continue-on-collection-errors
    rc=$?
    [ "$rc" -gt "$pytest_rc" ] && pytest_rc="$rc"
    [ -f "$part" ] && cat "$part" >> "$PYTEST_REPORT_FILE"
done
echo "pytest exit=${pytest_rc}"
echo "----- per-test results -----"
python /home/pytest_test_report.py "$PYTEST_REPORT_FILE"
"""


BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM %(image)s

ARG TARGETARCH
ARG REPO_URL="https://github.com/%(org)s/%(repo)s.git"
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
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="%(org)s/%(repo)s" \\
      org.opencontainers.image.description="%(org)s/%(repo)s Docker image" \\
      org.opencontainers.image.source="https://github.com/%(org)s/%(repo)s" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/%(repo)s

CMD ["/bin/bash"]
"""


_INSTALL_SET_SH = r"""#!/bin/bash

msb_core_dirs() {
    { find . -mindepth 2 -maxdepth 2 \( -name setup.py -o -name pyproject.toml \) -printf '%h\n'
      find ./tests -mindepth 2 -maxdepth 2 \( -name setup.py -o -name pyproject.toml \) -printf '%h\n' 2>/dev/null
    } | sed 's|^\./||' | grep -vE '(^|/)opentelemetry-distro$' | sort -u > /home/core_dirs.txt
    [ -f /home/dep_dirs.txt ] || : > /home/dep_dirs.txt
}

msb_install_dirs() {
    cat /home/core_dirs.txt /home/dep_dirs.txt /home/packages_under_test.txt 2>/dev/null \
        | awk 'NF && !seen[$0]++'
}

msb_constraints() {
    local date
    date=$(git show -s --format=%cd --date=short HEAD)
    python3 /home/era_constraints.py "$date" /home/constraints.txt         $(cat /home/packages_under_test.txt 2>/dev/null) || return 0
    [ -s /home/constraints.txt ] || return 0

    export PIP_CONSTRAINT=/home/constraints.txt
    return 0
}

msb_install() {
    local attempt miss dir args predeps
    python -m pip install --no-cache-dir "cython<3" > /tmp/msb_cython.log 2>&1 || true
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
        predeps=""
        for dir in $(msb_install_dirs); do
            [ -d "$dir" ] && predeps="$predeps -e ./$dir"
        done
        [ -n "$predeps" ] && python -m pip install --no-cache-dir --no-deps $predeps > /tmp/msb_prepass.log 2>&1 || true
        args=""
        for dir in $(msb_install_dirs); do
            [ -d "$dir" ] && args="$args -e ./$dir[test]"
        done
        if python -m pip install --no-cache-dir $args > /tmp/msb_install.log 2>&1; then
            echo "install: satisfied on pass $attempt"
            return 0
        fi
        nobuild=$(grep -oE 'Could not build wheels for [A-Za-z0-9_.-]+' /tmp/msb_install.log \
                  | tail -1 | awk '{print $NF}' | sed 's/,.*//')
        if [ -n "$nobuild" ] && [ -f /home/constraints.txt ]; then
            echo "install: pass $attempt - $nobuild will not build here, dropping its pin"
            grep -vE "^$nobuild==" /home/constraints.txt > /home/constraints.tmp
            mv /home/constraints.tmp /home/constraints.txt
            continue
        fi

        clash=$(grep -oE 'The user requested \(constraint\) [A-Za-z0-9_.-]+' /tmp/msb_install.log \
                | tail -1 | awk '{print $NF}')
        if [ -n "$clash" ] && [ -f /home/constraints.txt ]; then
            echo "install: pass $attempt - constraint $clash conflicts, dropping it"
            grep -vE "^$clash==" /home/constraints.txt > /home/constraints.tmp
            mv /home/constraints.tmp /home/constraints.txt
            continue
        fi

        miss=$(grep -oE 'No matching distribution found for [A-Za-z0-9_.-]+' /tmp/msb_install.log \
               | tail -1 | awk '{print $NF}' | sed 's/==.*//')
        [ -n "$miss" ] || break
        dir=$(grep -rlE "^[[:space:]]*name[[:space:]]*=[[:space:]]*[\"']?$miss[\"']?[[:space:]]*$" \
              --include=setup.cfg --include=pyproject.toml . 2>/dev/null | head -1)
        [ -n "$dir" ] || break
        dir=$(dirname "$dir" | sed 's|^\./||')
        echo "install: pass $attempt needs $miss -> adding $dir"
        echo "$dir" >> /home/dep_dirs.txt
    done
    echo "install: FAILED" >&2
    tail -25 /tmp/msb_install.log >&2
    return 1
}
"""


_ERA_CONSTRAINTS_PY = r'''#!/usr/bin/env python3
"""Pin the third-party dependencies of the packages under test to the commit date.

usage: era_constraints.py <YYYY-MM-DD> <out-file> <pkg-dir> [<pkg-dir> ...]

The 2020 code in this tree is tested against whatever PyPI serves today, and the drift
breaks the tests rather than the install, so it survives every earlier gate and surfaces as
"no tests collected":

          ModuleNotFoundError: No module named 'google.cloud.trace_v2.proto'
          -- google-cloud-trace moved that subpackage after 1.0.

          KeyError: 'ap-south-2'
          -- today's botocore lists a region today's-minus-N moto has no zones for.

Neither is a fact about the config; both are facts about the commit date. So the version is
DERIVED: for every distribution the packages under test declare - install_requires and the
[test] extra, read out of their own setup.cfg - take the newest release that existed when
the commit was written. Nothing is written down here, and a sixth PR needs no edit.

Only the packages under test are constrained. Constraining everything would drag setuptools
and pip back to 2020 too, which breaks the build for no gain.
"""
import concurrent.futures as cf
import configparser
import json
import os
import re
import sys
import urllib.request

DATE, OUT = sys.argv[1], sys.argv[2]
DIRS = sys.argv[3:]
UA = {"User-Agent": "multi-swe-bench opentelemetry-python image build"}

_NO_PIN = {"grpcio"}


def requirement_names(pkg_dir):
    """Distribution names this package declares, from setup.cfg."""
    cfg = os.path.join(pkg_dir, "setup.cfg")
    if not os.path.exists(cfg):
        return set()
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg)
    except Exception:
        return set()
    blobs = []
    if parser.has_option("options", "install_requires"):
        blobs.append(parser.get("options", "install_requires"))
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            blobs.append(value)
    names = set()
    for blob in blobs:
        for line in blob.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip()
            if name and not name.startswith("opentelemetry") and name not in _NO_PIN:
                names.add(name)
    return names


def requires_of(name, version):
    """Distribution names that <name>==<version> depends on, from PyPI metadata.

    Only base requirements: anything guarded by `extra == "..."` belongs to an optional
    feature nobody asked for, and pulling those in widens the pin set for nothing.
    """
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=45) as fh:
            data = json.load(fh)
    except Exception:
        return set()
    out = set()
    for spec in (data.get("info", {}).get("requires_dist") or []):
        if "extra ==" in spec:
            continue
        dep = re.split(r"[<>=!~\[;( ]", spec.strip(), 1)[0].strip()
        if dep and not dep.startswith("opentelemetry") and dep not in _NO_PIN:
            out.add(dep)
    return out


def newest_before(name):
    url = "https://pypi.org/pypi/%s/json" % name
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as fh:
                data = json.load(fh)
            break
        except Exception:
            data = None
    if not data:
        return None
    best = None
    for version, files in data.get("releases", {}).items():
        if not files:
            continue
        uploaded = min(f["upload_time"][:10] for f in files)
        if uploaded <= DATE and (best is None or uploaded > best[0]):
            best = (uploaded, version)
    return best[1] if best else None


def main():
    """Pin the closure, not just the directly-declared names.

    Pinning only what setup.cfg names leaves pip to resolve everything underneath, and
    that is where it stalls: #866 pins botocore==1.17.14, moto needs boto3, boto3 is NOT
    declared anywhere in this repo, and pip walks backwards through several hundred boto3
    releases looking for one that accepts a 2020 botocore. Measured: 100% CPU, no output,
    still going after 13 minutes.

    Following requires_dist to a small depth pins boto3 too, so the resolver is handed a
    fully-determined set and has nothing left to search. Depth 3 covers moto -> boto3 ->
    botocore/jmespath/s3transfer, which is as deep as this repo's test dependencies go.
    """
    frontier = set()
    for d in DIRS:
        frontier |= requirement_names(d)
    if DATE < "2022-01-01":
        frontier |= {"protobuf"}
    frontier -= _NO_PIN
    if not frontier:
        open(OUT, "w").close()
        print("era: nothing to constrain")
        return 0

    picked = {}
    for depth in range(3):
        todo = sorted(n for n in frontier if n not in picked)
        if not todo:
            break
        with cf.ThreadPoolExecutor(8) as ex:
            for name, version in zip(todo, ex.map(newest_before, todo)):
                picked[name] = version
        nxt = set()
        with cf.ThreadPoolExecutor(8) as ex:
            pairs = [(n, picked[n]) for n in todo if picked[n]]
            for deps in ex.map(lambda p: requires_of(*p), pairs):
                nxt |= deps
        frontier |= nxt

    with open(OUT, "w", encoding="utf-8") as fh:
        for name in sorted(picked):
            version = picked[name]
            if version:
                fh.write("%s==%s\n" % (name, version))
            else:
                print("era: %s - no release on or before %s, left unpinned" % (name, DATE))
    print("era: pinned %d distribution(s) to %s" % (
        sum(1 for v in picked.values() if v), DATE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


_HELPER_FILES = [
    ("check_git_changes.sh", "_CHECK_GIT_CHANGES_SH"),
    ("install_set.sh", "_INSTALL_SET_SH"),
    ("era_constraints.py", "_ERA_CONSTRAINTS_PY"),
    ("apply_patch.sh", "_APPLY_PATCH_SH"),
    ("conftest_report.py", "_CONFTEST_REPORT_PY"),
    ("pytest_test_report.py", "_PYTEST_TEST_REPORT_PY"),
    ("run_tests.sh", "_RUN_TESTS_SH"),
]


def _emit_helpers(exclude=()) -> str:
    out = []
    for name, const in _HELPER_FILES:
        if name in exclude:
            continue
        body = globals()[const]
        marker = "MSB_EOF_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        out.append(
            "cat > /home/%s <<'%s'\n%s\n%s\n" % (name, marker, body.rstrip("\n"), marker)
        )
        if name.endswith(".sh"):
            out.append("chmod +x /home/%s\n" % name)
        out.append("\n")
    return "".join(out)


def _packages_under_test(pr) -> list[str]:
    dirs = set()
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)", pr.test_patch or "", re.M):
        parts = path.split("/")
        if "tests" in parts:
            pkg = "/".join(parts[: parts.index("tests")])
            if pkg:
                dirs.add(pkg)
    return sorted(dirs)


class OpentelemetryPythonImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE % {
            "image": self.dependency(),
            "org": self.pr.org,
            "repo": self.pr.repo,
        }


class OpentelemetryPythonImageDefault(Image):

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
        return OpentelemetryPythonImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/@@REPO@@

bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse @@SHA@@)"

@@HELPERS@@
cat > /home/packages_under_test.txt <<'MSB_PKGS_EOF'
@@PACKAGES@@
MSB_PKGS_EOF

. /home/install_set.sh
msb_core_dirs
msb_constraints
msb_install

if msb_install_dirs | grep -q 'otcollector'; then
    python -m pip install --no-cache-dir "protobuf<3.20"
fi

python -m pip install --no-cache-dir "pytest~=7.4"

python --version
python -c 'import pytest; print(pytest.__version__)'
python -c 'import opentelemetry.sdk'
if msb_install_dirs | grep -q 'otcollector'; then
  python -c 'import google.protobuf, grpc; print(google.protobuf.__version__)'
  python -c 'from opentelemetry.ext.otcollector import trace_exporter'
fi

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

"""
                .replace("@@REPO@@", self.pr.repo)
                .replace("@@SHA@@", self.pr.base.sha)
                .replace("@@PACKAGES@@", "\n".join(_packages_under_test(self.pr)))
                .replace("@@HELPERS@@", _emit_helpers(exclude=("check_git_changes.sh",))),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/opentelemetry-python
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/opentelemetry-python
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/opentelemetry-python
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {image_name}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copies}
RUN bash /home/prepare.sh

{hardening}"""


@Instance.register("open-telemetry", "opentelemetry-python")
class OpentelemetryPython(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpentelemetryPythonImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

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
