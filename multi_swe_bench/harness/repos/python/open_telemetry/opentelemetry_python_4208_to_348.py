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


# apply_patch.sh -- plain `git apply` first, `--3way` only as an ANNOUNCED
# fallback. FLOW VERDICT DISCIPLINE requires a failure to be counted from the
# PRIMARY apply, so a silent --3way rescue would hide a genuinely broken patch.
# This PR's test patch contains a RENAME (see _RUN_TESTS_SH), which `git apply`
# handles natively. Measured: both patches apply cleanly, 0 binary hunks.
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


# conftest_report.py -- a pytest plugin recording one line per test.
#
# WHY NOT PARSE pytest's CONSOLE OUTPUT: every `-v` result line carries a
# trailing progress percentage --
#     .../test_otcollector_exporter.py::Test::test_x PASSED  [ 60%]
# -- and that percentage MOVES when the test count changes. This PR takes the
# suite from 5 tests to 10, so every percentage shifts between acts. Folding it
# into the name would manufacture a false transition for every test (audit 4B).
#
# The plugin records the nodeid and outcome directly, so a name is
# byte-identical across acts. It lives in /home, never the work tree, so it
# cannot dirty `git status` (FLOW Issue 7) -- which is why run_tests.sh must
# export PYTHONPATH=/home for `-p conftest_report` to import it.
_CONFTEST_REPORT_PY = '''"""pytest plugin: write `<nodeid>\\t<outcome>` per test to $PYTEST_REPORT_FILE."""
import os

_STORE = {}


def pytest_runtest_logreport(report):
    # `when` matters: a test that errors during SETUP never reaches "call" and
    # must still be recorded, or it silently vanishes and looks uncollected --
    # the shape FLOW GATE 0 exists to catch.
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


# run_tests.sh -- the graded command, identical in all three acts.
#
# `--continue-on-collection-errors` IS LOAD-BEARING, not tidiness. This PR's
# test patch adds test_otcollector_metrics_exporter.py, which imports
# `metrics_exporter` -- a module only the FIX patch creates. Without the flag,
# pytest treats that ONE uncollectable file as fatal for the WHOLE session:
#
#   ImportError: cannot import name 'metrics_exporter' ...
#   !!!! Interrupted: 1 error during collection !!!!
#   ===== no tests collected, 1 error =====
#
# i.e. a flat 0/0/0 test act that discards the 5 OTHER tests which ran fine --
# textbook FLOW **Issue 28**. With the flag: `5 passed, 1 error`, and the
# genuinely-blocked file is the only thing lost.
#
# NOTE the test patch also RENAMES test_otcollector_exporter.py ->
# test_otcollector_trace_exporter.py. pytest nodeids embed the file path, so
# those 5 tests carry different names before and after. They are NOT lost
# evidence: they pass in the test act under the new name, so the classifier's
# `test == PASS -> p2p` branch takes them, and they do not inflate n2p
# (FLOW Issues 9 / 27).
#
# `set -e` is absent (only -uo pipefail): pytest exits non-zero on a collection
# error, which is EXPECTED in the test act. Under -e the act would abort before
# the report script ran and score a silent 0/0/0 (GATE 0).
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/opentelemetry-python
export PYTHONPATH=/home
export PYTEST_REPORT_FILE=/tmp/pytest_report.tsv
rm -f "$PYTEST_REPORT_FILE"

# SCOPE IS ERA-ADAPTIVE, and it has to be. This file registers the PLAIN key, and
# prep_dataset.py strips number_interval (FLOW STEP 1), so EVERY opentelemetry-python
# entry resolves here -- not just the PR this recipe was first measured on. The repo
# was reorganised between eras:
#
#   2020 (PR #454 base 4b6a52d6) : ext/opentelemetry-ext-otcollector/...   <- exists
#   present day                   : ext/ IS GONE -> exporter/ propagator/ shim/
#
# A hardcoded `pytest ext/opentelemetry-ext-otcollector/tests/` therefore collects
# ZERO tests on any later PR. The detection below keeps the narrow, fully-measured
# scope for the era that has it, and falls back to the repo-wide layout otherwise.
# The image installed BEFORE any patch existed, so a package the patches CREATE is
# importable nowhere. #719 is exactly that: ext/opentelemetry-ext-sqlite3 does not exist at
# its base commit - the fix patch adds the whole package, and its tests cannot import it
# unless something installs it after the patch lands. The resolver also picks up the
# in-repo dependency that package pulls in (ext-dbapi), which no patch mentions.
#
# Runs in ALL THREE acts, not just fix-run. The three stages have to see the same treatment
# or the comparison between them stops meaning anything. Non-fatal here, unlike in
# prepare.sh: in the run and test acts the package usually does not exist yet.
. /home/install_set.sh
msb_core_dirs
msb_constraints
msb_install > /dev/null 2>&1 || true

# --import-mode=importlib is required, not cosmetic. Several packages ship their own
# tests/conftest.py and none of those tests/ directories has an __init__.py, so under
# pytest's default prepend import mode every one of them wants the module name
# `tests.conftest`. Collect two in a single run and pytest raises ImportPathMismatchError
# and aborts with exit 4 and ZERO tests - measured with opentelemetry-sdk/tests and
# ext/opentelemetry-ext-boto/tests. importlib mode imports each by path and they coexist.

# SCOPE IS DERIVED from what prepare.sh installed, not from a written-out list of glob
# patterns. msb_install_dirs holds the core packages plus the ones this PR's test patch
# touches; their tests/ directories are exactly the tests that can run here.
#
# The previous version hardcoded `ext/*/tests exporter/*/tests propagator/*/tests ...`,
# a list that has to be re-guessed every time the repo is reorganised - and an earlier
# revision was wrong in the other direction: it collected ONLY
# ext/opentelemetry-ext-otcollector/tests, so #678 (opentracing-shim) and #719 (sqlite3)
# ran with none of their own tests collected at all.
#
# Scoping to the installed set also means no collection errors: a package that was never
# installed is never collected.
TARGETS=""
for d in $(cat /home/packages_under_test.txt 2>/dev/null); do
    [ -d "$d/tests" ] && TARGETS="$TARGETS $d/tests"
done
# Grade ONLY the packages this PR's test patch touches, not every installed core package.
# Running unrelated core suites (e.g. opentelemetry-sdk for an opentelemetry-api PR) drags
# their flaky/timing tests into the verdict: pr-1134 (api, SpanContext immutability) was
# invalidated by sdk's flaky test_batch_span_processor_many_spans, and pr-1285 by an sdk
# test mutating a global attribute limit. The f2p/n2p that decide resolution live in the
# touched package's own tests, so this stays complete while removing cross-package noise.
# Fallback to the installed set only if the patch named no package under test.
if [ -z "$TARGETS" ]; then
    for d in $(msb_install_dirs); do
        [ -d "$d/tests" ] && TARGETS="$TARGETS $d/tests"
    done
fi
echo "pytest targets: ${TARGETS}"

# Run each target package's tests in its OWN pytest process, then aggregate the per-test
# TSVs. A single combined session lets one package's tests mutate global state that another
# package's tests then read -- e.g. an opentelemetry-sdk test lowers the global attribute
# value-length limit, so a later opentelemetry-exporter-zipkin test finds its 500-char tag
# already dropped and asserts KeyError (pr-1285). Every test still runs and still counts
# (p2p is unchanged); they simply no longer share a process. The same treatment runs in all
# three acts, so the cross-act comparison stays valid.
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


# The base image is written out IN FULL - syntax directive, ARGs, proxy env, cert
# symlinks, OCI labels and all - and that is the whole point.
#
# DockerfileEnhancer.enhance() returns the Dockerfile untouched the moment it already
# carries the syntax directive (image.py:316-317). Without that early return it runs
# _inject_final_sanitize(), which appends `git checkout ${BASE_COMMIT}` plus the history
# scrub to ANY Dockerfile containing a `git clone` (image.py:389-395).
#
# For a per-PR base that injection is harmless. For a base SHARED by five PRs it is
# fatal: the base would be pinned to whichever commit happened to build it first, its
# history scrubbed down to that one commit, and every other PR's `git checkout <sha>`
# would then fail on an object that no longer exists.
#
# So the enhancer is bypassed on purpose, and the infrastructure it would have added is
# reproduced here verbatim. The config audit flags a hand-written infra block as a WARN;
# it is accepted here because a single shared base cannot be had any other way.
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
# Shared by prepare.sh (image build) and run_tests.sh (every act). Sourced, not executed.
#
# The install set is derived in three layers, and the ORDER they are concatenated in is
# load-bearing, because pip resolves local editables in the order it is given them:
#
#   core        every setup.py directory at the repo root, plus any under tests/. These are
#               what the extensions pin at the in-tree dev version. Scanning for setup.py
#               survives the ext/ -> exporter/ propagator/ shim/ rename between eras.
#   deps        in-repo packages discovered by msb_install() below.
#   under test  the packages whose tests THIS patch touches.
#
# A dependency listed AFTER its dependent is not found - pip goes to PyPI for 0.8.dev0 and
# fails - so deps sit between core and the packages under test.

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

# Pin the third-party dependencies of the packages under test to the commit date, and
# hand the result to pip as a constraints file. See /home/era_constraints.py for why: the
# drift breaks the TESTS, not the install, so without this it surfaces three stages later
# as "no tests collected" (#819 google-cloud-trace, #866 moto).
#
# The date comes from the commit itself, so nothing here has to be updated per PR.
msb_constraints() {
    local date
    date=$(git show -s --format=%cd --date=short HEAD)
    python3 /home/era_constraints.py "$date" /home/constraints.txt         $(cat /home/packages_under_test.txt 2>/dev/null) || return 0
    [ -s /home/constraints.txt ] || return 0

    # Handed to pip as a CONSTRAINT, never installed with -r.
    #
    # A constraints file fixes the version of anything that gets installed without
    # requiring it, which is what is wanted: the closure includes entries that exist only
    # for other platforms - pypiwin32, pathlib2, ipaddress come in through moto's metadata
    # - and installing those on Linux fails outright. As constraints they simply never
    # apply.
    #
    # Every entry is an exact ==, and the closure reaches the transitive dependencies too,
    # so the resolver has nothing left to search. That is what stops the backtracking:
    # before boto3 was pinned, pip walked several hundred of its releases looking for one
    # that accepted a 2020 botocore, at 100% CPU with no output for 13 minutes.
    export PIP_CONSTRAINT=/home/constraints.txt
    return 0
}

# Install, and RESOLVE in-repo dependencies as pip reports them missing.
#
# A package under test can depend on another package in this repo that the test patch never
# touches: #719's ext-sqlite3 requires ext-dbapi==0.8.dev0, which exists nowhere but this
# checkout, so pip looks on PyPI and fails. The error names the distribution; setup.cfg maps
# that name back to a directory; the directory joins the deps layer and the install is
# retried. Two passes settled #719. The alternative - installing every package in the repo -
# is what `eachdist develop` does, and it drags in opentelemetry-ext-celery, whose
# celery~=4.0 pin resolves to wheels with metadata no current resolver parses.
msb_install() {
    local attempt miss dir args predeps
    # Old grpcio (pulled in by 2020-2021 era exporters such as opencensus, otlp and
    # zipkin's grpc paths) ships a Cython .pyx that Cython 3.x cannot compile, and its
    # setup.py fetches the newest Cython into .eggs unless one is already importable. Pin
    # cython<3 up front so that legacy source build succeeds. Harmless for modern PRs:
    # their own packages are pure-Python and modern grpcio installs from an arm64 wheel.
    python -m pip install --no-cache-dir "cython<3" > /tmp/msb_cython.log 2>&1 || true
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
        # Prepass: install every in-repo package code-only (--no-deps), order-independent,
        # so all local .dev versions (e.g. opentelemetry-semantic-conventions==X.dev) are
        # present before any cross-package pin is resolved. Without this, a package whose
        # editable dir is handed to pip after its dependent sends pip to PyPI for a .dev
        # version that was never published, and the install loops until it gives up.
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
        # A pinned version can also be one that cannot be BUILT on this platform. The era
        # closure picks cryptography 2.9.2 for #866, which predates OpenSSL 3.0; on amd64
        # pip finds a prebuilt wheel and never notices, but arm64 has no wheel for that
        # release, so it compiles against bookworm's OpenSSL 3 and gcc fails. Drop the pin
        # and let pip take a version that ships a wheel for this architecture - the parent
        # packages stay pinned, so the rest of the graph is still era-correct.
        nobuild=$(grep -oE 'Could not build wheels for [A-Za-z0-9_.-]+' /tmp/msb_install.log \
                  | tail -1 | awk '{print $NF}' | sed 's/,.*//')
        if [ -n "$nobuild" ] && [ -f /home/constraints.txt ]; then
            echo "install: pass $attempt - $nobuild will not build here, dropping its pin"
            grep -vE "^$nobuild==" /home/constraints.txt > /home/constraints.tmp
            mv /home/constraints.tmp /home/constraints.txt
            continue
        fi

        # A pin from the era closure can contradict a pinned parent: the closure takes
        # the newest release on or before the commit date, but moto 1.3.14 wants idna<2.9
        # while that date's newest idna is 2.10. pip names the offending constraint
        # exactly, so drop it and let the resolver pick that one itself - the parent stays
        # pinned, so the choice stays inside the era.
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

    #819  from google.cloud.trace_v2.proto.trace_pb2 import AttributeValue
          ModuleNotFoundError: No module named 'google.cloud.trace_v2.proto'
          -- google-cloud-trace moved that subpackage after 1.0.

    #866  moto/ec2/models.py: for zone in self.zones[self.region_name]
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

# grpcio is never era-pinned: its 2020-2021 releases have no arm64 wheel and their C
# extension does not compile against bookworm's toolchain (cython 3 rejects cygrpc.pyx;
# older cython gets further but gcc then fails). A modern grpcio installs from an arm64
# wheel and stays API-compatible with the era exporters (opencensus/otlp) that import it.
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
            # in-repo packages are installed from the tree; never constrain them
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
    # protobuf reaches the tests through the in-repo opentelemetry-proto, so setup.cfg
    # never names it and it escapes era pinning. For PRE-3.20 eras this is fatal: pip
    # installs today's protobuf (5.x), whose runtime rejects the *_pb2.py generated by an
    # era protoc, and the zipkin/otlp tests die at import ("Descriptors cannot be created
    # directly"). So seed it ONLY for old commits, where newest_before() pins it below the
    # 3.20 break. Modern PRs (2022+) ship generated code that matches current protobuf and
    # declare their own protobuf floor, so pinning it there only manufactures a version
    # clash -- leave protobuf to the resolver. DATE-adaptive, not a written-down version.
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
    """Shell that writes the helper scripts into /home at image-build time.

    They used to be File() entries, which put five extra files in every PR's image
    directory. The layout that directory is supposed to have is the eight the harness
    defines - the two patches, the four act scripts, check_git_changes.sh and the
    Dockerfile - and anything else makes a PR folder that does not match its siblings.

    Emitting them from prepare.sh keeps the directory clean and puts the scripts in the
    image all the same. Quoted heredoc markers, so nothing inside is expanded on the way
    in: run_tests.sh alone contains ${TARGETS}, $d and $(...) that must survive verbatim.
    """
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
    """Package directories to install, read off THIS PR's own test patch.

    `scripts/eachdist.py develop` was the obvious choice and it is wrong here: it installs
    all forty packages in the repo, including opentelemetry-ext-celery, which pins
    celery~=4.0. Every celery 4.x wheel declares `pytz (>dev)`, metadata no current
    resolver will parse, so the whole install aborts and takes the image with it - #866
    died exactly there. Pinning pip below 24.1 does not help; measured in the base image,
    pip 23.0.1 rejects it too.

    Nothing in this range tests celery. The packages that matter are the ones whose tests
    the patch touches, and the patch says which those are: a test file at
    `<pkg>/tests/...` means `<pkg>` has to be importable. Everything else is noise the
    grading never looks at.
    """
    dirs = set()
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)", pr.test_patch or "", re.M):
        parts = path.split("/")
        if "tests" in parts:
            pkg = "/".join(parts[: parts.index("tests")])
            if pkg:
                dirs.add(pkg)
    return sorted(dirs)


class OpentelemetryPythonImageBase(Image):
    """Shared base image - ONE for the whole range (`base`).

    The previous version of this config was SINGLE-STAGE -- one `ImageDefault`
    with `image_tag() -> pr-<N>` and no base layer at all, so every act rebuilt
    the whole environment. This splits it into the mandated two stages.

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() emits FROM -> apt -> clone -> WORKDIR -> reset ->
    checkout ${BASE_COMMIT} -> extra_setup -> scrub + its four assertions -> CMD,
    and DockerfileEnhancer prepends the BuildKit directive, the ARGs
    (BASE_COMMIT left EMPTY), the env block, labels and cert links.

    The tag carries the PR number because the base's CONTENT is per-PR -- a
    shared `:base` tag would be one name for two different images (FLOW Issue 25).
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

    # base_commit is 2020-03-10 and setup.py declares `python_requires >= 3.4`
    # with CI matrices naming 3.6/3.8. 3.8 is the newest interpreter for which
    # this 2020 dependency stack still resolves. -bookworm (not -slim) because
    # grpcio has no wheel for this pin on arm64 and compiles from source.
    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-bookworm"

    def image_tag(self) -> str:
        # ONE base for every PR in this range, not one each. The tag carries no PR
        # number, so all five instances resolve to the same image_full_name() and the
        # harness builds it once. That is only sound because this base stops at the
        # clone - see dockerfile(). The moment it checked out a commit or installed
        # from the tree it would be PR-specific again.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. build-essential covers
        # grpcio's C extension; nothing further is needed.
        return []

    def dockerfile(self) -> str:
        """The base stops at the clone. Deliberately.

        The harness default (image.py:250-256) would append `git checkout ${BASE_COMMIT}`,
        then extra_setup(), then the hardening block - all facts about ONE pull request.
        That is what forced a `base-pr-<N>` per PR and made the harness build five
        near-identical bases for five PRs.

        Stopping here leaves the base carrying only what every PR in the range shares:
        the interpreter, the apt layer, and the clone. The checkout, the install and the
        history scrub all moved to OpentelemetryPythonImageDefault, which is per-PR
        anyway. Five clones became one.

        The install had to move WITH the checkout, not merely alongside it: `pip install
        -e` installs whatever the working tree currently holds, so it is only correct
        once this PR's commit is checked out. See prepare.sh, where it now lives.

        See BASE_DOCKERFILE for why the infrastructure block is written out by hand
        rather than left to DockerfileEnhancer.
        """
        return BASE_DOCKERFILE % {
            "image": self.dependency(),
            "org": self.pr.org,
            "repo": self.pr.repo,
        }


class OpentelemetryPythonImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history
    scrub already happened in the base."""

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
            # The warm-up is a non-destructive IMPORT PROBE, never a reinstall.
            # A plain `pip install` here could REPLACE the base's EDITABLE
            # install, after which the fix patch's edit to
            # src/opentelemetry/ext/otcollector/metrics_exporter/__init__.py
            # would stop taking effect -- the Python analogue of the `npm ci`
            # that wiped a sibling config's node_modules and scored a silent
            # 0/0/0.
            #
            # `git clean -fdq` is required, not decorative: pytest writes
            # .pytest_cache/ and __pycache__/ into the tree, and the editable
            # installs leave *.egg-info/ and *.egg-link artefacts. Without the
            # clean the next act aborts on a dirty tree (FLOW Issue 4).
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

        # The checkout lives HERE, not in the base, because the base is now shared
        # by every PR in the range and cannot be pinned to any one commit.
        #
        # The hardening block moves with it. It scrubs git history down to the base
        # commit so nothing downstream can read the fix out of `git log`, and it has
        # to run on the image the acts actually execute in. Left in the base it would
        # be scrubbing a tree that had not been checked out yet - i.e. nothing.
        # BASE_COMMIT is substituted literally rather than left as a build ARG, so
        # the block's assertions compare against this PR's sha and no other.
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
        # ANSI first (the previous version omitted this -- audit 4C): a coloured
        # status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # pytest's own output shares the log and its lines END in the same status
        # words, so a whole-log scan would double-count every test AND absorb the
        # trailing progress percentage into the name. The fallback to the whole
        # text keeps a bare sequence of result lines parseable (the config
        # audit's 4C probe has no marker).
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

        # TestResult requires the three sets to be disjoint, else it raises.
        # Resolved toward the CONSERVATIVE outcome -- a name seen both passing
        # and skipping is counted as skipped, never claimed as a pass.
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
