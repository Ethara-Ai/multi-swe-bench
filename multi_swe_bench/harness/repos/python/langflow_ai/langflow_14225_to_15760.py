import json
import re
import shlex
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL = "langflow_14225_to_15760"
_PYTHON_IMAGE = "python:3.13-bookworm"
_NODE_IMAGE = "node:22-bookworm"
_UV_VERSION = "0.13.6"
_FRONTEND = "src/frontend"
_SUITE_BEGIN = "langflow-suite-begin"
_SUITE_END = "langflow-suite-end"
_REPORT_BEGIN = "langflow-report-begin"
_REPORT_END = "langflow-report-end"
_SUITE_TIMEOUT = 14400
_PYTEST_WORKERS = 4

_PYTEST_FILE = re.compile(r"^(?:.+/)?(?:test_[^/]+|[^/]+_test)\.py$")
_JEST_FILE = re.compile(r"^src/frontend/(?P<rel>src/.+\.(?:test|spec)\.tsx?)$")
_PROJECT = re.compile(
    r"^(?P<project>src/lfx|src/bundles/[^/]+|src/backend/base|src/langflow-stepflow|src/sdk)/(?P<rel>.+)$"
)

_SYNC_HELPERS = f"""sync_python() {{
  local extras=()
  if grep -q '^bundles = \\[' pyproject.toml; then
    extras=(--extra bundles)
  fi
  uv sync --frozen --all-packages "${{extras[@]}}"
}}

sync_frontend() {{
  (cd {_FRONTEND} && npm ci --no-audit --no-fund)
}}"""

_RUN_HELPERS = f"""REPORT=/tmp/langflow-report

emit_report() {{
  echo "{_REPORT_BEGIN}"
  if [ -f "$REPORT" ]; then
    cat "$REPORT"
    echo
  fi
  echo "{_REPORT_END}"
}}

present() {{
  local base="$1" file
  shift
  for file in "$@"; do
    if [ -f "$base/$file" ]; then
      printf '%s\\n' "$file"
    fi
  done
}}

reconcile_deps() {{
  if [ -n "$(git status --porcelain -- uv.lock '*pyproject.toml')" ]; then
    sync_python
  fi
  if [ -d {_FRONTEND}/node_modules ] && [ -n "$(git status --porcelain -- {_FRONTEND}/package-lock.json)" ]; then
    sync_frontend
  fi
}}

run_pytest() {{
  local project="$1" status=0
  shift
  local files=() dist=() select=() rerun=()
  mapfile -t files < <(present "$project" "$@")
  if python -c "import pytest_rerunfailures" > /dev/null 2>&1; then
    rerun=(--reruns 2)
  fi
  dist=(-n {_PYTEST_WORKERS} --dist loadfile)
  case "$project" in
    .|src/bundles/*)
      select=(-m "not api_key_required")
      ;;
  esac
  rm -f "$REPORT"
  if [ "${{#files[@]}}" -gt 0 ]; then
    (cd "$project" && timeout -k 60 {_SUITE_TIMEOUT} python -m pytest -p no:cacheprovider -o log_cli=false --rootdir "$PWD" -o timeout_method=thread -o timeout=300 --max-worker-restart=8 --continue-on-collection-errors --junitxml "$REPORT" "${{select[@]}}" "${{dist[@]}}" "${{rerun[@]}}" "${{files[@]}}") || status=$?
  fi
  emit_report
  return "$status"
}}

run_jest() {{
  local status=0
  local files=()
  mapfile -t files < <(present {_FRONTEND} "$@")
  rm -f "$REPORT"
  if [ "${{#files[@]}}" -gt 0 ]; then
    (cd {_FRONTEND} && timeout -k 60 {_SUITE_TIMEOUT} node_modules/.bin/jest --ci --silent --maxWorkers=4 --json --outputFile "$REPORT" --runTestsByPath "${{files[@]}}") || status=$?
  fi
  emit_report
  return "$status"
}}"""


def _venv(repo: str) -> str:
    return f"/opt/{repo}-venv"


