import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12.16-slim-bookworm"
_PY_VERSION = "3.12.16"
_UV_VERSION = "0.13.8"
_PYYAML_VERSION = "6.0.3"
_NODE_VERSION = "22.23.5"
_NODE_SHA256_X64 = "cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8"
_NODE_SHA256_ARM64 = "2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09"
_INTERVAL = "gaia_5720_to_8587"
_BASE_TAG = "base-5720_to_8587"

_WEBUI_DIR = "src/gaia/apps/webui"
_ELECTRON_DIR = "tests/electron"

_PY_TEST_RE = re.compile(
    r"^\+\+\+ b/((?:[^\s/]+/)*tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$",
    re.MULTILINE,
)
_WEBUI_TEST_RE = re.compile(
    r"^\+\+\+ b/src/gaia/apps/webui/(src/(?:[^\s/]+/)*[^\s/]+\.(?:test|spec)\.tsx?)$",
    re.MULTILINE,
)
_ELECTRON_TEST_RE = re.compile(
    r"^\+\+\+ b/tests/electron/((?:[^\s/]+/)*(?:[^\s/]+\.test\.c?js|test_[^\s/]+\.js))$",
    re.MULTILINE,
)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _py_files(pr: PullRequest) -> list[str]:
    return sorted(set(_PY_TEST_RE.findall(pr.test_patch or "")))


def _webui_files(pr: PullRequest) -> list[str]:
    return sorted(set(_WEBUI_TEST_RE.findall(pr.test_patch or "")))


def _electron_files(pr: PullRequest) -> list[str]:
    return sorted(set(_ELECTRON_TEST_RE.findall(pr.test_patch or "")))


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

_CI_PY = r"""import fnmatch
import json
import os
import re
import shlex
import sys

import yaml

root = "/home/__REPO__"
files = sys.argv[1:]
wfdir = os.path.join(root, ".github", "workflows")
stops = {"&&", "||", "|", ";"}
prefix = "uv pip install --system "


def ubuntu(job):
    text = json.dumps(job.get("runs-on", ""))
    if "ubuntu" in text:
        return True
    return "matrix" in text and "ubuntu" in json.dumps(job.get("strategy") or {})


def lines(run):
    out = []
    for line in str(run or "").replace("\\\n", " ").split("\n"):
        line = " ".join(line.split())
        if line and not line.startswith("#"):
            out.append(line)
    return out


def targets(job):
    found = set()
    for step in job.get("steps") or []:
        for line in lines(step.get("run")):
            if "pip install" in line:
                continue
            m = re.search(r"\bpytest\b(.*)", line)
            if not m:
                continue
            for tok in m.group(1).split():
                tok = tok.split("::")[0].strip("\"'")
                if tok.startswith("-"):
                    continue
                if "/" in tok or tok.endswith(".py"):
                    found.add(tok.rstrip("/"))
    return found


def covers(found):
    for f in files:
        for t in found:
            if f == t or f.startswith(t + "/") or fnmatch.fnmatch(f, t):
                return True
    return False


def installs(job):
    out = []
    for step in job.get("steps") or []:
        for line in lines(step.get("run")):
            if not line.startswith(prefix) or "${{" in line:
                continue
            args = []
            for tok in shlex.split(line[len(prefix):]):
                if tok in stops or tok.startswith((">", "2>")):
                    break
                args.append(tok)
            if args:
                out.append(args)
    return out


def envs(workflow, job):
    out = {}
    for scope in (workflow.get("env"), job.get("env")):
        out.update(scope or {})
    for step in job.get("steps") or []:
        if "pytest" in str(step.get("run") or ""):
            out.update(step.get("env") or {})
    return {k: str(v) for k, v in out.items() if "${{" not in str(v)}


unit = None
jobs = []
for name in sorted(os.listdir(wfdir)):
    if not name.endswith((".yml", ".yaml")):
        continue
    try:
        with open(os.path.join(wfdir, name), encoding="utf-8") as fh:
            workflow = yaml.safe_load(fh) or {}
    except yaml.YAMLError as err:
        print("mswb_ci: cannot parse %s: %s" % (name, str(err).splitlines()[0]))
        continue
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict) or not ubuntu(job):
            continue
        key = "%s:%s" % (name, job_name)
        if name == "test_unit.yml" and job_name == "unit-tests":
            unit = (key, workflow, job)
        elif covers(targets(job)):
            jobs.append((key, workflow, job))
if unit is None:
    print("mswb_ci: test_unit.yml has no Linux unit-tests job")
    raise SystemExit(1)

seen = []
env = {}
for key, workflow, job in [unit] + jobs:
    print("mswb_ci: job %s" % key)
    for args in installs(job):
        if args not in seen:
            seen.append(args)
    for k, v in envs(workflow, job).items():
        env.setdefault(k, v)
if not seen:
    print("mswb_ci: no install lines found")
    raise SystemExit(1)
with open("/tmp/mswb_ci_install.sh", "w", encoding="utf-8") as fh:
    for args in seen:
        line = "ci_install " + " ".join(shlex.quote(a) for a in args)
        print("mswb_ci: " + line)
        fh.write(line + "\n")
with open("/home/mswb_ci.env", "w", encoding="utf-8") as fh:
    for k in sorted(env):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            line = "export %s=%s" % (k, shlex.quote(env[k]))
            print("mswb_ci: " + line)
            fh.write(line + "\n")
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
[ "$(uv --version | cut -d' ' -f2)" = "__UV_VERSION__" ] || { echo "prepare.sh: uv __UV_VERSION__ is not active"; exit 1; }
[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }

touch /home/mswb_ci.env
__PY__
__WEBUI__
__ELECTRON__
echo "gate: Python __PY_VERSION__, Node __NODE_VERSION__, dependencies as of the base commit, the PR test runners load"
"""

