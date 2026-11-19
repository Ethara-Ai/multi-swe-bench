import re
import tomllib

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Era 1 of PrimeIntellect-ai/prime-agent: the pre-rewrite tree on `main`.
#   PR    base sha  branch  date        tree at base
#   2160  427ea4c7  main    2026-09-09  TypeScript era: package.json +
#                                       package-lock.json, NO Cargo.toml
# The PR's graded tests are Python only (scripts/benchmarks/tests/*.py, added by
# test.patch); its TypeScript sources are not exercised by them, so this era
# needs neither Node nor Rust: just Python >=3.11 + pytest + the suite's pinned
# third-party packages. The Rust workspace PRs (#2614, #2724, `rust` branch)
# are a separate era config: prime_agent_2724_to_2614.py.
_ORG = "PrimeIntellect-ai"
_INTERVAL = "prime_agent_2160_to_2160"

# Python pin: scripts/benchmarks/pyproject.toml (added by the PR) declares
# `requires-python = ">=3.11"` and ruff `target-version = "py311"`; no
# .python-version exists at 427ea4c7. Mirror of the Docker Hub official image
# (same multi-arch digests, no pull rate limit).
_PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.11.14-bookworm"
_PYTEST_VERSION = "8.4.2"
# The PR's Python tests import third-party packages (httpx, pexpect,
# prime-sandboxes, pydantic, pyte and their transitive closure) that are
# declared only by scripts/benchmarks/pyproject.toml + uv.lock, which fix.patch
# adds. Without them the graded fix stage cannot import the suite (and would
# need network). The exact pins are read at render time from the added lines of
# that uv.lock in self.pr.fix_patch (runtime closure of the virtual root, dev
# group excluded) and only the third-party packages are installed into the
# pytest venv; no fix.patch content enters the tree.
_UV_VERSION = "0.12.9"
_PY_LOCKFILE = "scripts/benchmarks/uv.lock"
# Import names of the suite's direct dependencies (pyproject `dependencies`),
# imported by the gate to prove the provisioned venv is usable.
_GATE_IMPORTS = "httpx pexpect prime_sandboxes pydantic pyte"
_TARGET_TIMEOUT = 1800

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SYNTH = re.compile(r"^@@SYNTH_FAIL (\S+)$")
_MARK_PYTEST = re.compile(r"^##MSB-PYTEST\b")
_MARK_END = re.compile(r"^##MSB-PYTEST-END\b")
_PYTEST = re.compile(
    r"^(\S+\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
)

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# Test driver shared by the gate (prepare.sh) and the three graded stages.
# It runs the Python test files test.patch adds/modifies with pytest, grouped by
# project directory. A file that fails to collect (typically: test.patch needs
# code from fix.patch) reports every test whose body test.patch adds/touches
# (/home/test_targets.txt: path<TAB>new-side line) as `@@SYNTH_FAIL <name>`
# under the exact name the passing stage prints, so the break is graded as a
# FAIL instead of vanishing (NONE). The exit status is non-zero if any
# invocation failed.
_DRIVER = r'''#!/usr/bin/env python3
import ast
import os
import re
import signal
import subprocess
import sys

REPO = "/home/@@REPO@@"
TIMEOUT = @@TIMEOUT@@
PYTHON = "/opt/msb-venv/bin/python"
GATE_IMPORTS = "@@GATE_IMPORTS@@".split()
NODEID = re.compile(r"^[^\s/][^\s]*\.py::")


def log(msg):
    print(msg, flush=True)


def wanted_files():
    with open("/home/test_files.txt") as fh:
        return [line.strip() for line in fh if line.strip()]


def run(cmd, cwd, env=None, timeout=TIMEOUT):
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        rc = 124
        err += ("\nMSB_TIMEOUT after %ss\n" % timeout).encode()
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def touched_lines():
    rows = {}
    with open("/home/test_targets.txt") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                rows.setdefault(parts[0], set()).add(int(parts[1]))
    return rows


def py_tests(path):
    """(pytest id suffix, first line, last line) of every test in a module."""
    try:
        with open(path) as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return []
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def span(node):
        return min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno

    found = []
    for node in tree.body:
        if isinstance(node, funcs) and node.name.startswith("test"):
            found.append((node.name,) + span(node))
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or node.bases):
            for sub in node.body:
                if isinstance(sub, funcs) and sub.name.startswith("test"):
                    found.append(("%s::%s" % (node.name, sub.name),) + span(sub))
    return found


def synth_py(rel, touched):
    lines = touched.get(rel, set())
    for name, start, end in py_tests(os.path.join(REPO, rel)):
        if any(start <= n <= end for n in lines):
            log("@@SYNTH_FAIL %s::%s" % (rel, name))


def python_groups(files):
    groups = {}
    for rel in files:
        base = os.path.basename(rel)
        if not rel.endswith(".py"):
            continue
        if not (base.startswith("test_") or base.endswith("_test.py")):
            continue
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        parent = os.path.dirname(path)
        project = os.path.dirname(parent) if os.path.basename(parent) in ("tests", "test") else parent
        groups.setdefault(project, []).append(path)
    return sorted(groups.items())


def pytest_cmd(paths, extra):
    return [
        PYTHON, "-m", "pytest", "-v", "-p", "no:cacheprovider", "-c", "/dev/null",
        "--rootdir", REPO, "-o", "console_output_style=classic",
    ] + extra + paths


def pyenv(project):
    env = dict(os.environ)
    env["PYTHONPATH"] = project + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def gate():
    files = wanted_files()
    rc, out, err = run([PYTHON, "-c", "import pytest; print(pytest.__version__)"], REPO, timeout=300)
    if rc != 0:
        sys.stderr.write(err)
        raise SystemExit("gate failed: pytest is not importable")
    for mod in GATE_IMPORTS:
        rc, out, err = run([PYTHON, "-c", "import %s" % mod], REPO, timeout=300)
        if rc != 0:
            sys.stderr.write(err)
            raise SystemExit("gate failed: %s is not importable" % mod)
        log("gate: import %s ok" % mod)
    for project, paths in python_groups(files):
        log("gate: collecting %s" % " ".join(paths))
        rc, out, err = run(pytest_cmd(paths, ["--collect-only", "-q"]), project, env=pyenv(project), timeout=900)
        if rc != 0:
            sys.stdout.write(out[-20000:])
            sys.stderr.write(err[-20000:])
            raise SystemExit("gate failed: pytest collection of %s" % project)
    log("DEPS_OK")


def run_all():
    files = wanted_files()
    touched = touched_lines()
    status = 0
    for project, paths in python_groups(files):
        log("##MSB-PYTEST %s" % project)
        rc, out, err = run(
            pytest_cmd(paths, ["-rA", "--continue-on-collection-errors"]),
            project,
            env=pyenv(project),
        )
        # pytest prints node ids relative to its working directory (the
        # project dir, which the suites expect); re-anchor them on the repo
        # root so names are stable and map onto test.patch paths.
        rel = os.path.relpath(project, REPO)
        if rel != ".":
            out = "".join(
                rel + "/" + line if NODEID.match(line) else line
                for line in out.splitlines(True)
            )
        sys.stdout.write(err)
        sys.stdout.write(out)
        if rc != 0:
            # A module that failed to collect (or a crashed run) prints no
            # node id for any of its tests: report its touched tests failed.
            for path in paths:
                rel_path = os.path.relpath(path, REPO)
                if not any(line.startswith(rel_path + "::") for line in out.splitlines()):
                    log("@@TARGET_COLLECT_FAILED %s" % rel_path)
                    synth_py(rel_path, touched)
        log("##MSB-PYTEST-END rc=%d" % rc)
        status = status or rc
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    if sys.argv[1:] == ["gate"]:
        gate()
    else:
        raise SystemExit(run_all())
'''

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, exact pins from the PR's uv.lock) ----------
@@PY_PROVISION@@
# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Imports pytest and every direct dependency of the suite from the venv, and
# collects any of the PR's Python test files that already exist at this commit.
python3 /home/msb_tests.py gate
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
@@APPLY@@
python3 /home/msb_tests.py run
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _test_files(test_patch: str) -> list[str]:
    """Files test.patch leaves present (added or modified, not deleted)."""
    found: dict[str, None] = {}
    current: str | None = None
    deleted = False
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            if current and not deleted:
                found[current] = None
            current = header.group(2)
            deleted = False
            continue
        if current and line.startswith("deleted file mode"):
            deleted = True
        if current and line.startswith("+++ /dev/null"):
            deleted = True
    if current and not deleted:
        found[current] = None
    return list(found)


