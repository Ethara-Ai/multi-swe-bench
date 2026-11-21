import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12.16-slim-bookworm"
_PY_VERSION = "3.12.16"
_UV_VERSION = "0.13.8"
_INTERVAL = "spec_kitty_316_to_3856"
_BASE_TAG = "base-316_to_3856"

_PY_TEST_RE = re.compile(r"^\+\+\+ b/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$", re.MULTILINE)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _py_files(pr: PullRequest) -> list[str]:
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
[ "$(uv --version | cut -d' ' -f2)" = "__UV_VERSION__" ] || { echo "prepare.sh: uv __UV_VERSION__ is not active"; exit 1; }

if [ -f uv.lock ]; then
    for attempt in 1 2 3; do
        uv sync --frozen --all-extras --python /usr/local/bin/python3 && break
        [ "$attempt" -lt 3 ] || { echo "prepare.sh: uv sync failed 3 times"; exit 1; }
        sleep 30
    done
else
    cutoff="$(git log -1 --format=%cI HEAD)"
    uv venv --python /usr/local/bin/python3 .venv || { echo "prepare.sh: uv venv failed"; exit 1; }
    for attempt in 1 2 3; do
        uv pip install --python .venv/bin/python --exclude-newer "$cutoff" -e ".[test]" && break
        [ "$attempt" -lt 3 ] || { echo "prepare.sh: dependency install failed 3 times"; exit 1; }
        sleep 30
    done
fi
uv cache clean

py_files=()
for f in __PY_FILES__; do
    if [ -f "$f" ]; then
        py_files+=("$f")
    fi
done
if [ ${#py_files[@]} -gt 0 ]; then
    rc=0
    .venv/bin/python -m pytest -p no:cacheprovider --collect-only -q --continue-on-collection-errors "${py_files[@]}" > /tmp/mswb_collect_py.log || rc=$?
    tail -n 1 /tmp/mswb_collect_py.log
    if grep -q '^ERROR ' /tmp/mswb_collect_py.log; then
        grep '^ERROR ' /tmp/mswb_collect_py.log
    fi
    case "$rc" in
        0|1|5) ;;
        *) tail -n 50 /tmp/mswb_collect_py.log; echo "prepare.sh: pytest --collect-only exited with status $rc"; exit 1 ;;
    esac
fi
.venv/bin/python -c "import specify_cli, pytest"
echo "gate: Python __PY_VERSION__, dependencies installed, specify_cli and the PR test files load"
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
    echo "mswb: none of the test files exist yet"
    exit 0
fi

rc=0
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv/bin/python -m pytest -p no:cacheprovider \
    -o junit_family=xunit1 --continue-on-collection-errors \
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
sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
print("===== BEGIN TEST RESULTS =====")
if not os.path.exists(path):
    print("mswb: no junit report was written")
    print("===== END TEST RESULTS =====")
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
    print("%s %s" % (nodeid, status[nodeid]))
print("===== END TEST RESULTS =====")
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class SpecKittyImageBase316(Image):
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
    git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class SpecKittyImageDefault316(Image):
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
        return SpecKittyImageBase316(self.pr, self.config)

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
            "__PY_FILES__": " ".join(_py_files(self.pr)),
        }

    def _act(self, name: str, *patches: str) -> File:
        lines = [_RUN_HEADER, "", f"cd /home/{self.pr.repo}"]
        for patch in patches:
            lines.append(
                f"git apply --whitespace=nowarn {patch} || git apply --3way --whitespace=nowarn {patch} || "
                f'{{ echo "{name}: git apply {patch} failed" >&2; exit 1; }}'
            )
        files = _py_files(self.pr)
        if files:
            lines.append(f"bash /home/mswb_pytest.sh {' '.join(files)}")
        return File(".", name, "\n".join(lines) + "\n")

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
    UV_HTTP_TIMEOUT=300

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^===== BEGIN TEST RESULTS =====$(.*?)^===== END TEST RESULTS =====$", re.M | re.S)
_LINE_RE = re.compile(r"^(?P<name>tests/\S.*?\.py(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$")


@Instance.register("spec-kitty", "spec-kitty")
@Instance.register("spec-kitty", _INTERVAL)
class SPEC_KITTY_316_TO_3856(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SpecKittyImageDefault316(self.pr, self._config)

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
