import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12.16-slim-trixie"
_PY_VERSION = "3.12.16"
_UV_VERSION = "0.13.8"
_MATURIN_VERSION = "1.16.0"
_NODE_VERSION = "24.22.3"
_NODE_SHA256_X64 = "4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb"
_NODE_SHA256_ARM64 = "6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c"
_RUSTUP_VERSION = "1.29.3"
_RUSTUP_SHA256_AMD64 = "b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55"
_RUSTUP_SHA256_ARM64 = "9f608c142e0e38a78957d0661ba1dfaf0b6fb37184cfaa4b9a0aa57af6184282"
_RUST_TOOLCHAIN = "1.95.0"
_HF_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_HF_MODEL_SHA = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_INTERVAL = "headroom_480_to_4773"
_BASE_TAG = "base-480_to_4773"

_PY_TEST_RE = re.compile(
    r"^\+\+\+ b/((?:tests|scripts/tests|plugins/[^/\s]+/tests)/(?:[^\s/]+/)*test_[^\s/]+\.py)$",
    re.MULTILINE,
)
_JS_TEST_RE = re.compile(
    r"^\+\+\+ b/(sdk/typescript|plugins/[^/\s]+)/(\S+\.(?:test|spec)\.[cm]?[jt]sx?)$",
    re.MULTILINE,
)
_PLUGIN_RE = re.compile(r"^plugins/([^/\s]+)/tests/")


def _files(pattern: re.Pattern, pr: PullRequest) -> list[str]:
    return sorted(set(pattern.findall(pr.test_patch or "")))


def _js_groups(pr: PullRequest) -> list[tuple[str, list[str]]]:
    grouped: dict[str, set[str]] = {}
    for pkg, test_file in _JS_TEST_RE.findall(pr.test_patch or ""):
        grouped.setdefault(pkg, set()).add(test_file)
    return [(pkg, sorted(files)) for pkg, files in sorted(grouped.items())]


def _plugins(pr: PullRequest) -> list[str]:
    found = set()
    for f in _files(_PY_TEST_RE, pr):
        m = _PLUGIN_RE.match(f)
        if m:
            found.add(m.group(1))
    return sorted(found)


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

export CARGO_NET_RETRY=10 CARGO_HTTP_TIMEOUT=120
[ "$(python3 -c 'import platform; print(platform.python_version())')" = "__PY_VERSION__" ] || { echo "prepare.sh: Python __PY_VERSION__ is not active"; exit 1; }
[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
grep -q '^channel = "__RUST_TOOLCHAIN__"' rust-toolchain.toml || { echo "prepare.sh: rust-toolchain.toml does not pin __RUST_TOOLCHAIN__"; exit 1; }
[ "$(rustc --version | cut -d' ' -f2)" = "__RUST_TOOLCHAIN__" ] || { echo "prepare.sh: rustc __RUST_TOOLCHAIN__ is not active"; exit 1; }

torch_version="$(python3 -c "import tomllib; d = tomllib.load(open('uv.lock', 'rb')); print(next(p['version'] for p in d['package'] if p['name'] == 'torch'))")"
gpu_packages="$(python3 -c "import tomllib; d = tomllib.load(open('uv.lock', 'rb')); print(' '.join(sorted(p['name'] for p in d['package'] if p['name'] in ('torch', 'triton') or p['name'].startswith('nvidia-'))))")"
skip=()
for p in $gpu_packages; do
    skip+=(--no-emit-package "$p")
done
uv export --frozen --no-hashes --extra dev --extra langchain --extra html --no-emit-project "${skip[@]}" -o /tmp/mswb_requirements.txt
for attempt in 1 2 3; do
    uv pip install --python /usr/local/bin/python3 --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==${torch_version}+cpu" && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: CPU torch ${torch_version} install failed 3 times"; exit 1; }
    sleep 30
done
for attempt in 1 2 3; do
    uv pip install --python /usr/local/bin/python3 -r /tmp/mswb_requirements.txt && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: locked dependency install failed 3 times"; exit 1; }
    sleep 30
done

rm -rf /tmp/mswb_dist
if grep -q '^\[profile\.ci\]' Cargo.toml; then
    profile=(--profile ci)
else
    profile=(--release)
