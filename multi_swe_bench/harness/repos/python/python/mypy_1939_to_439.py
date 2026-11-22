from __future__ import annotations

import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "python"
_REPO = "mypy"
_ERA_MATCH = re.fullmatch(r"mypy_(\d+)_to_(\d+)", Path(__file__).stem)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like mypy_<hi>_to_<lo>.py")
_INTERVAL = f"{_REPO}_{_ERA_MATCH.group(1)}_to_{_ERA_MATCH.group(2)}"
_BASE_TAG = f"base-{_ERA_MATCH.group(1)}_to_{_ERA_MATCH.group(2)}"
_BASE_IMAGE = "python:3.5-slim-buster@sha256:2348c9c9fa90808066919c036d59919fbf82c7f9277c2ff8b6b8cc6e72f5b9d7"
_PIP_VERSION = "20.3.4"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^##MYPY_RESULTS_BEGIN##[ \t]*$(.*?)^##MYPY_RESULTS_END##[ \t]*$", re.M | re.S)
_CASE_LINE_RE = re.compile(r"^##MYPY_CASE## (PASSED|FAILED|SKIPPED) (\S.*?)[ \t]*$", re.M)

_ENV_SH = """export CI=true
export COLUMNS=150
export PYTHONDONTWRITEBYTECODE=1
export PYTHONIOENCODING=utf-8
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONPATH="/home/__REPO__"
install_deps() {
    local constraints=/tmp/mypy-constraints.txt
    local args=()
    printf '%s\\n' 'pytest<4' 'pytest-xdist<1.25' > "$constraints" || return 1
    if [ -f mypy/fastparse.py ] && grep -qE '^[[:space:]]*from typed_ast import ([a-z0-9_]+, *)*ast35[[:space:]]*$' mypy/fastparse.py; then
        echo "typed-ast<1.1" >> "$constraints" || return 1
    fi
    [ ! -f test-requirements.txt ] || args+=(-r test-requirements.txt)
    [ "${#args[@]}" -gt 0 ] || return 0
    python -m pip install --no-cache-dir -c "$constraints" "${args[@]}" || return 1
}
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -e

__ENV__
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
if [ -f .gitmodules ]; then
    for attempt in 1 2 3; do
        git submodule update --init --recursive && break || true
        [ "$attempt" -lt 3 ] || { echo "prepare.sh: submodule update failed 3 times"; exit 1; }
        echo "prepare.sh: submodule update attempt $attempt failed; retrying in 30s"
        sleep 30
    done
fi
bash /home/check_git_changes.sh

if [ -f setup.py ] && grep -qF "package_dir={'': 'lib-typing/3.2'" setup.py; then
    export PYTHONPATH="/home/__REPO__/lib-typing/3.2${PYTHONPATH:+:$PYTHONPATH}"
fi
for attempt in 1 2 3; do
    install_deps && break || true
    [ "$attempt" -lt 3 ] || break
    echo "prepare.sh: dependency install attempt $attempt failed; retrying in 30s"
    sleep 30
done
git clean -fdx

python /home/mypy_tests.py check
echo "DEPS_OK"
"""

_TEST_BLOCK = """if ! git diff --quiet HEAD -- test-requirements.txt mypy-requirements.txt build-requirements.txt setup.py pyproject.toml; then
    install_deps
fi

rc=0
python /home/mypy_tests.py run || rc=$?
exit "$rc"
"""

_RUN_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
if [ -f setup.py ] && grep -qF "package_dir={'': 'lib-typing/3.2'" setup.py; then
    export PYTHONPATH="/home/__REPO__/lib-typing/3.2${PYTHONPATH:+:$PYTHONPATH}"
fi
"""

_MYPY_TESTS_PY = r"""import fnmatch
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

PATCH = "/home/test.patch"
TEST_DIRS = ("mypy/test", "mypyc/test")
MAX_MYUNIT_PATTERNS = 200
PYTEST_KEYWORD_CHUNK = 250
DIFF_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
DATA_RE = re.compile(r"[\"']([\w.*\-\[\]/]+\.test)[\"']")
CASE_RE = re.compile(r"^\[case (\w+)", re.M)
DATA_SUITE_RE = re.compile(r"^class \w+\([^)]*\bDataSuite\b[^)]*\)", re.M)
MYUNIT_SUITE_RE = re.compile(r"^class \w+\((?:myunit\.)?Suite\)", re.M)