def _run_env(repo: str) -> str:
    return f"""export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export DO_NOT_TRACK=true
export LFX_TEST_ALLOW_LANGFLOW=1
export LANGFLOW_TEST_HARD_TIMEOUT=600
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export UV_PROJECT_ENVIRONMENT={_venv(repo)}
export VIRTUAL_ENV={_venv(repo)}
export PATH={_venv(repo)}/bin:$PATH
export NODE_OPTIONS=--max-old-space-size=4096
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PUPPETEER_SKIP_DOWNLOAD=1
export CYPRESS_INSTALL_BINARY=0"""


def _suites(test_patch: str) -> list[tuple[str, str, list[str]]]:
    groups: dict[tuple[str, str], set[str]] = {}
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)", test_patch, re.M):
        found = _JEST_FILE.match(path)
        if found:
            groups.setdefault(("jest", _FRONTEND), set()).add(found.group("rel"))
            continue
        if not _PYTEST_FILE.match(path):
            continue
        found = _PROJECT.match(path)
        if found:
            groups.setdefault(("pytest", found.group("project")), set()).add(found.group("rel"))
        else:
            groups.setdefault(("pytest", "."), set()).add(path)
    return [(kind, project, sorted(files)) for (kind, project), files in sorted(groups.items())]


def _test_commands(suites: list[tuple[str, str, list[str]]]) -> str:
    lines = ["rc=0"]
    for kind, project, files in suites:
        targets = " ".join(shlex.quote(file) for file in files)
        lines.append(f'echo "{_SUITE_BEGIN} {kind}:{project}"')
        if kind == "jest":
            lines.append(f"run_jest {targets} || rc=$?")
        else:
            lines.append(f"run_pytest {shlex.quote(project)} {targets} || rc=$?")
        lines.append(f'echo "{_SUITE_END}"')
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _provision(suites: list[tuple[str, str, list[str]]]) -> str:
    lines = ["sync_python"]
    if any(kind == "jest" for kind, _, _ in suites):
        lines.append("sync_frontend")
    return "\n".join(lines)


def _gate(suites: list[tuple[str, str, list[str]]]) -> str:
    lines = [
        'python -c "import langflow, lfx, pytest, xdist, pytest_timeout, pytest_asyncio"',
        "python -m pytest --version",
    ]
    if any(kind == "jest" for kind, _, _ in suites):
        lines.append(f"(cd {_FRONTEND} && node_modules/.bin/jest --version)")
    return "\n".join(lines)


def _pytest_modules(suites: list[tuple[str, str, list[str]]]) -> dict[str, dict[str, str]]:
    modules: dict[str, dict[str, str]] = {}
    for kind, project, files in suites:
        if kind != "pytest":
            continue
        modules[project] = {file[: -len(".py")].replace("/", "."): file for file in files}
    return modules


def _split_classname(classname: str, modules: dict[str, str]) -> Optional[tuple[str, list[str]]]:
    best = ""
    for module in modules:
        if (classname == module or classname.startswith(module + ".")) and len(module) > len(best):
            best = module
    if not best:
        return None
    rest = classname[len(best) + 1 :]
    return modules[best], [part for part in rest.split(".") if part]


def _pytest_cases(xml_text: str, project: str, modules: dict[str, str]) -> list[tuple[str, str]]:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    prefix = "" if project == "." else f"{project}/"
    cases: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            status = "failed"
        elif "skipped" in tags:
            status = "skipped"
        else:
            status = "passed"
        if classname:
            located = _split_classname(classname, modules)
            if located is None:
                continue
            file, classes = located
            cases.append(("::".join([prefix + file, *classes, name]), status))
        elif name in modules:
            cases.append((prefix + modules[name], status))
    return cases


