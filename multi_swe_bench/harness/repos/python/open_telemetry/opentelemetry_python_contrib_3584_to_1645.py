import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CORE_URL = "https://github.com/open-telemetry/opentelemetry-python.git"


def _package_dir(pr: PullRequest) -> str:
    paths = re.findall(
        r"^\+\+\+ b/(\S+)",
        (pr.fix_patch or "") + "\n" + (pr.test_patch or ""),
        re.M,
    )
    counts: dict[str, int] = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) > 2 and parts[0] in ("instrumentation", "util", "exporter", "processor", "propagator"):
            counts[parts[0] + "/" + parts[1]] = counts.get(parts[0] + "/" + parts[1], 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda k: counts[k])


_ERA_RESOLVER = r"""
import json
import re
import sys
import urllib.request
from importlib import metadata

CDATE = sys.argv[1]
EXCLUDE = {
    "pip", "setuptools", "wheel", "hatchling", "hatch-vcs", "editables",
    "tox", "virtualenv", "packaging",
}

def first_upload(files):
    times = [f.get("upload_time", "9999") for f in files if isinstance(f, dict)]
    return min(times) if times else None

pins = []
for dist in metadata.distributions():
    name = (dist.metadata["Name"] or "").strip()
    if not name or name.lower() in EXCLUDE:
        continue
    installed = dist.version
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/" + name + "/json",
            headers={"User-Agent": "era-pin"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except Exception:
        continue
    releases = data.get("releases") or {}
    inst_time = first_upload(releases.get(installed) or [])
    if not inst_time or inst_time < CDATE:
        continue
    best = None
    best_time = ""
    for ver, fs in releases.items():
        if not fs or not isinstance(fs, list):
            continue
        if re.search(r"(a|b|c|rc|dev|post)[0-9]", ver, re.I):
            continue
        t = first_upload(fs)
        if not t or t >= CDATE:
            continue
        if t > best_time:
            best, best_time = ver, t
    if best:
        pins.append(name + "==" + best)

print(" ".join(pins))
"""

_INSTRUMENTS_RESOLVER = r"""
import configparser
import os
import sys
import tomllib

reqs = []
for path in sys.argv[1:]:
    if not os.path.isfile(path):
        continue
    if path.endswith(".toml"):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        reqs += data.get("project", {}).get("optional-dependencies", {}).get("instruments", [])
    else:
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        if cfg.has_option("options.extras_require", "instruments"):
            reqs += [
                line.strip()
                for line in cfg.get("options.extras_require", "instruments").splitlines()
                if line.strip()
            ]
for req in reqs:
    print(req)
"""

_RESOLVE_DEPS = r"""
import os, re, sys, configparser

def dist_name(d):
    cfg = os.path.join(d, "setup.cfg")
    if os.path.isfile(cfg):
        c = configparser.ConfigParser()
        try:
            c.read(cfg, encoding="utf-8")
            if c.has_option("metadata", "name"):
                return c.get("metadata", "name").strip()
        except Exception:
            pass
    pt = os.path.join(d, "pyproject.toml")
    if os.path.isfile(pt):
        t = open(pt, encoding="utf-8").read()
        m = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', t)
        if m:
            return m.group(1)
    return None

def deps_of(d):
    out = []
    cfg = os.path.join(d, "setup.cfg")
    if os.path.isfile(cfg):
        c = configparser.ConfigParser()
        try:
            c.read(cfg, encoding="utf-8")
            if c.has_option("options", "install_requires"):
                out += c.get("options", "install_requires").split("\n")
        except Exception:
            pass
    pt = os.path.join(d, "pyproject.toml")
    if os.path.isfile(pt):
        t = open(pt, encoding="utf-8").read()
        m = re.search(r'(?s)dependencies\s*=\s*\[(.*?)\]', t)
        if m:
            out += re.findall(r'"([^"]+)"', m.group(1))
    names = []
    for line in out:
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z0-9._-]+)', line)
        if m:
            names.append(m.group(1).lower().replace("_", "-"))
    return names

root = sys.argv[1]
target = sys.argv[2]
index = {}
for base in ("instrumentation", "util", "exporter", "propagator", "processor", "resource", "sdk-extension", "."):
    b = os.path.join(root, base)
    if not os.path.isdir(b):
        continue
    for e in sorted(os.listdir(b)):
        d = os.path.join(b, e)
        if os.path.isdir(d):
            n = dist_name(d)
            if n:
                index[n.lower().replace("_", "-")] = d

seen, order = set(), []

def walk(d):
    for n in deps_of(d):
        if n in index and n not in seen:
            seen.add(n)
            walk(index[n])
            order.append(index[n])

if os.path.isdir(target):
    walk(target)
for p in order:
    print(os.path.relpath(p, root).replace("\\", "/"))
"""