_PY_SNIPPET = r"""cutoff="$(git log -1 --format=%cI HEAD)"
uv venv --python /usr/local/bin/python3 .venv || { echo "prepare.sh: uv venv failed"; exit 1; }
python3 /home/mswb_ci.py __PY_FILES__ || { echo "prepare.sh: reading the CI install lines failed"; exit 1; }
ci_install() {
    for attempt in 1 2 3; do
        uv pip install --python .venv/bin/python --exclude-newer "$cutoff" --torch-backend cpu "$@" && return 0
        [ "$attempt" -lt 3 ] || { echo "prepare.sh: uv pip install $* failed 3 times"; exit 1; }
        sleep 30
    done
}
. /tmp/mswb_ci_install.sh
uv cache clean
. /home/mswb_ci.env

py_files=()
for f in __PY_FILES__; do
    if [ -f "$f" ]; then
        py_files+=("$f")
    fi
done
if [ ${#py_files[@]} -gt 0 ]; then
    rc=0
    .venv/bin/python -m pytest -p no:cacheprovider --rootdir=/home/__REPO__ --collect-only -q --continue-on-collection-errors "${py_files[@]}" > /tmp/mswb_collect_py.log || rc=$?
    tail -n 1 /tmp/mswb_collect_py.log
    if grep -q '^ERROR ' /tmp/mswb_collect_py.log; then
        grep '^ERROR ' /tmp/mswb_collect_py.log
    fi
    case "$rc" in
        0|1|5) ;;
        *) tail -n 50 /tmp/mswb_collect_py.log; echo "prepare.sh: pytest --collect-only exited with status $rc"; exit 1 ;;
    esac
fi
.venv/bin/python -c "import gaia, pytest"
"""

_NPM_SNIPPET = r"""cd /home/__REPO__/__DIR__
for attempt in 1 2 3; do
    npm ci && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: npm ci failed 3 times in __DIR__"; exit 1; }
    sleep 30
done
test -x node_modules/.bin/__TOOL__ || { echo "prepare.sh: __TOOL__ is not installed in __DIR__"; exit 1; }
node_modules/.bin/__TOOL__ --version
npm cache clean --force
cd /home/__REPO__
"""

_PYTEST_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__
report="/tmp/mswb_pytest.xml"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $f is not in the tree yet"
    fi
done
rm -f "$report"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the Python test files exist yet"
    exit 0
fi

. /home/mswb_ci.env
rc=0
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv/bin/python -m pytest -p no:cacheprovider \
    --rootdir=/home/__REPO__ -o junit_family=xunit1 --continue-on-collection-errors \
    --junitxml="$report" -rfE --tb=short "${files[@]}" < /dev/null || rc=$?
echo "mswb: pytest exit $rc"
.venv/bin/python /home/mswb_emit.py "$report" || exit 1

case "$rc" in
    0|1|5) ;;
    *) echo "=== MSB: pytest exited with status $rc ==="; exit "$rc" ;;
esac
exit 0
"""

_EMIT_PY = r"""import os
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
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
with open("/tmp/mswb_results.txt", "a", encoding="ascii", errors="backslashreplace") as out:
    for nodeid in sorted(status):
        out.write("%s %s\n" % (nodeid, status[nodeid]))
print("mswb: pytest reported %d tests" % len(status))
"""

_JS_SH = r"""#!/bin/bash
set -uo pipefail

lane="$1"
tool="$2"
shift 2
cd "/home/__REPO__/$lane"
out="/tmp/mswb_${tool}.json"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $lane/$f is not in the tree yet"
    fi