WRAPPER = r'''
import sys
import mypy.myunit as mu
out = open(sys.argv[1], "a")
orig = mu.run_single_test
def run_single_test(name, test):
    failed, skipped = orig(name, test)
    status = "SKIPPED" if skipped else ("FAILED" if failed else "PASSED")
    out.write("{} myunit::{}\n".format(status, name))
    out.flush()
    return failed, skipped
mu.run_single_test = run_single_test
if hasattr(mu, "main"):
    mu.main(sys.argv[2:])
else:
    import tests
    mu.run_test(tests.AllSuite(), sys.argv[2:])
'''


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def changed_paths():
    if not os.path.exists(PATCH):
        return []
    seen = []
    for path in DIFF_RE.findall(read(PATCH)):
        if path not in seen:
            seen.append(path)
    return seen


def test_modules():
    found = {}
    for directory in TEST_DIRS:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.startswith("test") and name.endswith(".py"):
                path = directory + "/" + name
                found[path] = read(path)
    return found


def myunit_style():
    if os.path.exists("mypy/myunit/__init__.py"):
        return "main" if "def main(" in read("mypy/myunit/__init__.py") else None
    if os.path.exists("mypy/myunit.py"):
        return "main" if "def main(" in read("mypy/myunit.py") else "legacy"
    return None


def module_name(path):
    return path[: -len(".py")].replace("/", ".")


def importers(helper, modules):
    short = os.path.basename(helper)[: -len(".py")]
    dotted = module_name(helper)
    users = []
    for path, text in modules.items():
        if re.search(r"\b" + re.escape(dotted) + r"\b", text) or re.search(
            r"from\s+(?:mypy|mypyc)\.test\s+import\s+[^\n]*\b" + re.escape(short) + r"\b", text
        ):
            users.append(path)
    return users


def select():
    paths = changed_paths()
    modules = test_modules()
    selection = {}
    for path in paths:
        if path in modules:
            selection[path] = None
    for path in paths:
        if not path.endswith(".py") or path in modules or not os.path.exists(path):
            continue
        if not path.startswith(TEST_DIRS):
            continue
        users = importers(path, modules)
        if 0 < len(users) <= 5:
            for user in users:
                selection[user] = None
    data_files = [path for path in paths if path.endswith(".test")]
    for path in data_files:
        if not os.path.exists(path):
            continue
        names = set(CASE_RE.findall(read(path)))
        if not names:
            continue
        base = os.path.basename(path)
        for module, text in modules.items():
            patterns = [os.path.basename(p) for p in DATA_RE.findall(text)]
            if not any(fnmatch.fnmatchcase(base, pattern) for pattern in patterns):
                continue
            if module in selection and selection[module] is None:
                continue
            selection.setdefault(module, set()).update(names)
    if not selection and not data_files and "mypy/test/testcheck.py" in modules:
        selection["mypy/test/testcheck.py"] = None
    return selection, modules


def runners(path, text):
    kinds = []
    if MYUNIT_SUITE_RE.search(text):
        kinds.append("myunit")
    if DATA_SUITE_RE.search(text) or not kinds:
        kinds.append("pytest")
    return kinds


def has_xdist():
    try:
        import xdist
    except ImportError:
        return False
    return True


def junit_cases(path):
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        print("mypy_tests: cannot read {}: {}".format(path, exc))
        return None
    cases = []
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        if not name:
            continue
        classname = case.get("classname") or ""
        test_id = classname + "::" + name if classname else name
        tags = set(child.tag for child in case)
        if tags & {"failure", "error"}:
            status = "FAILED"
        elif "skipped" in tags:
            status = "SKIPPED"
        else:
            status = "PASSED"
        cases.append((status, test_id))
    return cases


