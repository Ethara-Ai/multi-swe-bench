import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12.16-slim-bookworm"
_PY_VERSION = "3.12.16"
_UV_VERSION = "0.13.8"
_NODE_VERSION = "20.20.2"
_NODE_SHA256_X64 = "df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d"
_NODE_SHA256_ARM64 = "73093db209e4e9e09dd7d15a47aeaab1b74833830df03efa5f942a1122c5fa71"
_INTERVAL = "hermes_webui_780_to_7906"
_BASE_TAG = "base-780_to_7906"
_CI_TEST_DEPS = (
    '"pyyaml>=6.0" pytest pytest-timeout pytest-asyncio pytest-shard '
    'python-docx openpyxl python-pptx "websockets>=15,<16" ruff "mcp<2"'
)

_PY_TEST_RE = re.compile(
    r"^\+\+\+ b/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$",
    re.MULTILINE,
)


_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _files(pr: PullRequest) -> list[str]:
    return sorted(set(_PY_TEST_RE.findall(pr.test_patch or "")))


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

stamp="$(git show -s --format=%cI HEAD)"
reqs=(-r requirements.txt)
if [ -f requirements-dev.txt ]; then
    reqs+=(-r requirements-dev.txt)
fi
for attempt in 1 2 3; do
    uv pip install --python /usr/local/bin/python3 --exclude-newer "$stamp" "${reqs[@]}" __CI_TEST_DEPS__ && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: dependency install failed 3 times"; exit 1; }
    sleep 30
done
uv pip check --python /usr/local/bin/python3
uv pip freeze --python /usr/local/bin/python3
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
python3 -c "import yaml, pytest, pytest_timeout, pytest_asyncio"
echo "gate: dependencies as of $stamp, Python __PY_VERSION__, Node __NODE_VERSION__ and the PR test files load"
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
    --timeout=60 --continue-on-collection-errors \
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

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class HermesWebuiImageBase(Image):
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
    git ca-certificates curl xz-utils \\
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


class HermesWebuiImageDefault(Image):
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
        return HermesWebuiImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__CI_TEST_DEPS__": _CI_TEST_DEPS,
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__PY_VERSION__": _PY_VERSION,
            "__NODE_VERSION__": _NODE_VERSION,
            "__PY_FILES__": " ".join(_files(self.pr)),
        }

    def _test_cmds(self) -> str:
        lines = [f"cd /home/{self.pr.repo}"]
        py = _files(self.pr)
        if py:
            lines.append(f"bash /home/mswb_pytest.sh {' '.join(py)}")
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
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_emit.py", _EMIT_PY),
            File(".", "mswb_pytest.sh", _render(_PYTEST_SH, values)),
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_HTTP_TIMEOUT=300 \\
    NPM_CONFIG_UPDATE_NOTIFIER=false

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>pytest::tests/\S.*?\.py(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("nesquena", "hermes-webui")
@Instance.register("nesquena", _INTERVAL)
class HERMES_WEBUI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HermesWebuiImageDefault(self.pr, self._config)

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
