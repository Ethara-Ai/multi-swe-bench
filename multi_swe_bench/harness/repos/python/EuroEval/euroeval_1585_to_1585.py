import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_KEY = "euroeval_1585_to_1585"
_BASE_TAG = "base"
_PY_IMAGE = "python:3.12-slim-bookworm"
_UV_VERSION = "0.12.13"

_PROXY_ARGS = "\n".join(
    [
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"',
    ]
)

_ENV_BLOCK = "\n".join(
    [
        "ENV DEBIAN_FRONTEND=noninteractive \\",
        "    LANG=C.UTF-8 \\",
        "    LC_ALL=C.UTF-8 \\",
        "    TZ=UTC \\",
        "    CI=true \\",
        "    PYTHONUNBUFFERED=1 \\",
        "    PYTHONDONTWRITEBYTECODE=1 \\",
        "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\",
        "    PIP_ROOT_USER_ACTION=ignore \\",
        "    UV_LINK_MODE=copy \\",
        "    UV_HTTP_TIMEOUT=300 \\",
        "    HF_HOME=/home/hf-cache \\",
        "    HF_HUB_DISABLE_TELEMETRY=1 \\",
        "    TOKENIZERS_PARALLELISM=false \\",
        "    OMP_NUM_THREADS=4 \\",
        "    http_proxy=${http_proxy} \\",
        "    https_proxy=${https_proxy} \\",
        "    HTTP_PROXY=${HTTP_PROXY} \\",
        "    HTTPS_PROXY=${HTTPS_PROXY} \\",
        "    no_proxy=${no_proxy} \\",
        "    NO_PROXY=${NO_PROXY} \\",
        "    SSL_CERT_FILE=${CA_CERT_PATH} \\",
        "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\",
        "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
    ]
)

_CERT_FARM = "\n".join(
    [
        "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\",
        "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt",
    ]
)

_APT_BLOCK = "\n".join(
    [
        "RUN apt-get update && apt-get install -y --no-install-recommends \\",
        "    ca-certificates curl git build-essential pkg-config \\",
        "    && rm -rf /var/lib/apt/lists/*",
    ]
)

_TOOLCHAIN_BLOCK = "\n".join(
    [
        "RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel && \\",
        '    python -m pip install --no-cache-dir "uv==' + _UV_VERSION + '"',
    ]
)


def _strip_binary_sections(patch_content: str) -> str:
    if not patch_content:
        return patch_content

    lines = patch_content.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1

    cleaned = "\n".join(result)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def _prune_block(repo: str, sha: str) -> str:
    return "\n".join(
        [
            "RUN set -eux; \\",
            "    cd /home/" + repo + "; \\",
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            "    git remote remove origin 2>/dev/null || true; \\",
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\",
            "        | xargs -r -n1 git update-ref -d; \\",
            "    git reflog expire --expire=now --all; \\",
            "    git reflog expire --expire-unreachable=now --all; \\",
            "    git gc --prune=now --aggressive; \\",
            "    git repack -a -d -l --quiet; \\",
            "    rm -f .git/objects/info/alternates; \\",
            "    git config --local gc.auto 0; \\",
            "    git config --local fetch.recurseSubmodules false; \\",
            '    git config --local remote.pushDefault ""; \\',
            '    test "$(git rev-parse HEAD)" = "' + sha + '"; \\',
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\',
            '    test -z "$(git remote)"; \\',
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"',
        ]
    )


def _submodule_block(repo: str) -> str:
    return "\n".join(
        [
            "RUN if [ -f /home/" + repo + "/.gitmodules ]; then \\",
            "        cd /home/" + repo + " && git submodule foreach --recursive ' \\",
            "            git checkout --detach HEAD; \\",
            "            git remote remove origin 2>/dev/null || true; \\",
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\',
            "                | xargs -r -n1 git update-ref -d; \\",
            "            git reflog expire --expire=now --all; \\",
            "            git reflog expire --expire-unreachable=now --all; \\",
            "            git gc --prune=now --aggressive; \\",
            "            rm -f .git/objects/info/alternates; \\",
            "        '; \\",
            "    fi",
        ]
    )