done
rm -f "$out"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the $tool test files exist yet"
    exit 0
fi

rc=0
if [ "$tool" = "vitest" ]; then
    node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
else
    node_modules/.bin/jest --ci --json --outputFile="$out" --runTestsByPath "${files[@]}" < /dev/null || rc=$?
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
  console.log('mswb: no JS test report was written');
  process.exit(0);
}
const raw = fs.readFileSync(out, 'utf8');
if (raw.trim() === '') {
  console.log('mswb: the JS test report is empty');
  process.exit(1);
}
const report = JSON.parse(raw);
const root = '/home/__REPO__';
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const clean = (x) => String(x).replace(/\s+/g, ' ').trim();
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map(clean).join(' > ');
    const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    const key = rel + '::' + title;
    if (!status.has(key) || rank[st] > rank[status.get(key)]) {
      status.set(key, st);
    }
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('mswb: suite error in ' + rel + ': ' + String(file.message || '').split('\n')[0]);
  }
}
const lines = [...status.keys()].sort().map((key) => key + ' ' + status.get(key) + '\n');
fs.appendFileSync('/tmp/mswb_results.txt', lines.join(''));
console.log('mswb: ' + path.basename(out) + ' reported ' + status.size + ' tests');
"""

_REPORT_SH = r"""#!/bin/bash
set -uo pipefail

echo "===== BEGIN TEST RESULTS ====="
if [ -f /tmp/mswb_results.txt ]; then
    LC_ALL=C sort -u /tmp/mswb_results.txt
fi
echo "===== END TEST RESULTS ====="
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class GaiaImageBase5720(Image):
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
    rm -f /tmp/node.tar.xz

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}" "pyyaml=={_PYYAML_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GaiaImageDefault5720(Image):
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
        return GaiaImageBase5720(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__PY_VERSION__": _PY_VERSION,
            "__UV_VERSION__": _UV_VERSION,
            "__NODE_VERSION__": _NODE_VERSION,
            "__PY_FILES__": " ".join(_py_files(self.pr)),
        }

    def _prepare(self) -> str:
        values = self._values()
        py = _render(_PY_SNIPPET, values) if _py_files(self.pr) else ""
        webui = ""
        if _webui_files(self.pr):
            webui = _render(_NPM_SNIPPET, {**values, "__DIR__": _WEBUI_DIR, "__TOOL__": "vitest"})
        electron = ""
        if _electron_files(self.pr):
            electron = _render(_NPM_SNIPPET, {**values, "__DIR__": _ELECTRON_DIR, "__TOOL__": "jest"})
        body = _render(_PREPARE_SH, values)
        return body.replace("__PY__\n", py).replace("__WEBUI__\n", webui).replace("__ELECTRON__\n", electron)

    def _act(self, name: str, *patches: str) -> File:
        lines = [_RUN_HEADER, "", f"cd /home/{self.pr.repo}", "rm -f /tmp/mswb_results.txt"]
        for patch in patches:
            lines.append(
                f"git apply --whitespace=nowarn {patch} || git apply --3way --whitespace=nowarn {patch} || "
                f'{{ echo "{name}: git apply {patch} failed" >&2; exit 1; }}'
            )
        py = _py_files(self.pr)
        if py:
            lines.append(f"bash /home/mswb_pytest.sh {' '.join(py)}")
        webui = _webui_files(self.pr)
        if webui:
            lines.append(f"bash /home/mswb_js.sh {_WEBUI_DIR} vitest {' '.join(webui)}")
        electron = _electron_files(self.pr)
        if electron:
            lines.append(f"bash /home/mswb_js.sh {_ELECTRON_DIR} jest {' '.join(electron)}")
        lines.append("bash /home/mswb_report.sh")
        return File(".", name, "\n".join(lines) + "\n")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_ci.py", _render(_CI_PY, values)),
            File(".", "mswb_emit.py", _EMIT_PY),
            File(".", "mswb_pytest.sh", _render(_PYTEST_SH, values)),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_js.sh", _render(_JS_SH, values)),
            File(".", "mswb_report.sh", _REPORT_SH),
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_HTTP_TIMEOUT=300 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^===== BEGIN TEST RESULTS =====$(.*?)^===== END TEST RESULTS =====$", re.M | re.S)
_LINE_RE = re.compile(
    r"^(?P<name>\S+?\.(?:py|tsx?|c?js)(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("amd", _INTERVAL)
class GAIA_5720_TO_8587(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GaiaImageDefault5720(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        blocks = _RESULTS_RE.findall(_ANSI_RE.sub("", test_log).replace("\r", ""))
        for line in (blocks[-1] if blocks else "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name"))

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