_TEST_BODY = r"""
VENV=$(cat /home/venv_path)
PY="${VENV}/bin/python"

if [ -d "/home/[[REPO]]/[[PKGDIR]]" ] && { [ -f "/home/[[REPO]]/[[PKGDIR]]/pyproject.toml" ] || [ -f "/home/[[REPO]]/[[PKGDIR]]/setup.py" ]; }; then
    "${PY}" -m pip install --no-cache-dir -q --no-deps -e "/home/[[REPO]]/[[PKGDIR]]"
    "${PY}" /home/instruments.py "/home/[[REPO]]/[[PKGDIR]]/pyproject.toml" "/home/[[REPO]]/[[PKGDIR]]/setup.cfg" > /home/stage_instruments.txt
    if [ -s /home/stage_instruments.txt ]; then
        "${PY}" -m pip install --no-cache-dir -q -r /home/stage_instruments.txt
    fi
fi

set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*\.py|[^/]*_test\.py)$' \
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    [ -f "$f" ] && TEST_TARGETS="$TEST_TARGETS $f"
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

set +e
TESTS_DIR=""
for f in $TEST_TARGETS; do
    D=$(dirname "$f")
    if [ -z "$TESTS_DIR" ]; then
        TESTS_DIR="$D"
    elif [ "$TESTS_DIR" != "$D" ]; then
        TESTS_DIR=""
        break
    fi
done

if [ -n "$TESTS_DIR" ] && [ "$TESTS_DIR" != "." ]; then
    cd "$TESTS_DIR"
    BASE_TARGETS=""
    for f in $TEST_TARGETS; do
        BASE_TARGETS="$BASE_TARGETS $(basename "$f")"
    done
    TEST_TARGETS="$BASE_TARGETS"
fi

set +e
echo "running pytest in ${TESTS_DIR:-.} on:$TEST_TARGETS"
"${PY}" -m pytest -v -rA --no-header --tb=short -p no:cacheprovider \
    --continue-on-collection-errors $TEST_TARGETS 2>&1
"""


class OtelContribEraImageBase(Image):
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
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        return "base-3584_to_1645"

    def workdir(self) -> str:
        return "base-3584_to_1645"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    build-essential \\
    pkg-config \\
    libssl-dev \\
    libffi-dev \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class OtelContribEraImageDefault(Image):
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
        return OtelContribEraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        pkgdir = _package_dir(self.pr)

        prepare = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
for i in 1 2 3; do
    git fetch --no-tags --depth=1 origin [[SHA]] 2>/dev/null && break
    git fetch --no-tags origin 2>/dev/null && break
    sleep 5
done
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

cat > /home/resolve_deps.py <<'RESOLVEEOF'
[[RESOLVER]]
RESOLVEEOF

cat > /home/era_pin.py <<'ERAEEOF'
[[ERARESOLVER]]
ERAEEOF

cat > /home/instruments.py <<'INSTREOF'
[[INSTRRESOLVER]]
INSTREOF

CORE_SHA=$(grep -rhoE 'CORE_REPO_SHA: *[0-9a-f]{40}' .github/workflows/ 2>/dev/null \
    | head -1 | grep -oE '[0-9a-f]{40}' || true)