_INSTALL_DEPS_PY = r'''import os
import subprocess
import sys

ENV_FILE = "/home/py_env.sh"


def _run(command, cwd):
    sys.stdout.write("RUN " + " ".join(command) + "\n")
    sys.stdout.flush()
    return subprocess.call(command, cwd=cwd) == 0


def _has(repo_dir, name):
    return os.path.isfile(os.path.join(repo_dir, name))


def _uv_sync(repo_dir):
    base = ["uv", "sync", "--no-progress"]
    for extra in (["--frozen"], ["--locked"], []):
        if _run(base + extra, repo_dir):
            return True
    return False


def _poetry(repo_dir):
    if not _run([sys.executable, "-m", "pip", "install", "poetry"], repo_dir):
        return False
    for extra in (["--with", "dev"], []):
        if _run(["poetry", "install"] + extra, repo_dir):
            return True
    return False


def _pip_project(repo_dir):
    ok = False
    for spec in ('.[dev]', '.[test]', '.[testing]', '.'):
        if _run([sys.executable, "-m", "pip", "install", "-e", spec], repo_dir):
            ok = True
            break
    for name in sorted(os.listdir(repo_dir)):
        if name.startswith("requirements") and name.endswith(".txt"):
            _run([sys.executable, "-m", "pip", "install", "-r", name], repo_dir)
            ok = True
    req_dir = os.path.join(repo_dir, "requirements")
    if os.path.isdir(req_dir):
        for name in sorted(os.listdir(req_dir)):
            if name.endswith(".txt"):
                _run(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements/" + name],
                    repo_dir,
                )
                ok = True
    _run([sys.executable, "-m", "pip", "install", "pytest"], repo_dir)
    return ok


def _write_env(repo_dir):
    venv = os.path.join(repo_dir, ".venv")
    python_bin = os.path.join(venv, "bin", "python")
    lines = []
    if os.path.isfile(python_bin):
        lines.append("export VIRTUAL_ENV=" + venv)
        lines.append("export PATH=" + os.path.join(venv, "bin") + ":$PATH")
        lines.append("export PYTHON_BIN=" + python_bin)
    else:
        lines.append("export PYTHON_BIN=" + sys.executable)
    with open(ENV_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    sys.stdout.write("PY_ENV\n" + "\n".join(lines) + "\n")


def main():
    repo_dir = sys.argv[1]
    if _has(repo_dir, "uv.lock"):
        _uv_sync(repo_dir)
    elif _has(repo_dir, "poetry.lock"):
        _poetry(repo_dir)
    else:
        _pip_project(repo_dir)
    _write_env(repo_dir)


main()
'''

_DEPS_GATE_PY = r'''import os
import re
import subprocess
import sys


def _patch_targets(patch_path, repo_dir):
    targets = []
    if not os.path.isfile(patch_path):
        return targets
    with open(patch_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if not match:
                continue
            candidate = match.group(2)
            if not candidate.endswith(".py"):
                continue
            if os.path.isfile(os.path.join(repo_dir, candidate)):
                if candidate not in targets:
                    targets.append(candidate)
    return targets


def main():
    repo_dir = sys.argv[1]
    patch_path = sys.argv[2]
    python_bin = os.environ.get("PYTHON_BIN", sys.executable)

    version = subprocess.check_output(
        [python_bin, "-c", "import sys; print(sys.version.split()[0])"], cwd=repo_dir
    )
    sys.stdout.write("PYTHON " + version.decode().strip() + "\n")

    if subprocess.call([python_bin, "-c", "import pytest"], cwd=repo_dir) != 0:
        raise SystemExit("deps_gate: pytest is not importable")

    targets = _patch_targets(patch_path, repo_dir)
    command = [
        python_bin,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--no-header",
        "--color=no",
        "-p",
        "no:cacheprovider",
    ] + targets
    code = subprocess.call(command, cwd=repo_dir)
    if code not in (0, 5):
        raise SystemExit("deps_gate: collection failed (exit " + str(code) + ")")

    sys.stdout.write("DEPS_OK\n")


main()
'''


