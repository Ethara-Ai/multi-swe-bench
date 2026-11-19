import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.10.22-slim-bookworm"
_UV_VERSION = "0.13.8"
_INTERVAL = "haystack_10435_to_13006"
_BASE_TAG = "base-10435_to_13006"

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
git checkout --detach __SHA__
bash /home/check_git_changes.sh

cutoff="$(git log -1 --format=%cI HEAD)"
python3 -c 'import tomllib' 2>/dev/null || uv pip install --system --exclude-newer "$cutoff" tomli
deps="$(python3 -c 'try:
    import tomllib
except ImportError:
    import tomli as tomllib
envs = tomllib.load(open("pyproject.toml", "rb"))["tool"]["hatch"]["envs"]
print("\n".join(envs["default"]["dependencies"] + envs["test"]["dependencies"]))')"
mapfile -t dep_list <<< "$deps"
uv pip install --system --exclude-newer "$cutoff" --torch-backend cpu -e . "${dep_list[@]}" \
    || { echo "prepare.sh: dependency install failed"; exit 1; }
python3 -m pytest -p no:cacheprovider --collect-only -qq -m "not integration" test
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__
__APPLY__
rc=0
python3 -c '
import gc
import sys
import pytest
class MainThreadGC:
    def __init__(self):
        self.n = 0
    def pytest_runtest_logfinish(self, nodeid, location):
        self.n += 1
        gc.collect(2 if self.n % 100 == 0 else 1)
gc.disable()
sys.exit(pytest.main(sys.argv[1:], plugins=[MainThreadGC()]))
' -p no:cacheprovider -o log_cli=false -o junit_family=xunit1 \
    -m "not integration" --continue-on-collection-errors \
    --junitxml=/tmp/junit.xml -rfE --tb=short test < /dev/null || rc=$?
python3 - /tmp/junit.xml <<'EOF'
import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
print("===== BEGIN TEST RESULTS =====")
rank = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
status = {}
for case in ET.parse(sys.argv[1]).getroot().iter("testcase"):
    file = case.get("file") or ""
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    if not file.endswith(".py"):
        print("junit: testcase without a file: %s %s" % (classname, name))
        continue
    module = file[:-3].replace("/", ".")
    parts = [file]
    if classname.startswith(module + "."):
        parts.extend(classname[len(module) + 1:].split("."))
    if classname or name != module:
        parts.append(name)
    nodeid = "::".join(parts)
    kids = {child.tag for child in case}
    if "failure" in kids or "error" in kids:
        st = "FAILED"
    elif "skipped" in kids:
        st = "SKIPPED"
    else:
        st = "PASSED"
    if rank[st] >= rank.get(status.get(nodeid), 0):
        status[nodeid] = st
for nodeid in sorted(status):
    print("%s %s" % (nodeid, status[nodeid]))
print("===== END TEST RESULTS =====")
EOF
case "$rc" in
    0|1) ;;
    *) echo "pytest exited with status $rc"; exit "$rc" ;;
esac
"""


class HaystackImageBase10435(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _PY_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {_PY_IMAGE}

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

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir "uv=={_UV_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class HaystackImageDefault10435(Image):
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
        return HaystackImageBase10435(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run(self, name: str, *patches: str) -> File:
        apply = "\n".join(f"git apply --whitespace=nowarn {p}" for p in patches)
        text = _RUN_SH.replace("__REPO__", self.pr.repo).replace("__APPLY__", apply)
        return File(".", name, text)

    def files(self) -> list[File]:
        prepare = _PREPARE_SH.replace("__REPO__", self.pr.repo).replace("__SHA__", self.pr.base.sha)
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            self._run("run.sh"),
            self._run("test-run.sh", "/home/test.patch"),
            self._run("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return f"""FROM {base.image_name()}:{base.image_tag()}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_NO_CACHE=1 \\
    UV_HTTP_TIMEOUT=300

{copies}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^===== BEGIN TEST RESULTS =====$(.*?)^===== END TEST RESULTS =====$", re.M | re.S)
_LINE_RE = re.compile(r"^(?P<name>test/\S.*?\.py(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$")


@Instance.register("deepset-ai", _INTERVAL)
class HAYSTACK_10435_TO_13006(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HaystackImageDefault10435(self.pr, self._config)

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