for i in 1 2 3 4 5; do
    git clone --quiet [[COREURL]] /home/otel-core && break
    rm -rf /home/otel-core
    sleep 5
done
test -d /home/otel-core
CDATE=$(git show -s --format=%cI HEAD)
if [ -z "${CORE_SHA}" ]; then
    CORE_SHA=$(git -C /home/otel-core rev-list -n1 --before="${CDATE}" origin/main)
fi
echo "core repo sha pinned for this commit: ${CORE_SHA}"
if ! git -C /home/otel-core checkout --quiet --detach "${CORE_SHA}"; then
    if git -C /home/otel-core fetch --quiet origin "${CORE_SHA}"; then
        git -C /home/otel-core checkout --quiet --detach "${CORE_SHA}"
    else
        CORE_SHA=$(git -C /home/otel-core rev-list -n1 --before="${CDATE}" origin/main)
        git -C /home/otel-core checkout --quiet --detach "${CORE_SHA}"
    fi
fi
test "$(git -C /home/otel-core rev-parse HEAD)" = "${CORE_SHA}"

pip install --no-cache-dir -q "tox<5"

FACTOR=$(grep -E '[[PKGDIR]]/tests' tox.ini 2>/dev/null | head -1 \
    | sed -e 's/:.*//' -e 's/^[[:space:]]*//' -e 's/{.*//')
echo "tox factor for [[PKGDIR]]: ${FACTOR:-<none>}"

ENVN=""
if [ -n "${FACTOR}" ]; then
    ENVN=$(tox -l 2>/dev/null | grep -E "^py311-.*${FACTOR}" | tail -1 || true)
fi
echo "tox env: ${ENVN:-<none, using plain venv>}"

VENV=""
if [ -n "${ENVN}" ]; then
    if tox --workdir /home/.tox -e "${ENVN}" --notest; then
        VENV=/home/.tox/${ENVN}
    fi
fi
if [ -z "${VENV}" ]; then
    python -m venv /home/.tox/fallback
    VENV=/home/.tox/fallback
    "${VENV}/bin/python" -m pip install --no-cache-dir -q -U pip "setuptools<80" wheel hatchling hatch-vcs editables pytest
fi

PY="${VENV}/bin/python"
"${PY}" -m pip install --no-cache-dir -q -U pip "setuptools<80" wheel hatchling hatch-vcs editables
"${PY}" -m pip install --no-cache-dir -q pytest

for SUB in opentelemetry-api opentelemetry-semantic-conventions opentelemetry-sdk tests/opentelemetry-test-utils; do
    "${PY}" -m pip install --no-cache-dir -q "/home/otel-core/${SUB}"
done

if [ -d "/home/[[REPO]]/opentelemetry-instrumentation" ]; then
    "${PY}" -m pip install --no-cache-dir -q --no-deps --no-build-isolation -e "/home/[[REPO]]/opentelemetry-instrumentation"
fi


DEPS=$("${PY}" /home/resolve_deps.py /home/[[REPO]] /home/[[REPO]]/[[PKGDIR]])
echo "in-repo dependencies: ${DEPS:-<none>}"
for D in ${DEPS}; do
    "${PY}" -m pip install --no-cache-dir -q --no-deps -e "/home/[[REPO]]/${D}"
done

if [ -d "/home/[[REPO]]/[[PKGDIR]]" ]; then
    "${PY}" -m pip install --no-cache-dir -q --no-deps -e "/home/[[REPO]]/[[PKGDIR]]"
fi

"${PY}" /home/instruments.py "/home/[[REPO]]/[[PKGDIR]]/pyproject.toml" "/home/[[REPO]]/[[PKGDIR]]/setup.cfg" > /home/instruments.txt
if [ -s /home/instruments.txt ]; then
    "${PY}" -m pip install --no-cache-dir -q -r /home/instruments.txt
    echo "instrument extras: $(tr "\n" " " < /home/instruments.txt)"
else
    echo "instrument extras: none declared"
fi