class EuroEval1585To1585ImageBase(Image):
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
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        source = "https://github.com/" + org + "/" + repo

        if self.config.need_clone:
            acquire = "\n".join(
                [
                    'RUN git clone "${REPO_URL}" /home/' + repo + " && \\",
                    "    cd /home/" + repo + " && git rev-parse HEAD >/dev/null",
                ]
            )
        else:
            acquire = "COPY " + repo + " /home/" + repo

        lines = [
            "# syntax=docker/dockerfile:1.6",
            "",
            "FROM " + _PY_IMAGE,
            "",
            "ARG TARGETARCH",
            'ARG REPO_URL="' + source + '.git"',
            "ARG BASE_COMMIT",
            "",
            _PROXY_ARGS,
            "",
            _ENV_BLOCK,
            "",
            self.global_env,
            "",
            'LABEL org.opencontainers.image.title="' + org + "/" + repo + '" \\',
            '      org.opencontainers.image.description="'
            + org
            + "/"
            + repo
            + ' Docker image" \\',
            '      org.opencontainers.image.source="' + source + '" \\',
            '      org.opencontainers.image.authors="https://www.ethara.ai/"',
            "",
            _CERT_FARM,
            "",
            _APT_BLOCK,
            "",
            _TOOLCHAIN_BLOCK,
            "",
            "RUN git config --global --add safe.directory '*'",
            "",
            "WORKDIR /home/",
            "",
            acquire,
            "",
            self.clear_env,
            "",
            'CMD ["/bin/bash"]',
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


class EuroEval1585To1585ImageDefault(Image):
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
        return EuroEval1585To1585ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _repo_dir(self) -> str:
        return "/home/" + _safe_path_component(self.pr.repo)

    def _test_command(self) -> list[str]:
        repo_dir = self._repo_dir()
        return [
            "export CI=true",
            "export PYTHONUNBUFFERED=1",
            ". /home/py_env.sh",
            "cd " + repo_dir,
            '"$PYTHON_BIN" -m pytest -rA --no-header --color=no -p no:cacheprovider'
            " --continue-on-collection-errors",
            "",
        ]

    def files(self) -> list[File]:
        repo_dir = self._repo_dir()
        sha = self.pr.base.sha

        check_git_changes = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "",
                "if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then",
                '    echo "check_git_changes: not a git repository" >&2',
                "    exit 1",
                "fi",
                "",
                'if [ -n "$(git status --porcelain)" ]; then',
                '    echo "check_git_changes: working tree is dirty" >&2',
                "    git status --porcelain >&2",
                "    exit 1",
                "fi",
                "",
                'echo "check_git_changes: clean"',
                "",
            ]
        )

        prepare = "\n".join(
            [
                "#!/bin/bash",
                "set -e",
                "",
                "cd " + repo_dir,
                "git reset --hard",
                "git clean -fdx",
                "bash /home/check_git_changes.sh",
                'if ! git cat-file -e "' + sha + '^{commit}" 2>/dev/null; then',
                '    git fetch --no-tags origin "' + sha + '"',
                "fi",
                'git checkout --detach "' + sha + '"',
                "bash /home/check_git_changes.sh",
                "",
                "export CI=true",
                "export UV_LINK_MODE=copy",
                "export PIP_DISABLE_PIP_VERSION_CHECK=1",
                "export PIP_ROOT_USER_ACTION=ignore",
                'mkdir -p "${HF_HOME:-/home/hf-cache}"',
                "",
                "python /home/install_deps.py " + repo_dir + " || true",
                "",
                ". /home/py_env.sh",
                '"$PYTHON_BIN" -m pytest -rA --no-header --color=no -p no:cacheprovider || true',
                "",
                "python /home/deps_gate.py " + repo_dir + " /home/test.patch",
                "",
            ]
        )

        run_sh = "\n".join(
            ["#!/bin/bash", "set -eo pipefail", ""] + self._test_command()
        )

        test_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch",
                "",
            ]
            + self._test_command()
        )

        fix_run_sh = "\n".join(
            [
                "#!/bin/bash",
                "set -eo pipefail",
                "",
                "cd " + repo_dir,
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                "",
            ]
            + self._test_command()
        )

        return [
            File(".", "fix.patch", _strip_binary_sections(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_sections(self.pr.test_patch)),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "install_deps.py", _INSTALL_DEPS_PY),
            File(".", "deps_gate.py", _DEPS_GATE_PY),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            "COPY " + file.name + " /home/" for file in self.files()
        )

        lines = [
            "FROM " + name + ":" + tag,
            "",
            self.global_env,
            "",
            copy_commands,
            "",
            "WORKDIR /home/" + repo,
            "",
            "RUN git reset --hard",
            "RUN git checkout " + sha,
            "",
            "RUN bash /home/prepare.sh",
            "",
            _prune_block(repo, sha),
            "",
            _submodule_block(repo),
            "",
            self.clear_env,
            "",
        ]

        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def _parse_pytest_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log or "")

    progress = re.compile(
        r"(\S+\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)"
    )
    summary = re.compile(
        r"^(?:\[[^\]]*\]\s*)*(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+\.py::\S+)"
    )

    def record(status: str, name: str) -> None:
        if not name:
            return
        if status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status in ("SKIPPED", "XFAIL"):
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

    for raw_line in clean_log.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = summary.match(line)
        if match:
            record(match.group(1), match.group(2))
            continue

        match = progress.search(line)
        if match:
            record(match.group(2), match.group(1))

    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    passed_tests -= skipped_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class EUROEVAL_1585_TO_1585(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EuroEval1585To1585ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return _parse_pytest_log(log)


for _org, _name in (("EuroEval", _ERA_KEY), ("EuroEval", "EuroEval")):
    Instance.register(_org, _name)(EUROEVAL_1585_TO_1585)