def pytest_base():
    return [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--color=no"]


def run_pytest_once(paths, keyword, tag):
    report = "/tmp/mypy-junit-{}.xml".format(tag)
    if os.path.exists(report):
        os.remove(report)
    command = pytest_base() + ["--junitxml=" + report]
    if has_xdist():
        command += ["-n", "4"]
    if keyword:
        command += ["-k", keyword]
    returncode = subprocess.call(command + sorted(paths))
    if not os.path.exists(report):
        print("mypy_tests: pytest exited with {} without writing {}".format(returncode, report))
        return None
    return junit_cases(report)


def run_pytest(paths, names, tag):
    ordered = sorted(names or ())
    chunks = [ordered[i:i + PYTEST_KEYWORD_CHUNK] for i in range(0, len(ordered), PYTEST_KEYWORD_CHUNK)] or [[]]
    print("mypy_tests: pytest {} module(s), {} case filter(s) in {} run(s)".format(len(paths), len(ordered), len(chunks)))
    sys.stdout.flush()
    cases = []
    for index, chunk in enumerate(chunks):
        found = run_pytest_once(paths, " or ".join(chunk), "{}-{}".format(tag, index))
        if found is None:
            return None
        cases.extend(found)
    return cases


def run_myunit(style, paths, names, tag):
    report = "/tmp/mypy-myunit-{}.txt".format(tag)
    open(report, "w").close()
    args = []
    if style == "main":
        for path in sorted(paths):
            args += ["-m", module_name(path)]
    if names and len(names) <= MAX_MYUNIT_PATTERNS:
        args += ["*" + name + "*" for name in sorted(names)]
    print("mypy_tests: myunit {} module(s), {} case filter(s)".format(len(paths), len(names or ())))
    sys.stdout.flush()
    returncode = subprocess.call([sys.executable, "-c", WRAPPER, report] + args)
    cases = []
    for line in read(report).splitlines():
        status, _, test_id = line.partition(" ")
        if test_id:
            cases.append((status, test_id))
    if not cases and returncode != 0:
        print("mypy_tests: myunit exited with {} without reporting any case".format(returncode))
        return None
    return cases


def plan():
    selection, modules = select()
    style = myunit_style()
    groups = {}
    if style == "legacy" and selection:
        names = set()
        whole = False
        for value in selection.values():
            if value is None:
                whole = True
            else:
                names.update(value)
        if whole or len(names) > MAX_MYUNIT_PATTERNS:
            groups[("myunit", True)] = [set(selection), set()]
        else:
            groups[("myunit", False)] = [set(selection), names]
        selection = {}
    for path, names in sorted(selection.items()):
        for kind in runners(path, modules.get(path, "")):
            key = (kind, names is None)
            entry = groups.setdefault(key, [set(), set()])
            entry[0].add(path)
            if names:
                entry[1].update(names)
    for (kind, whole), (paths, names) in sorted(groups.items()):
        print("mypy_tests: {} {} -> {}".format(kind, "whole" if whole else "cases", " ".join(sorted(paths))))
    return style, groups


def check():
    style, groups = plan()
    imports = ["mypy"]
    if os.path.exists("conftest.py"):
        imports.append("pytest")
    if style:
        imports.append("mypy.myunit")
    if style == "legacy":
        imports.append("tests")
    for (kind, _), (paths, _) in groups.items():
        if kind == "myunit" and style == "main":
            imports.extend(module_name(path) for path in paths)
    if subprocess.call([sys.executable, "-c", "import " + ", ".join(sorted(set(imports)))]) != 0:
        print("mypy_tests: import check failed")
        return 1
    collect = set()
    for (kind, _), (paths, _) in groups.items():
        if kind == "pytest":
            collect.update(paths)
    if collect:
        command = pytest_base() + ["--collect-only", "-q"]
        if has_xdist():
            command += ["-n", "0"]
        returncode = subprocess.call(command + sorted(collect), stdout=subprocess.DEVNULL)
        if returncode not in (0, 5):
            print("mypy_tests: pytest collection failed with {}".format(returncode))
            return 1
    return 0


def run():
    style, groups = plan()
    results = []
    ok = True
    for index, ((kind, whole), (paths, names)) in enumerate(sorted(groups.items())):
        tag = "{}-{}".format(kind, index)
        if kind == "pytest":
            cases = run_pytest(paths, None if whole else names, tag)
        else:
            cases = run_myunit(style, paths, None if whole else names, tag)
        if cases is None:
            ok = False
        else:
            results.extend(cases)
    print("##MYPY_RESULTS_BEGIN##")
    for status, test_id in results:
        print("##MYPY_CASE## {} {}".format(status, test_id))
    print("##MYPY_RESULTS_END##")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(check() if sys.argv[1:] == ["check"] else run())
"""


class MypyImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' -e 's|security.debian.org|archive.debian.org|g' -e '/-updates/d' /etc/apt/sources.list \\
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "pip=={_PIP_VERSION}"
RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class MypyImageDefault(Image):
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
        return MypyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__ENV__", _ENV_SH)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_RUN_HEADER_SH)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mypy_tests.py", _MYPY_TESTS_PY),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{_TEST_BLOCK}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{_TEST_BLOCK}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{_TEST_BLOCK}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = (
            Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)
            .replace("git gc --prune=now --aggressive", "git gc --prune=now")
        )

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    clean = _ANSI_RE.sub("", test_log)

    statuses: dict[str, set[str]] = {}
    for block in _RESULTS_RE.findall(clean):
        for status, test_id in _CASE_LINE_RE.findall(block):
            statuses.setdefault(test_id, set()).add(status)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    for test_id, seen in statuses.items():
        if "FAILED" in seen:
            failed_tests.add(test_id)
        elif "PASSED" in seen:
            passed_tests.add(test_id)
        else:
            skipped_tests.add(test_id)

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


@Instance.register(_ORG, _INTERVAL)
class MYPY(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return MypyImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)