TESTPKG="/home/[[REPO]]/[[PKGDIR]]"
if [ -n "[[PKGDIR]]" ] && [ -d "${TESTPKG}" ]; then
    "${PY}" -m pip install --no-cache-dir -q --no-build-isolation "${TESTPKG}[test]" || echo "test extra: install failed, continuing"
    TRD=""
    SUFF=""
    case "${ENVN}" in
        *-[0-9]) SUFF="${ENVN##*-}";;
    esac
    if [ -n "${SUFF}" ] && [ -f "${TESTPKG}/test-requirements-${SUFF}.txt" ]; then
        TRD="${TESTPKG}/test-requirements-${SUFF}.txt"
    elif [ -f "${TESTPKG}/test-requirements-1.txt" ]; then
        TRD="${TESTPKG}/test-requirements-1.txt"
    elif [ -f "${TESTPKG}/test-requirements-0.txt" ]; then
        TRD="${TESTPKG}/test-requirements-0.txt"
    elif [ -f "${TESTPKG}/test-requirements.txt" ]; then
        TRD="${TESTPKG}/test-requirements.txt"
    fi
    if [ -n "${TRD}" ]; then
        echo "installing ${TRD}"
        (cd /home/[[REPO]] && "${PY}" -m pip install -q -r "${TRD}") || echo "test requirements: install failed, continuing"
    fi
    "${PY}" -m pip install --no-cache-dir -q --no-deps -e "${TESTPKG}"
fi
if [ -d "/home/[[REPO]]/util/opentelemetry-util-http" ]; then
    "${PY}" -m pip install --no-cache-dir -q --no-deps -e "/home/[[REPO]]/util/opentelemetry-util-http"
fi
for SUB in opentelemetry-api opentelemetry-semantic-conventions opentelemetry-sdk tests/opentelemetry-test-utils; do
    "${PY}" -m pip install --no-cache-dir -q "/home/otel-core/${SUB}"
done

PINS=$("${PY}" /home/era_pin.py "${CDATE}")
echo "era downgrades: ${PINS:-<none>}"
if [ -n "${PINS}" ]; then
    "${PY}" -m pip install --no-cache-dir -q ${PINS}
fi

echo "${VENV}" > /home/venv_path

test -x "${PY}"
"${PY}" -c "import opentelemetry.sdk, opentelemetry.test, opentelemetry.trace; print('CORE_OK')"
"${PY}" -m pytest --version
"""

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                prepare.replace("[[RESOLVER]]", _RESOLVE_DEPS.strip("\n"))
                .replace("[[ERARESOLVER]]", _ERA_RESOLVER.strip("\n"))
                .replace("[[INSTRRESOLVER]]", _INSTRUMENTS_RESOLVER.strip("\n"))
                .replace("[[COREURL]]", _CORE_URL)
                .replace("[[PKGDIR]]", pkgdir)
                .replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
"""
                 + _TEST_BODY).replace("[[PKGDIR]]", pkgdir).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
"""
                 + _TEST_BODY).replace("[[PKGDIR]]", pkgdir).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
"""
                 + _TEST_BODY).replace("[[PKGDIR]]", pkgdir).replace("[[REPO]]", repo),
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

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("open-telemetry", "opentelemetry_python_contrib_3584_to_1645")
class OpentelemetryPythonContrib3584To1645(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OtelContribEraImageDefault(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        inline = re.compile(
            r"^(?P<name>\S+::\S+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>\S+::\S+)"
        )
        collection_error = re.compile(r"^ERROR\s+(?P<name>\S+\.py)$")
        collecting_error = re.compile(r"ERROR collecting\s+(?P<name>\S+\.py)")

        for line in clean.split("\n"):
            stripped = line.strip()
            m = inline.match(stripped) or summary.match(stripped)
            if not m:
                c = collection_error.match(stripped) or collecting_error.search(stripped)
                if c:
                    failed_tests.add(c.group("name"))
                continue
            name = m.group("name").strip()
            status = m.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
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
