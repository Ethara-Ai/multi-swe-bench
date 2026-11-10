import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.11.18-slim-bookworm"
_PY_VERSION = "3.11.18"
_BASE_TAG = "base-1535_to_2227"
_REQUIREMENTS = "requirements-ci.txt"
_BUILD_REQUIREMENTS = ".github/requirements/pep517-build.txt"
_CPU_REQUIREMENTS = "/tmp/mswb-requirements-ci.txt"
_CPU_PAGES = (
    "https://download.pytorch.org/whl/cpu/torch/",
    "https://download.pytorch.org/whl/cpu/torchvision/",
)
_CPU_WHEELS = (
    (
        "torch",
        "2.13.0+cpu",
        (
            "sha256:6746dbcbeb526eb61330b76b41ff1b4eb848951103a892eeb080dfa2b264667b",
            "sha256:84453b69508ec79902f899c5ed9495acb9e2bbe9fda5f1d5d6f19e3c3842e1a7",
        ),
    ),
    (
        "torchvision",
        "0.28.0+cpu",
        (
            "sha256:1dad604dfc0177ecebe0891bd9701fe2c62ec3f7819a247be541b3fb6effee99",
            "sha256:22958193d72444ed7cbcc665ba4821a31e5279f9c4d1ad08520918b30896b78a",
        ),
    ),
)
_GPU_NAMES = (
    "torch",
    "torchvision",
    "triton",
    "cuda-bindings",
    "cuda-pathfinder",
    "cuda-toolkit",
)
_GPU_PREFIXES = ("nvidia-",)
_GPU_PIN_COUNT = 21

_TEST_FILE_RE = re.compile(
    r"^\+\+\+ b/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$", re.MULTILINE
)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch or "")))


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

_CPU_TOOL = "mswb_cpu_requirements.py"

_CPU_TOOL_PY = r'''import re
import sys

GPU_NAMES = set(__GPU_NAMES__)
GPU_PREFIXES = __GPU_PREFIXES__
GPU_PIN_COUNT = __GPU_PIN_COUNT__
CPU_WHEELS = __CPU_WHEELS__


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def is_gpu(name):
    return name in GPU_NAMES or name.startswith(GPU_PREFIXES)


def main():
    source, target = sys.argv[1], sys.argv[2]
    blocks = []
    with open(source, encoding="utf-8") as handle:
        for line in handle:
            head = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
            if head:
                blocks.append([normalize(head.group(1)), [line]])
            elif blocks and line[:1].isspace():
                blocks[-1][1].append(line)
            elif line.strip() and not line.startswith("#"):
                raise SystemExit(f"mswb_cpu_requirements: unexpected line {line!r}")

    dropped = sorted(name for name, _ in blocks if is_gpu(name))
    missing = sorted(GPU_NAMES - set(dropped))
    if missing or len(dropped) != GPU_PIN_COUNT:
        raise SystemExit(
            f"mswb_cpu_requirements: expected {GPU_PIN_COUNT} GPU pins, got {dropped}, missing {missing}"
        )

    with open(target, "w", encoding="utf-8") as handle:
        for name, lines in blocks:
            if not is_gpu(name):
                handle.write("".join(lines))
        for name, version, hashes in CPU_WHEELS:
            handle.write(f"{name}=={version}")
            handle.write("".join(f" \\\n    --hash={digest}" for digest in hashes))
            handle.write("\n")

    kept = sum(1 for name, _ in blocks if not is_gpu(name))
    print(f"mswb_cpu_requirements: kept {kept} pins, dropped {len(dropped)} GPU pins, added {len(CPU_WHEELS)} CPU wheels")


if __name__ == "__main__":
    main()
'''

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

python3 -m pip install --no-cache-dir --require-hashes -r __BUILD_REQUIREMENTS__
python3 -m pip install --no-cache-dir --no-deps --no-build-isolation -e .
python3 /home/__CPU_TOOL__ __REQUIREMENTS__ __CPU_REQUIREMENTS__
python3 -m pip install --no-cache-dir --require-hashes __FIND_LINKS__ -r __CPU_REQUIREMENTS__
python3 -m pip check

if python3 -m pip list --format=freeze | grep -iE '^(nvidia-|cuda-|triton==)'; then
    echo "prepare.sh: a GPU package is installed"
    exit 1
fi
python3 -c "import pytest, torch, torchvision, semantica; assert torch.__version__ == '__TORCH_VERSION__', torch.__version__; assert torchvision.__version__ == '__TORCHVISION_VERSION__', torchvision.__version__"

present=()
for f in __TEST_FILES__; do
    if [ -f "$f" ]; then
        present+=("$f")
    fi
done
if [ ${#present[@]} -gt 0 ]; then
    python3 -m pytest -p no:cacheprovider --collect-only -q "${present[@]}" > /tmp/mswb_collect.log
    tail -n 1 /tmp/mswb_collect.log
fi
echo "gate: semantica, CPU torch, pytest and the PR test files load"
"""

_PYTEST_SH = r"""#!/bin/bash
set -uo pipefail

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
    echo "mswb: none of the test files exist yet"
    exit 0
fi

rc=0
python3 -m pytest -p no:cacheprovider -o junit_family=xunit1 \
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


def _cpu_tool() -> str:
    return _render(
        _CPU_TOOL_PY,
        {
            "__GPU_NAMES__": repr(_GPU_NAMES),
            "__GPU_PREFIXES__": repr(_GPU_PREFIXES),
            "__GPU_PIN_COUNT__": str(_GPU_PIN_COUNT),
            "__CPU_WHEELS__": repr(_CPU_WHEELS),
        },
    )


class Semantica1535To2227ImageBase(Image):
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
    git build-essential pkg-config curl ca-certificates libgl1 libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

RUN [ "$(python3 -c 'import platform; print(platform.python_version())')" = "{_PY_VERSION}" ] || {{ echo "python {_PY_VERSION} missing"; exit 1; }}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Semantica1535To2227ImageDefault(Image):
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
        return Semantica1535To2227ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        return _render(
            _PREPARE_SH,
            {
                "__REPO__": self.pr.repo,
                "__TEST_FILES__": " ".join(_test_files(self.pr)),
                "__SHA__": self.pr.base.sha,
                "__PY_VERSION__": _PY_VERSION,
                "__BUILD_REQUIREMENTS__": _BUILD_REQUIREMENTS,
                "__REQUIREMENTS__": _REQUIREMENTS,
                "__CPU_TOOL__": _CPU_TOOL,
                "__CPU_REQUIREMENTS__": _CPU_REQUIREMENTS,
                "__FIND_LINKS__": " ".join(f"--find-links {page}" for page in _CPU_PAGES),
                "__TORCH_VERSION__": _CPU_WHEELS[0][1],
                "__TORCHVISION_VERSION__": _CPU_WHEELS[1][1],
            },
        )

    def _test_cmds(self) -> str:
        return f"""cd /home/{self.pr.repo}
bash /home/mswb_pytest.sh {" ".join(_test_files(self.pr))}
"""

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
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", _CPU_TOOL, _cpu_tool()),
            File(".", "mswb_emit.py", _EMIT_PY),
            File(".", "mswb_pytest.sh", _PYTEST_SH),
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
    PIP_RETRIES=10 \\
    PIP_DEFAULT_TIMEOUT=120

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


@Instance.register("semantica-agi", "semantica_1535_to_2227")
class SEMANTICA_1535_TO_2227(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Semantica1535To2227ImageDefault(self.pr, self._config)

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