def _jest_cases(json_text: str, root: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(json_text.strip())
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    cases: list[tuple[str, str]] = []
    for result in data.get("testResults") or []:
        path = result.get("name") or result.get("testFilePath") or ""
        if path.startswith(root):
            path = path[len(root) :]
        assertions = result.get("assertionResults") or []
        if not assertions:
            if result.get("status") == "failed":
                cases.append((path, "failed"))
            continue
        for assertion in assertions:
            titles = [*(assertion.get("ancestorTitles") or []), assertion.get("title") or ""]
            parts = [re.sub(r"\s+", " ", title).strip() for title in titles]
            name = " > ".join([path, *[part for part in parts if part]])
            status = assertion.get("status")
            if status == "passed":
                cases.append((name, "passed"))
            elif status == "failed":
                cases.append((name, "failed"))
            else:
                cases.append((name, "skipped"))
    return cases


class Langflow14225To15760ImageBase(Image):
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

FROM --platform=$BUILDPLATFORM {_PYTHON_IMAGE} AS repo-src
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*' && \\
    git clone "${{REPO_URL}}" /src/{self.pr.repo} && \\
    cd /src/{self.pr.repo} && git rev-parse HEAD >/dev/null

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PIP_CERT=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    UV_NATIVE_TLS=1 \\
    UV_PYTHON=/usr/local/bin/python3.13 \\
    UV_PYTHON_DOWNLOADS=never \\
    UV_LINK_MODE=copy \\
    UV_HTTP_TIMEOUT=300 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

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
    git ca-certificates curl build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version && npx --version

RUN pip install --no-cache-dir "uv=={_UV_VERSION}" && \\
    uv --version && python3.13 --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

COPY --from=repo-src /src/{self.pr.repo} /home/{self.pr.repo}

ENV UV_PROJECT_ENVIRONMENT={_venv(self.pr.repo)}

RUN --mount=type=cache,id=langflow-uv-cache-${{TARGETARCH}},target=/root/.cache/uv,sharing=shared \\
    cd /home/{self.pr.repo} && \\
    if grep -q '^bundles = \\[' pyproject.toml; then extras="--extra bundles"; else extras=""; fi && \\
    (uv sync --frozen --all-packages $extras || true)

CMD ["/bin/bash"]
"""


class Langflow14225To15760ImageDefault(Image):
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
        return Langflow14225To15760ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        suites = _suites(self.pr.test_patch)
        run_env = _run_env(repo)
        test_commands = _test_commands(suites)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{run_env}

{_SYNC_HELPERS}

{_provision(suites)}

{_gate(suites)}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{run_env}

{_SYNC_HELPERS}

{_RUN_HELPERS}

cd /home/{repo}
reconcile_deps
{test_commands}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{run_env}

{_SYNC_HELPERS}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
reconcile_deps
{test_commands}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{run_env}

{_SYNC_HELPERS}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
reconcile_deps
{test_commands}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {base.image_full_name()}

ARG TARGETARCH

{copy_commands}
RUN --mount=type=cache,id=langflow-uv-cache-${{TARGETARCH}},target=/root/.cache/uv,sharing=shared \\
    --mount=type=cache,id=langflow-npm-cache-${{TARGETARCH}},target=/root/.npm,sharing=shared \\
    bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("langflow-ai", "langflow")
@Instance.register("langflow-ai", _INTERVAL)
class LANGFLOW_14225_TO_15760(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Langflow14225To15760ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        modules = _pytest_modules(_suites(self.pr.test_patch))
        root = f"/home/{self.pr.repo}/"
        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        begin = re.compile(rf"^{re.escape(_SUITE_BEGIN)} (\S+)\s*$")

        suite = ""
        report: Optional[list[str]] = None
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            if report is not None:
                if line.strip() == _REPORT_END:
                    kind, _, project = suite.partition(":")
                    text = "\n".join(report)
                    if kind == "jest":
                        cases = _jest_cases(text, root)
                    else:
                        cases = _pytest_cases(text, project, modules.get(project, {}))
                    for name, status in cases:
                        if status == "passed":
                            passed_tests.add(name)
                        elif status == "failed":
                            failed_tests.add(name)
                        else:
                            skipped_tests.add(name)
                    report = None
                else:
                    report.append(line)
                continue
            stripped = line.strip()
            found = begin.match(stripped)
            if found:
                suite = found.group(1)
                continue
            if stripped == _SUITE_END:
                suite = ""
                continue
            if stripped == _REPORT_BEGIN and suite:
                report = []

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