def _added_file(patch: str, path: str) -> str:
    """Content of `path` as added (new file) by `patch`, from its '+' lines."""
    lines: list[str] = []
    inside = False
    for line in patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            inside = header.group(2) == path
            continue
        if inside and line.startswith("+") and not line.startswith("+++ "):
            lines.append(line[1:])
    return "\n".join(lines) + "\n" if lines else ""


def _uv_lock_pins(lock_text: str) -> tuple[list[str], str]:
    """Exact `name==version` pins of the runtime closure of a uv.lock's
    virtual/editable root package(s), plus its `exclude-newer` cutoff."""
    lock = tomllib.loads(lock_text)
    pkgs = {p["name"]: p for p in lock.get("package", [])}
    roots = [p for p in pkgs.values() if "virtual" in p.get("source", {}) or "editable" in p.get("source", {})]
    todo = [d["name"] for r in roots for d in r.get("dependencies", [])]
    seen: dict[str, None] = {}
    while todo:
        name = todo.pop()
        if name in seen or name not in pkgs:
            continue
        seen[name] = None
        todo.extend(d["name"] for d in pkgs[name].get("dependencies", []))
    pins = sorted(f"{n}=={pkgs[n]['version']}" for n in seen if "registry" in pkgs[n].get("source", {}))
    return pins, lock.get("options", {}).get("exclude-newer", "")


