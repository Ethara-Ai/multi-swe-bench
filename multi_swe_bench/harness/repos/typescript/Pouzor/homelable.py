import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.13.18-slim-bookworm"
_PY_VERSION = "3.13.18"
_NODE_VERSION = "20.20.2"
_NODE_SHA256_X64 = "df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d"
_NODE_SHA256_ARM64 = "73093db209e4e9e09dd7d15a47aeaab1b74833830df03efa5f942a1122c5fa71"
_UV_VERSION = "0.13.5"
_BASE_TAG = "base-178_to_586"
_MCP_PYTHON = "/home/homelable/mcp/.venv/bin/python"

_FRONTEND_RE = re.compile(
    r"^\+\+\+ b/frontend/(src/\S+\.(?:test|spec)\.[cm]?[jt]sx?)$", re.MULTILINE
)
_BACKEND_RE = re.compile(
    r"^\+\+\+ b/backend/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$", re.MULTILINE
)
_MCP_RE = re.compile(
    r"^\+\+\+ b/mcp/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$", re.MULTILINE
)


def _files(pattern: re.Pattern, pr: PullRequest) -> list[str]:
    return sorted(set(pattern.findall(pr.test_patch or "")))


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__SHA__"
git checkout --detach __SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"

[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
[ "$(python3 -c 'import platform; print(platform.python_version())')" = "__PY_VERSION__" ] || { echo "prepare.sh: Python __PY_VERSION__ is not active"; exit 1; }

cd /home/__REPO__/frontend
for attempt in 1 2 3; do
    npm ci && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: npm ci failed 3 times"; exit 1; }
    sleep 30
done
test -x node_modules/.bin/vitest || { echo "prepare.sh: vitest is not installed"; exit 1; }

cd /home/__REPO__
cutoff="$(git show -s --format=%cI HEAD)"
uv pip install --python /usr/local/bin/python3 --exclude-newer "$cutoff" -r backend/requirements.txt
uv pip check --python /usr/local/bin/python3
python3 -m pip check

uv venv --python /usr/local/bin/python3 mcp/.venv
uv pip install --python __MCP_PYTHON__ --exclude-newer "$cutoff" -r mcp/requirements.txt pytest
uv pip check --python __MCP_PYTHON__
uv cache clean

cd /home/__REPO__/backend
backend=()
for f in __BACKEND_FILES__; do
    if [ -f "$f" ]; then
        backend+=("$f")
    fi
done
if [ ${#backend[@]} -gt 0 ]; then
    python3 -m pytest -p no:cacheprovider --collect-only -q "${backend[@]}" > /tmp/mswb_collect_backend.log
    tail -n 1 /tmp/mswb_collect_backend.log
fi
python3 -c "import fastapi, sqlalchemy, pydantic, pytest, pytest_asyncio"

cd /home/__REPO__/mcp
mcp=()
for f in __MCP_FILES__; do
    if [ -f "$f" ]; then
        mcp+=("$f")
    fi
done
if [ ${#mcp[@]} -gt 0 ]; then
    __MCP_PYTHON__ -m pytest -p no:cacheprovider --collect-only -q "${mcp[@]}" > /tmp/mswb_collect_mcp.log
    tail -n 1 /tmp/mswb_collect_mcp.log
fi
__MCP_PYTHON__ -c "import pytest, mcp"

cd /home/__REPO__/frontend
frontend=()
for f in __FRONTEND_FILES__; do
    if [ -f "$f" ]; then
        frontend+=("$f")
    fi
done
if [ ${#frontend[@]} -gt 0 ]; then
    node_modules/.bin/vitest list --filesOnly "${frontend[@]}" > /tmp/mswb_collect_frontend.log
    tail -n 1 /tmp/mswb_collect_frontend.log
fi
node -e "require('./node_modules/vitest/package.json'); require('./node_modules/jsdom/package.json')"
echo "gate: vitest, the backend packages, the mcp venv and the PR test files load"
"""

_PYTEST_SH = r"""#!/bin/bash
set -uo pipefail

dir="$1"
python="$2"
shift 2
cd "/home/__REPO__/$dir"
report="/tmp/mswb_pytest_${dir}.xml"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $dir/$f is not in the tree yet"
    fi
done
rm -f "$report"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the $dir test files exist yet"
    exit 0
fi

rc=0
"$python" -m pytest -p no:cacheprovider -o junit_family=xunit1 \
    --continue-on-collection-errors \
    --junitxml="$report" -rfE --tb=short "${files[@]}" < /dev/null || rc=$?
echo "mswb: $dir pytest exit $rc"
python3 /home/mswb_emit.py "$report" "$dir" || exit 1

case "$rc" in
    0|1|2|5) ;;
    *) echo "=== MSB: $dir pytest exited with status $rc ==="; exit "$rc" ;;
esac
if [ ! -f "$report" ] && [ "$rc" -ne 5 ]; then
    echo "=== MSB: $dir pytest wrote no report (status $rc) ==="
    exit 1
fi
exit 0
"""

_EMIT_PY = r"""import os
import sys
import xml.etree.ElementTree as ET

path, prefix = sys.argv[1], sys.argv[2]
if not os.path.exists(path):
    print("mswb: no junit report was written for %s" % prefix)
    raise SystemExit(0)
rank = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
status = {}
for case in ET.parse(path).getroot().iter("testcase"):
    file = case.get("file") or ""
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    if not file.endswith(".py"):
        print("mswb: testcase without a file: %s %s" % (classname, name))
        continue
    module = file[:-3].replace("/", ".")
    if not classname and name == module:
        nodeid = file
    else:
        parts = [file]
        if classname.startswith(module + "."):
            parts.extend(classname[len(module) + 1:].split("."))
        parts.append(name)
        nodeid = "::".join(parts)
    kids = {child.tag for child in case}
    if "failure" in kids or "error" in kids:
        st = "FAILED"
    elif "skipped" in kids:
        st = "SKIPPED"
    else:
        st = "PASSED"
    if nodeid not in status or rank[st] > rank[status[nodeid]]:
        status[nodeid] = st
for nodeid in sorted(status):
    print("pytest::%s/%s %s" % (prefix, nodeid, status[nodeid]))
"""

_VITEST_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__/frontend
out=/tmp/mswb_vitest.json
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: frontend/$f is not in the tree yet"
    fi
done
rm -f "$out"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the frontend test files exist yet"
    exit 0
fi

rc=0
node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
echo "mswb: vitest exit $rc"
node /home/mswb_digest.js "$out" || exit 1
if [ "$rc" -gt 1 ]; then
    echo "=== MSB: vitest exited with status $rc ==="
    exit "$rc"
fi
if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
    echo "=== MSB: vitest wrote no report (status $rc) ==="
    exit 1
fi
exit 0
"""

_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const out = process.argv[2];
if (!fs.existsSync(out)) {
  console.log('mswb: no vitest report was written');
  process.exit(0);
}
const root = '/home/__REPO__';
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const report = JSON.parse(fs.readFileSync(out, 'utf8'));
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map((x) => String(x).replace(/\s+/g, ' ').trim()).join(' > ');
    const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    const key = 'vitest::' + rel + '::' + title;
    if (!status.has(key) || rank[st] > rank[status.get(key)]) {
      status.set(key, st);
    }
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('mswb: suite error in ' + rel + ': ' + String(file.message || '').split('\n')[0]);
  }
}
for (const key of [...status.keys()].sort()) {
  console.log(key + ' ' + status.get(key));
}
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class HomelableImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates curl xz-utils nmap iputils-ping \\
    && rm -rf /var/lib/apt/lists/*

RUN [ "$(python3 -c 'import platform; print(platform.python_version())')" = "{_PY_VERSION}" ] || {{ echo "python {_PY_VERSION} missing"; exit 1; }}

RUN set -eux; \\
    target="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "${{target}}" in \\
      amd64) arch=x64; sha={_NODE_SHA256_X64} ;; \\
      arm64) arch=arm64; sha={_NODE_SHA256_ARM64} ;; \\
      *) echo "unsupported arch ${{target}}"; exit 1 ;; \\
    esac; \\
    curl -fsSL --retry 5 -o /tmp/node.tar.xz "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{arch}}.tar.xz"; \\
    echo "${{sha}}  /tmp/node.tar.xz" | sha256sum -c -; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \\
    rm -f /tmp/node.tar.xz; \\
    [ "$(node -v)" = "v{_NODE_VERSION}" ]

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}" && \\
    [ "$(uv --version | cut -d' ' -f2)" = "{_UV_VERSION}" ]

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class HomelableImageDefault(Image):
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
        return HomelableImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__NODE_VERSION__": _NODE_VERSION,
            "__PY_VERSION__": _PY_VERSION,
            "__MCP_PYTHON__": _MCP_PYTHON,
            "__FRONTEND_FILES__": " ".join(_files(_FRONTEND_RE, self.pr)),
            "__BACKEND_FILES__": " ".join(_files(_BACKEND_RE, self.pr)),
            "__MCP_FILES__": " ".join(_files(_MCP_RE, self.pr)),
        }

    def _test_cmds(self) -> str:
        lines = [f"cd /home/{self.pr.repo}"]
        frontend = _files(_FRONTEND_RE, self.pr)
        backend = _files(_BACKEND_RE, self.pr)
        mcp = _files(_MCP_RE, self.pr)
        if frontend:
            lines.append(f"bash /home/mswb_vitest.sh {' '.join(frontend)}")
        if backend:
            lines.append(f"bash /home/mswb_pytest.sh backend python3 {' '.join(backend)}")
        if mcp:
            lines.append(f"bash /home/mswb_pytest.sh mcp {_MCP_PYTHON} {' '.join(mcp)}")
        return "\n".join(lines) + "\n"

    def _act(self, name: str, *patches: str) -> File:
        apply = ""
        if patches:
            apply = (
                f"cd /home/{self.pr.repo}\n"
                f"git apply --whitespace=nowarn {' '.join(patches)} || "
                f'{{ echo "{name}: git apply failed" >&2; exit 1; }}\n'
            )
        return File(".", name, f"{_RUN_HEADER}\n\n{apply}{self._test_cmds()}")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_emit.py", _EMIT_PY),
            File(".", "mswb_pytest.sh", _render(_PYTEST_SH, values)),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_vitest.sh", _render(_VITEST_SH, values)),
            File(".", "prepare.sh", _render(_PREPARE_SH, values)),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    HUSKY=0 \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    PIP_RETRIES=10 \\
    PIP_DEFAULT_TIMEOUT=120 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>(?:pytest::(?:backend|mcp)/tests/\S.*?\.py|vitest::frontend/src/\S.*?\.[cm]?[jt]sx?)(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("Pouzor", "homelable")
class HOMELABLE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HomelableImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
