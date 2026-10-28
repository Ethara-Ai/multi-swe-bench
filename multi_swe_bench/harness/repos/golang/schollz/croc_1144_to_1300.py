import json
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "croc_1144_to_1300"
_BASE_TAG = "base-1144_to_1300"
_GO_IMAGE = "golang:1.27.3-trixie"
_NODE_VERSION = "24.22.1"
_WEB_DIR = "web"
_VITEST_RESULTS = "/tmp/vitest-results.json"
_VITEST_TIMEOUT_MS = 60000
_GO_PACKAGES = "$(go list -e ./... | grep -v /node_modules/)"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_VOLATILE_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")
_GLOB_SPECIAL = re.compile(r"([*?\[\\])")
_QUOTED_PAIR = re.compile(r'^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")$')
_FULL_INDEX = re.compile(r"^index [0-9a-f]{40}\.\.[0-9a-f]{40}(?: |$)")
_GO_ACTIONS = {"pass", "fail", "skip"}
_VITEST_STATUS = {
    "passed": "pass",
    "failed": "fail",
    "skipped": "skip",
    "pending": "skip",
    "todo": "skip",
    "disabled": "skip",
}

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_VITEST_EVENTS = """const fs = require("fs");
const path = require("path");

const [resultsFile, root] = process.argv.slice(2);
const report = JSON.parse(fs.readFileSync(resultsFile, "utf8"));
for (const file of report.testResults || []) {
  const relative = path.relative(root, file.name).split(path.sep).join("/");
  for (const assertion of file.assertionResults || []) {
    const name = [relative, ...(assertion.ancestorTitles || []), assertion.title].join(" > ");
    process.stdout.write(JSON.stringify({ Vitest: name, Status: assertion.status }) + "\\n");
  }
}
"""


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _prepare_script(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e "{sha}^{{commit}}"; then
    git fetch --no-tags origin "{sha}"
fi
git checkout --detach {sha}
bash /home/check_git_changes.sh

go mod download
if [ -f {_WEB_DIR}/package-lock.json ]; then
    npm --prefix {_WEB_DIR} ci --no-audit --no-fund
fi

go build {_GO_PACKAGES}
go test -count=1 -run '^$' {_GO_PACKAGES}
if [ -f {_WEB_DIR}/package-lock.json ]; then
    {_WEB_DIR}/node_modules/.bin/vitest --version
fi
echo DEPS_OK
"""


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return f"""#!/bin/bash
set -eo pipefail
export CI=true

{setup}

status=0
go test -p 1 -count=1 -json {_GO_PACKAGES} 2>&1 | tr -cd '\\11\\12\\15\\40-\\176' || status=$?

if [ -f {_WEB_DIR}/package-lock.json ]; then
    rm -f {_VITEST_RESULTS}
    (cd {_WEB_DIR} && node_modules/.bin/vitest run --testTimeout={_VITEST_TIMEOUT_MS} --reporter=json --outputFile={_VITEST_RESULTS}) 2>&1 || status=$?
    node /home/vitest_events.cjs {_VITEST_RESULTS} /home/{repo} || status=$?
fi

exit $status
"""


def _unquote(path: str) -> str:
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    text = path[1:-1].encode("ascii", "backslashreplace").decode("unicode_escape")
    return text.encode("latin-1").decode("utf-8", "replace")


def _strip_prefix(path: str) -> str:
    path = _unquote(path)
    return path[2:] if path[:2] in ("a/", "b/") else path


def _header_paths(header: str) -> list[str]:
    quoted = _QUOTED_PAIR.match(header)
    if quoted:
        return [_strip_prefix(quoted.group(1)), _strip_prefix(quoted.group(2))]
    middle = len(header) // 2
    left, right = header[:middle], header[middle + 1 :]
    if header[middle : middle + 1] == " " and left[2:] == right[2:]:
        return [_strip_prefix(left), _strip_prefix(right)]
    return []


def _binary_paths(*patches: str) -> list[str]:
    found: dict[str, None] = {}
    for patch in patches:
        paths: list[str] = []
        full_index = False
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                paths = _header_paths(line[len("diff --git ") :])
                full_index = False
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                paths.append(_unquote(line.split(" ", 2)[2]))
            elif _FULL_INDEX.match(line):
                full_index = True
            elif line.startswith("Binary files ") and line.endswith(" differ"):
                found.update(dict.fromkeys(paths))
            elif line == "GIT binary patch" and not full_index:
                found.update(dict.fromkeys(paths))
    return list(found)


def _apply_command(excluded: list[str], *patch_files: str) -> str:
    flags = [
        "--exclude=" + shlex.quote(_GLOB_SPECIAL.sub(r"\\\1", path))
        for path in excluded
    ]
    return " ".join(["git apply --whitespace=nowarn", *flags, *patch_files])


def _package_key(package: str, org: str, repo: str) -> str:
    prefix = re.compile(
        rf"^github\.com/{re.escape(org)}/{re.escape(repo)}(?:/v\d+)?(?=/|$)",
        re.IGNORECASE,
    )
    return prefix.sub("", package).lstrip("/") or "."


def _test_event(event: dict, org: str, repo: str) -> tuple[str, str] | None:
    vitest = event.get("Vitest")
    if isinstance(vitest, str) and vitest:
        status = _VITEST_STATUS.get(event.get("Status"))
        return (vitest, status) if status else None
    package = event.get("Package")
    test = event.get("Test")
    action = event.get("Action")
    if not isinstance(package, str) or not isinstance(test, str):
        return None
    if not package or not test or action not in _GO_ACTIONS:
        return None
    key = _package_key(package, org, repo)
    return f"{key}::{_VOLATILE_ADDR.sub('0x<addr>', test)}", action


class Croc1144To1300ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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

ENV CGO_ENABLED=1 \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-buildvcs=false \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{node_arch}}.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1 --no-same-owner

RUN git config --global --add safe.directory '*'"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class Croc1144To1300ImageDefault(Image):
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
        return Croc1144To1300ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = _apply_command(
            _binary_paths(self.pr.test_patch),
            "/home/test.patch",
        )
        fix_apply = _apply_command(
            _binary_paths(self.pr.test_patch, self.pr.fix_patch),
            "/home/test.patch",
            "/home/fix.patch",
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(repo, self.pr.base.sha)),
            File(".", "vitest_events.cjs", _VITEST_EVENTS),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register("schollz", _INTERVAL)
class CROC_1144_TO_1300(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Croc1144To1300ImageDefault(self.pr, self._config)

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
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        for line in _ANSI.sub("", test_log).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            result = _test_event(event, self.pr.org, self.pr.repo)
            if result:
                buckets[result[1]].add(result[0])
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
