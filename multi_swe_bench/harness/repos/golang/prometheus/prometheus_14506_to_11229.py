from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "14506_to_11229"
_GO_IMAGE = "golang:1.22.5-bookworm"
_NODE_IMAGE = "node:16.14.2-bullseye"
_MODULE_PREFIX = "github.com/prometheus/prometheus/"
_GO_TEST_CMD = "go test -json -count=1 -timeout=60m ./... 2>&1"
_UI_DIR = "web/ui"
_UI_WORKSPACES = ("module/lezer-promql", "module/codemirror-promql", "react-app")
_WORKSPACE_MARKER = "##WORKSPACE## "

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PATCH_PATH_RE = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)
_GO_PKG_LINE_RE = re.compile(r"^(ok|FAIL|\?)\s+(\S+)(?:\s|$)")
_GO_HOST_PORT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:]*\]|localhost):\d+")
_JEST_FILE_RE = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+\.(?:tsx|ts|jsx|js|mjs|cjs))(?:\s|$)")
_JEST_MARK_RE = re.compile(r"^(\s*)([✓✔√]|[✕✗×]|[○◌]|✎)(?:\s+(.*?))?\s*$")
_JEST_SKIP_BLOCK_RE = re.compile(r"^(\s*)(?:●|console\.\w+\s*$)")
_JEST_DESC_RE = re.compile(r"^(\s+)(\S.*?)\s*$")
_JEST_TIME_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")
_JEST_PASS_MARKS = "✓✔√"
_JEST_FAIL_MARKS = "✕✗×"

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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
"""


def _uses_node(pr: PullRequest) -> bool:
    paths = _PATCH_PATH_RE.findall(pr.test_patch)
    return bool(paths) and all(p.startswith(f"{_UI_DIR}/") for p in paths)


def _go_prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

go version
go env GOTOOLCHAIN GOPATH GOMODCACHE

go mod download || true
go build ./... || {{ echo "prepare.sh: go build failed at the base commit"; exit 1; }}
go test -count=1 -run '^$' ./... || {{ echo "prepare.sh: a test package failed to compile at the base commit"; exit 1; }}
"""


def _node_prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

node --version
npm --version

cd /home/{pr.repo}/{_UI_DIR}
npm ci || {{ echo "prepare.sh: npm ci failed in {_UI_DIR}"; exit 1; }}
npm run build:module || {{ echo "prepare.sh: building lezer-promql and codemirror-promql failed"; exit 1; }}
test -f module/lezer-promql/src/parser.js || {{ echo "prepare.sh: lezer-promql src/parser.js was not generated"; exit 1; }}
test -f module/lezer-promql/dist/index.d.ts || {{ echo "prepare.sh: lezer-promql dist/index.d.ts was not generated"; exit 1; }}
test -f module/codemirror-promql/dist/cjs/index.js || {{ echo "prepare.sh: codemirror-promql dist/cjs/index.js was not built"; exit 1; }}
test -x node_modules/.bin/react-scripts || {{ echo "prepare.sh: react-scripts was not installed"; exit 1; }}
test -x node_modules/.bin/jest || {{ echo "prepare.sh: jest was not installed"; exit 1; }}
"""


def _go_run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
{apply_line}{_GO_TEST_CMD}
"""


def _node_run_sh(pr: PullRequest, apply_line: str) -> str:
    workspaces = " ".join(_UI_WORKSPACES)
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
{apply_line}cd /home/{pr.repo}/{_UI_DIR}
rc=0
for ws in {workspaces}; do
  echo "{_WORKSPACE_MARKER}{_UI_DIR}/$ws"
  (cd "$ws" && npm run test -- --verbose 2>&1) || rc=1
