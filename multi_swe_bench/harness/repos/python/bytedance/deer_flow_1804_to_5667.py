import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12.16-slim-bookworm"
_PY_VERSION = "3.12.16"
_UV_VERSION = "0.13.8"
_NODE_VERSION = "22.23.5"
_NODE_SHA256_X64 = "cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8"
_NODE_SHA256_ARM64 = "2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09"
_PNPM_VERSION = "10.26.2"
_INTERVAL = "deer_flow_1804_to_5667"
_BASE_TAG = "base-1804_to_5667"

_PY_TEST_RE = re.compile(
    r"^\+\+\+ b/backend/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$",
    re.MULTILINE,
)
_JS_TEST_RE = re.compile(
    r"^\+\+\+ b/frontend/(tests/unit/\S+\.test\.[cm]?[jt]sx?)$",
    re.MULTILINE,
)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _py_files(pr: PullRequest) -> list[str]:
    return sorted(set(_PY_TEST_RE.findall(pr.test_patch or "")))


def _main_files(pr: PullRequest) -> list[str]:
    return [f for f in _py_files(pr) if not f.startswith("tests/blocking_io/")]


def _blocking_files(pr: PullRequest) -> list[str]:
    return [f for f in _py_files(pr) if f.startswith("tests/blocking_io/")]


def _js_files(pr: PullRequest) -> list[str]:
    return sorted(set(_JS_TEST_RE.findall(pr.test_patch or "")))


def _text_only(patch: str) -> str:
    parts = _DIFF_SPLIT_RE.split(patch or "")
    kept = [
        p
        for p in parts
        if p and not re.search(r"^(?:Binary files .* differ|GIT binary patch)$", p, re.MULTILINE)
    ]
    return "".join(kept)


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