fi
maturin build "${profile[@]}" --out /tmp/mswb_dist --interpreter /usr/local/bin/python3
uv pip install --python /usr/local/bin/python3 --no-deps /tmp/mswb_dist/*.whl
site="$(python3 -c 'import sysconfig; print(sysconfig.get_path("platlib"))')"
cp "$site"/headroom/_core*.so headroom/
rm -rf /tmp/mswb_dist target
__PLUGINS__
uv pip check --python /usr/local/bin/python3
__JS__
for attempt in 1 2 3; do
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python3 -c "from huggingface_hub import snapshot_download; print(snapshot_download('__HF_MODEL__'))" > /tmp/mswb_model_path.txt && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: __HF_MODEL__ download failed 3 times"; exit 1; }
    sleep 30
done
case "$(tail -n 1 /tmp/mswb_model_path.txt)" in
    */snapshots/__HF_MODEL_SHA__) ;;
    *) echo "prepare.sh: __HF_MODEL__ resolved to $(tail -n 1 /tmp/mswb_model_path.txt), expected revision __HF_MODEL_SHA__"; exit 1 ;;
esac
uv cache clean

cd /home/__REPO__
py_files=()
for f in __PY_FILES__; do
    if [ -f "$f" ]; then
        py_files+=("$f")
    fi