def _touched_lines(test_patch: str) -> str:
    """New-side line numbers each test.patch hunk adds, or sits next to a
    removal, in .py files (path<TAB>line rows for the driver's synthesis)."""
    rows: dict[str, None] = {}
    path: str | None = None
    new_line = 0
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith(".py") else None
            continue
        if path is None:
            continue
        hunk = _HUNK.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            rows[f"{path}\t{new_line}"] = None
            new_line += 1
        elif line.startswith("-"):
            rows[f"{path}\t{new_line}"] = None
        elif line.startswith(" ") or line == "":
            new_line += 1
    return "".join(f"{row}\n" for row in rows)


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("@@REPO@@", pr.repo)
        .replace("@@TIMEOUT@@", str(_TARGET_TIMEOUT))
        .replace("@@GATE_IMPORTS@@", _GATE_IMPORTS)
    )


class PrimeAgent2160To2160ImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_PYTHON_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
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
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Era-wide test tooling: an isolated venv with pytest (the PR's own pinned
# packages are added per PR by prepare.sh).
RUN python3 -m venv /opt/msb-venv && \\
    /opt/msb-venv/bin/pip install --no-cache-dir "pytest=={_PYTEST_VERSION}" && \\
    /opt/msb-venv/bin/python -m pytest --version

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    python3 --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PrimeAgent2160To2160ImageDefault(Image):
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
        return PrimeAgent2160To2160ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        pr = self.pr
        pins, cutoff = _uv_lock_pins(_added_file(pr.fix_patch, _PY_LOCKFILE))
        if not pins:
            raise ValueError(f"no pins parsed from {_PY_LOCKFILE} in fix.patch")
        args = " ".join(f'"{p}"' for p in pins)
        newer = f" --exclude-newer {cutoff}" if cutoff else ""
        py = (
            f"# Third-party packages this PR's Python tests import, pinned exactly as in\n"
            f"# {_PY_LOCKFILE} (added by the PR itself). Only the packages are installed\n"
            "# into the pytest venv; the tree stays at the base sha.\n"
            f'/opt/msb-venv/bin/pip install --no-cache-dir "uv=={_UV_VERSION}"\n'
            "/opt/msb-venv/bin/uv pip install --native-tls --no-cache "
            f"--python /opt/msb-venv/bin/python{newer} {args}\n"
            f"/opt/msb-venv/bin/uv pip check --python /opt/msb-venv/bin/python\n"
        )
        return (
            _render(_PREPARE, pr)
            .replace("@@SHA@@", pr.base.sha)
            .replace("@@PY_PROVISION@@", py)
        )

    def files(self) -> list[File]:
        pr = self.pr
        test_files = "".join(f"{f}\n" for f in _test_files(pr.test_patch))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "test_files.txt", test_files),
            File(".", "test_targets.txt", _touched_lines(pr.test_patch)),
            File(".", "msb_tests.py", _render(_DRIVER, pr)),
            File(".", "prepare.sh", self._prepare()),
            File(".", "run.sh", _render(_RUN, pr).replace("@@APPLY@@", "")),
            File(".", "test-run.sh", _render(_RUN, pr).replace("@@APPLY@@", _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr).replace("@@APPLY@@", _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {base.image_full_name()}

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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
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


@Instance.register(_ORG, _INTERVAL)
class PRIME_AGENT_2160_TO_2160(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PrimeAgent2160To2160ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        in_pytest = False

        for raw in clean.split("\n"):
            line = raw.rstrip()
            synth = _SYNTH.match(line)
            if synth:
                failed_tests.add(synth.group(1))
                continue
            if _MARK_END.match(line):
                in_pytest = False
                continue
            if _MARK_PYTEST.match(line):
                in_pytest = True
                continue
            if in_pytest:
                result = _PYTEST.match(line)
                if result:
                    name, status = result.group(1), result.group(2)
                    if status in ("PASSED", "XPASS"):
                        passed_tests.add(name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