done
exit $rc
"""


def _go_package(name: str) -> str:
    if name.startswith(_MODULE_PREFIX):
        return name[len(_MODULE_PREFIX) :]
    if name == _MODULE_PREFIX.rstrip("/"):
        return "."
    return name


def _is_repo_package(name: str) -> bool:
    return name.startswith(_MODULE_PREFIX) or name == _MODULE_PREFIX.rstrip("/")


def _parse_go_json_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    buckets = {"pass": passed, "fail": failed, "skip": skipped}
    markers = {"ok": passed, "FAIL": failed, "?": skipped}

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            m = _GO_PKG_LINE_RE.match(line)
            if m and _is_repo_package(m.group(2)):
                markers[m.group(1)].add(_go_package(m.group(2)))
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        action = event.get("Action")
        package = event.get("Package") or ""
        if action not in buckets or not _is_repo_package(package):
            continue

        test = event.get("Test")
        if test:
            test = _GO_HOST_PORT_RE.sub(r"\1:PORT", test)
        ident =f"{_go_package(package)}::{test}" if test else _go_package(package)
        buckets[action].add(ident)

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


def _parse_jest_workspaces_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    seen: dict[str, int] = {}

    workspace = ""
    cur_file = ""
    stack: list[tuple[int, str]] = []
    skip_indent: int | None = None

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.rstrip()

        if line.startswith(_WORKSPACE_MARKER):
            workspace = line[len(_WORKSPACE_MARKER) :].strip()
            cur_file, stack, skip_indent = "", [], None
            continue

        m = _JEST_FILE_RE.match(line)
        if m:
            cur_file, stack, skip_indent = m.group(1), [], None
            continue

        if line.startswith(("Test Suites:", "Tests:")):
            cur_file, stack, skip_indent = "", [], None
            continue

        if not cur_file or not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None

        m = _JEST_SKIP_BLOCK_RE.match(line)
        if m:
            skip_indent = len(m.group(1))
            continue

        m = _JEST_MARK_RE.match(line)
        if m:
            mark, name = m.group(2), _JEST_TIME_RE.sub("", m.group(3) or "").strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if mark in "○◌" and name.startswith("skipped "):
                name = name[len("skipped ") :]
            elif mark == "✎" and name.startswith("todo "):
                name = name[len("todo ") :]
            path = " > ".join([t for _, t in stack] + [name])
            prefix = f"{workspace}/{cur_file}" if workspace else cur_file
            ident = f"{prefix}::{path}"
            seen[ident] = seen.get(ident, 0) + 1
            if seen[ident] > 1:
                ident = f"{ident} #{seen[ident]}"
            if mark in _JEST_PASS_MARKS:
                passed.add(ident)
            elif mark in _JEST_FAIL_MARKS:
                failed.add(ident)
            else:
                skipped.add(ident)
            continue

        m = _JEST_DESC_RE.match(line)
        if m:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))

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


class PrometheusImageBase_PROMETHEUS_14506_TO_11229(Image):
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
        return _NODE_IMAGE if _uses_node(self.pr) else _GO_IMAGE

    def image_tag(self) -> str:
        toolchain = "node" if _uses_node(self.pr) else "go"
        return f"base-{toolchain}-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        toolchain_env = "" if _uses_node(self.pr) else "ENV GOTOOLCHAIN=local\n\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until


RUN git --version

{toolchain_env}RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class PrometheusImageDefault_PROMETHEUS_14506_TO_11229(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return PrometheusImageBase_PROMETHEUS_14506_TO_11229(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if _uses_node(self.pr):
            prepare, run_sh = _node_prepare_sh(self.pr), _node_run_sh
        else:
            prepare, run_sh = _go_prepare_sh(self.pr), _go_run_sh

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run_sh(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                run_sh(self.pr, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                run_sh(
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "PrometheusImageDefault_PROMETHEUS_14506_TO_11229 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("prometheus", "prometheus_14506_to_11229")
class PROMETHEUS_14506_TO_11229(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PrometheusImageDefault_PROMETHEUS_14506_TO_11229(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        if _uses_node(self.pr):
            return _parse_jest_workspaces_log(test_log)
        return _parse_go_json_log(test_log)
