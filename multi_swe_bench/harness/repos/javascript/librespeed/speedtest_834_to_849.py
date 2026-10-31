import json
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "speedtest_834_to_849"
_BASE_TAG = "base-834_to_849"
_NODE_IMAGE = "node:22.23.4-bookworm"
_HARNESS_CONFIG = "/home/playwright_harness.config.js"
_CASE_REPORTER = "/home/case_reporter.js"
_SPECS = "static-repository-assets.spec.js"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BINARY_DIFF = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.M)
_GLOB_SPECIAL = re.compile(r"([*?\[\\])")
_RESULTS = {"pass", "fail", "skip"}

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

_PLAYWRIGHT_HARNESS_CONFIG = """const path = require("node:path");

const root = process.cwd();
const base = require(path.join(root, "playwright.config.js"));

module.exports = {
  ...base,
  testDir: path.join(root, "tests", "e2e"),
  testMatch: process.env.PLAYWRIGHT_SPECS,
  globalSetup: undefined,
  globalTeardown: undefined,
  retries: 0,
  reporter: [[process.env.PLAYWRIGHT_REPORTER]],
  webServer: base.webServer ? { ...base.webServer, cwd: root } : undefined,
};
"""

_CASE_REPORTER_JS = """class CaseReporter {
  onBegin(config, suite) {
    this.suite = suite;
  }

  onEnd() {
    const results = { expected: "pass", flaky: "pass", unexpected: "fail", skipped: "skip" };
    for (const test of this.suite.allTests()) {
      const titles = test.titlePath().filter(Boolean).slice(1);
      const name = titles.join(" > ");
      process.stdout.write(JSON.stringify({ Case: name, Result: results[test.outcome()] }) + "\\n");
    }
  }

  printsToStdio() {
    return true;
  }
}

module.exports = CaseReporter;
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

git checkout --detach {sha}
bash /home/check_git_changes.sh

npm ci

node -e "require('@playwright/test'); require('./playwright.config.js')"
"""


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return f"""#!/bin/bash
set -eo pipefail
export CI=true

cat > {_HARNESS_CONFIG} <<'EOF'
{_PLAYWRIGHT_HARNESS_CONFIG}EOF

cat > {_CASE_REPORTER} <<'EOF'
{_CASE_REPORTER_JS}EOF

{setup}

export PLAYWRIGHT_SPECS={_SPECS}
export PLAYWRIGHT_REPORTER={_CASE_REPORTER}

npx playwright test --config {_HARNESS_CONFIG}

"""


def _binary_paths(*patches: str) -> list[str]:
    found: dict[str, None] = {}
    for patch in patches:
        for old, new in _BINARY_DIFF.findall(patch):
            found[new or old] = None
    return list(found)


def _apply_command(excluded: list[str], *patch_files: str) -> str:
    flags = [
        "--exclude=" + shlex.quote(_GLOB_SPECIAL.sub(r"\\\1", path))
        for path in excluded
    ]
    return " ".join(["git apply --whitespace=nowarn", *flags, *patch_files])


def _test_event(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    case = event.get("Case")
    if isinstance(case, str) and case and event.get("Result") in _RESULTS:
        return case, event["Result"]
    return None


class Speedtest834To849ImageBase(Image):
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
        return _NODE_IMAGE

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
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


class Speedtest834To849ImageDefault(Image):
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
        return Speedtest834To849ImageBase(self.pr, self._config)

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


@Instance.register("librespeed", _INTERVAL)
class SPEEDTEST_834_TO_849(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Speedtest834To849ImageDefault(self.pr, self._config)

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
            event = _test_event(line)
            if event:
                buckets[event[1]].add(event[0])
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