[ "$(python3 -c 'import platform; print(platform.python_version())')" = "__PY_VERSION__" ] || { echo "prepare.sh: Python __PY_VERSION__ is not active"; exit 1; }
[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
[ "$(pnpm --version)" = "__PNPM_VERSION__" ] || { echo "prepare.sh: pnpm __PNPM_VERSION__ is not active"; exit 1; }
[ "$(uv --version | cut -d' ' -f2)" = "__UV_VERSION__" ] || { echo "prepare.sh: uv __UV_VERSION__ is not active"; exit 1; }

cd /home/__REPO__/backend
for attempt in 1 2 3; do
    uv sync --frozen --group dev --python /usr/local/bin/python3 && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: uv sync failed 3 times"; exit 1; }
    sleep 30
done
uv cache clean
__JS__
cd /home/__REPO__/backend
py_files=()
for f in __PY_FILES__; do
    if [ -f "$f" ]; then
        py_files+=("$f")
    fi
done
if [ ${#py_files[@]} -gt 0 ]; then
    PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider --rootdir=/home/__REPO__/backend --collect-only -q "${py_files[@]}" > /tmp/mswb_collect_py.log
    tail -n 1 /tmp/mswb_collect_py.log
fi
__JS_GATE__
PYTHONPATH=. .venv/bin/python -c "import deerflow, pytest"
echo "gate: locked backend deps, Python __PY_VERSION__, deerflow and the PR test files load"
"""

_JS_SNIPPET = r"""cd /home/__REPO__/frontend
for attempt in 1 2 3; do
    pnpm install --frozen-lockfile && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
    sleep 30
done
if [ -f rstest.config.ts ]; then
    test -x node_modules/.bin/rstest || { echo "prepare.sh: rstest is not installed in frontend"; exit 1; }
else
    test -x node_modules/.bin/vitest || { echo "prepare.sh: vitest is not installed in frontend"; exit 1; }
fi
pnpm store prune
"""

_JS_GATE_SNIPPET = r"""cd /home/__REPO__/frontend
js_files=()
for f in __JS_FILES__; do
    if [ -f "$f" ]; then
        js_files+=("$f")
    fi
done
if [ ${#js_files[@]} -gt 0 ]; then
    if [ -f rstest.config.ts ]; then
        node_modules/.bin/rstest list --filesOnly "${js_files[@]}" > /tmp/mswb_collect_js.log
    else
        node_modules/.bin/vitest list --filesOnly "${js_files[@]}" > /tmp/mswb_collect_js.log
    fi
    tail -n 1 /tmp/mswb_collect_js.log
fi
cd /home/__REPO__/backend
"""

_PYTEST_SH = r"""#!/bin/bash
set -uo pipefail

group="$1"
shift
cd /home/__REPO__/backend
report="/tmp/mswb_pytest_${group}.xml"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: backend/$f is not in the tree yet"
    fi
done
rm -f "$report"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the $group test files exist yet"
    exit 0
fi

marker=()
if [ "$group" = "main" ]; then
    marker=(-m "not live")
fi
rc=0
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv/bin/python -m pytest -p no:cacheprovider \
    --rootdir=/home/__REPO__/backend -o junit_family=xunit1 "${marker[@]}" \
    --continue-on-collection-errors \
    --junitxml="$report" -rfE --tb=short "${files[@]}" < /dev/null || rc=$?
echo "mswb: pytest $group exit $rc"
.venv/bin/python /home/mswb_emit.py "$report" backend/ || exit 1

case "$rc" in
    0|1|2|5) ;;
    *) echo "=== MSB: pytest $group exited with status $rc ==="; exit "$rc" ;;
esac
if [ ! -f "$report" ] && [ "$rc" -ne 5 ]; then
    echo "=== MSB: pytest $group wrote no report (status $rc) ==="
    exit 1
fi
exit 0
"""

_EMIT_PY = r"""import os
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
prefix = sys.argv[2]
if not os.path.exists(path):
    print("mswb: no junit report was written")
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
        nodeid = prefix + file
    else:
        parts = [prefix + file]
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
    print("pytest::%s %s" % (nodeid, status[nodeid]))
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
tool=vitest
if [ -f rstest.config.ts ]; then
    tool=rstest
    node_modules/.bin/rstest run --reporter json "${files[@]}" > "$out" < /dev/null || rc=$?
else
    node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
fi
echo "mswb: $tool exit $rc"
node /home/mswb_digest.js "$out" || exit 1
if [ "$rc" -gt 1 ]; then
    echo "=== MSB: $tool exited with status $rc ==="
    exit "$rc"
fi
if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
    echo "=== MSB: $tool wrote no report (status $rc) ==="
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
const raw = fs.readFileSync(out, 'utf8');
const start = raw.indexOf('{\n  "tool": "rstest"');
let report;
if (start >= 0) {
  const end = raw.indexOf('\n}\n', start);
  report = JSON.parse(raw.slice(start, end < 0 ? raw.length : end + 2));
} else if (raw.trim() === '') {
  console.log('mswb: the test report is empty');
  process.exit(1);
} else {
  report = JSON.parse(raw);
}
const clean = (x) => String(x).replace(/\s+/g, ' ').trim();
const put = (key, st) => {
  if (!status.has(key) || rank[st] > rank[status.get(key)]) {
    status.set(key, st);
  }
};
if (report.tool === 'rstest') {
  const front = path.join(root, 'frontend');
  for (const file of report.files || []) {
    const rel = path.relative(root, path.resolve(front, file.testPath));
    const results = file.results || [];
    for (const t of results) {
      const title = [...(t.parentNames || []), t.name].map(clean).join(' > ');
      const st = t.status === 'pass' ? 'PASSED' : t.status === 'fail' ? 'FAILED' : 'SKIPPED';
      put('rstest::' + rel + '::' + title, st);
    }
    if (results.length === 0 && file.status === 'fail') {
      const err = (file.errors || [])[0] || {};
      console.log('mswb: suite error in ' + rel + ': ' + String(err.message || '').split('\n')[0]);
    }
  }
} else {
  for (const file of report.testResults || []) {
    const rel = path.relative(root, file.name);
    const results = file.assertionResults || [];
    for (const t of results) {
      const title = [...(t.ancestorTitles || []), t.title].map(clean).join(' > ');
      const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
      put('vitest::' + rel + '::' + title, st);
    }
    if (results.length === 0 && file.status === 'failed') {
      console.log('mswb: suite error in ' + rel + ': ' + String(file.message || '').split('\n')[0]);
    }
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


class DeerFlowImageBase(Image):
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
    git build-essential ca-certificates curl xz-utils \\
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

RUN npm install -g --no-fund --no-audit "pnpm@{_PNPM_VERSION}" && \\
    [ "$(pnpm --version)" = "{_PNPM_VERSION}" ]

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}" && \\
    [ "$(uv --version | cut -d' ' -f2)" = "{_UV_VERSION}" ]

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class DeerFlowImageDefault(Image):
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
        return DeerFlowImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__PY_VERSION__": _PY_VERSION,
            "__NODE_VERSION__": _NODE_VERSION,
            "__PNPM_VERSION__": _PNPM_VERSION,
            "__UV_VERSION__": _UV_VERSION,
            "__PY_FILES__": " ".join(_py_files(self.pr)),
            "__JS_FILES__": " ".join(_js_files(self.pr)),
        }

    def _prepare(self) -> str:
        has_js = bool(_js_files(self.pr))
        text = _render(
            _PREPARE_SH,
            {
                "__JS__": _JS_SNIPPET if has_js else "",
                "__JS_GATE__": _JS_GATE_SNIPPET if has_js else "",
            },
        )
        return _render(text, self._values())

    def _test_cmds(self) -> str:
        lines = [f"cd /home/{self.pr.repo}"]
        main = _main_files(self.pr)
        if main:
            lines.append(f"bash /home/mswb_pytest.sh main {' '.join(main)}")
        blocking = _blocking_files(self.pr)
        if blocking:
            lines.append(f"bash /home/mswb_pytest.sh blocking_io {' '.join(blocking)}")
        js = _js_files(self.pr)
        if js:
            lines.append(f"bash /home/mswb_vitest.sh {' '.join(js)}")
        return "\n".join(lines) + "\n"

    def _act(self, name: str, *patches: str) -> File:
        apply = f"cd /home/{self.pr.repo}\ngit update-index -q --refresh\n"
        if patches:
            apply += (
                f"git apply --3way --whitespace=nowarn {' '.join(patches)} || "
                f'{{ echo "{name}: git apply failed" >&2; exit 1; }}\n'
            )
        return File(".", name, f"{_RUN_HEADER}\n\n{apply}{self._test_cmds()}")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_emit.py", _EMIT_PY),
            File(".", "mswb_pytest.sh", _render(_PYTEST_SH, values)),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_vitest.sh", _render(_VITEST_SH, values)),
            File(".", "prepare.sh", self._prepare()),
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
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_HTTP_TIMEOUT=300

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>(?:pytest::backend/tests/\S.*?\.py|(?:vitest|rstest)::frontend/tests/\S.*?\.[cm]?[jt]sx?)(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("bytedance", "deer-flow")
@Instance.register("bytedance", _INTERVAL)
class DEER_FLOW(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DeerFlowImageDefault(self.pr, self._config)

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