done
if [ ${#py_files[@]} -gt 0 ]; then
    python3 -m pytest -p no:cacheprovider --rootdir=/home/__REPO__ --collect-only -q "${py_files[@]}" > /tmp/mswb_collect_py.log
    tail -n 1 /tmp/mswb_collect_py.log
fi
__JS_GATE__
python3 -c "import headroom, headroom._core, pytest, pytest_asyncio, torch; assert torch.__version__.endswith('+cpu'), torch.__version__"
echo "gate: locked deps, CPU torch, headroom._core and the PR test files load"
"""

_PLUGIN_SNIPPET = r"""uv pip install --python /usr/local/bin/python3 --no-deps -e plugins/__PLUGIN__
"""

_JS_SNIPPET = r"""cd /home/__REPO__/__PKG__
for attempt in 1 2 3; do
    npm ci && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: npm ci in __PKG__ failed 3 times"; exit 1; }
    sleep 30
done
test -x node_modules/.bin/vitest || { echo "prepare.sh: vitest is not installed in __PKG__"; exit 1; }
cd /home/__REPO__
"""

_JS_GATE_SNIPPET = r"""cd /home/__REPO__/__PKG__
js_files=()
for f in __JS_FILES__; do
    if [ -f "$f" ]; then
        js_files+=("$f")
    fi
done
if [ ${#js_files[@]} -gt 0 ]; then
    node_modules/.bin/vitest list --filesOnly "${js_files[@]}" > /tmp/mswb_collect_js___N__.log
    tail -n 1 /tmp/mswb_collect_js___N__.log
fi
cd /home/__REPO__
"""

_PYTEST_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__
report=/tmp/mswb_pytest.xml
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
    echo "mswb: none of the python test files exist yet"
    exit 0
fi

rc=0
python3 -m pytest -p no:cacheprovider --rootdir=/home/__REPO__ -o junit_family=xunit1 \
    --continue-on-collection-errors \
    --junitxml="$report" -rfE --tb=short "${files[@]}" < /dev/null || rc=$?
echo "mswb: pytest exit $rc"
python3 /home/mswb_emit.py "$report" || exit 1

case "$rc" in
    0|1|2|5) ;;
    *) echo "=== MSB: pytest exited with status $rc ==="; exit "$rc" ;;
esac
if [ ! -f "$report" ] && [ "$rc" -ne 5 ]; then
    echo "=== MSB: pytest wrote no report (status $rc) ==="
    exit 1
fi
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
for nodeid in sorted(status):
    print("pytest::%s %s" % (nodeid, status[nodeid]))
"""

_VITEST_SH = r"""#!/bin/bash
set -uo pipefail

pkg="$1"
shift
cd "/home/__REPO__/$pkg"
out="/tmp/mswb_vitest_$(echo "$pkg" | tr '/' '_').json"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $pkg/$f is not in the tree yet"
    fi
done
rm -f "$out"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the $pkg test files exist yet"
    exit 0
fi

rc=0
node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
echo "mswb: $pkg vitest exit $rc"
node /home/mswb_digest.js "$out" || exit 1
if [ "$rc" -gt 1 ]; then
    echo "=== MSB: $pkg vitest exited with status $rc ==="
    exit "$rc"
fi
if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
    echo "=== MSB: $pkg vitest wrote no report (status $rc) ==="
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


class HeadroomImageBase(Image):
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
    git build-essential pkg-config ca-certificates curl xz-utils \\
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

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_AUTO_INSTALL=0 \\
    PATH=/usr/local/cargo/bin:$PATH

RUN set -eux; \\
    target="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "${{target}}" in \\
      amd64) triple=x86_64-unknown-linux-gnu; sum={_RUSTUP_SHA256_AMD64} ;; \\
      arm64) triple=aarch64-unknown-linux-gnu; sum={_RUSTUP_SHA256_ARM64} ;; \\
      *) echo "unsupported arch ${{target}}"; exit 1 ;; \\
    esac; \\
    curl --proto '=https' --tlsv1.2 -fsSL --retry 5 -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/${{triple}}/rustup-init"; \\
    echo "${{sum}}  /tmp/rustup-init" | sha256sum -c -; \\
    chmod +x /tmp/rustup-init; \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \\
    rm -f /tmp/rustup-init; \\
    rustup set auto-self-update disable; \\
    rustup toolchain install {_RUST_TOOLCHAIN} --profile minimal --component rustfmt --component clippy; \\
    rustup default {_RUST_TOOLCHAIN}; \\
    [ "$(rustc --version | cut -d' ' -f2)" = "{_RUST_TOOLCHAIN}" ]

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}" "maturin=={_MATURIN_VERSION}" && \\
    [ "$(uv --version | cut -d' ' -f2)" = "{_UV_VERSION}" ] && \\
    [ "$(maturin --version | cut -d' ' -f2)" = "{_MATURIN_VERSION}" ]

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class HeadroomImageDefault(Image):
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
        return HeadroomImageBase(self.pr, self.config)

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
            "__RUST_TOOLCHAIN__": _RUST_TOOLCHAIN,
            "__HF_MODEL__": _HF_MODEL,
            "__HF_MODEL_SHA__": _HF_MODEL_SHA,
            "__PY_FILES__": " ".join(_files(_PY_TEST_RE, self.pr)),
        }

    def _prepare(self) -> str:
        plugins = "".join(
            _render(_PLUGIN_SNIPPET, {"__PLUGIN__": p}) for p in _plugins(self.pr)
        )
        groups = _js_groups(self.pr)
        js = "".join(_render(_JS_SNIPPET, {"__PKG__": pkg}) for pkg, _ in groups)
        js_gate = "".join(
            _render(
                _JS_GATE_SNIPPET,
                {"__PKG__": pkg, "__JS_FILES__": " ".join(files), "__N__": str(n)},
            )
            for n, (pkg, files) in enumerate(groups)
        )
        text = _render(
            _PREPARE_SH,
            {
                "__PLUGINS__": plugins,
                "__JS__": js,
                "__JS_GATE__": js_gate,
            },
        )
        return _render(text, self._values())

    def _test_cmds(self) -> str:
        lines = [f"cd /home/{self.pr.repo}"]
        py = _files(_PY_TEST_RE, self.pr)
        if py:
            lines.append(f"bash /home/mswb_pytest.sh {' '.join(py)}")
        for pkg, files in _js_groups(self.pr):
            lines.append(f"bash /home/mswb_vitest.sh {pkg} {' '.join(files)}")
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
    UV_HTTP_TIMEOUT=300 \\
    CARGO_NET_RETRY=10 \\
    CARGO_HTTP_TIMEOUT=120 \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    HF_HUB_OFFLINE=1 \\
    TRANSFORMERS_OFFLINE=1 \\
    HF_HUB_DISABLE_TELEMETRY=1 \\
    TOKENIZERS_PARALLELISM=false \\
    HEADROOM_HARD_WATCHDOG_SECS=0

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>(?:pytest::(?:tests|scripts/tests|plugins/[^/\s]+/tests)/\S.*?\.py|vitest::(?:sdk/typescript|plugins/[^/\s]+)/\S.*?\.[cm]?[jt]sx?)(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("headroomlabs-ai", "headroom")
@Instance.register("headroomlabs-ai", _INTERVAL)
class HEADROOM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HeadroomImageDefault(self.pr, self._config)

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
