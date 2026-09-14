
import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PLUS_LINE_RE = re.compile(r"^\+\+\+ b/(\S+)\s*$", re.MULTILINE)
_TEST_FILE_RE = re.compile(r"\.test\.[cm]?[jt]sx?$")

_CHANNEL_DIRS = ("telegram", "discord", "web", "browser", "line")

_CORE_DIRS = ("agents", "commands", "auto-reply")

_CORE_SCOPE: dict[int, str] = {
    33111: "src/auto-reply",
}

_CORE_SCOPE_DEFAULT = "src/agents src/commands src/auto-reply"


_LANE_ARGS = {
    "gateway": "--config vitest.gateway.config.ts",
    "channels": "--config vitest.channels.config.ts",
    "extensions": "--config vitest.extensions.config.ts",
    "core": "--config vitest.config.ts src/agents src/commands src/auto-reply",
    "unit": "--config vitest.unit.config.ts",
}

_LANE_ORDER = ("gateway", "channels", "extensions", "core", "unit")

_JSON_FENCE_RE = re.compile(
    r"-----BEGIN_VITEST_JSON lane=(\S+)-----\n(.*?)\n-----END_VITEST_JSON-----",
    re.DOTALL,
)


def _lane_for_path(path: str) -> str:
    if path.startswith("extensions/"):
        return "extensions"
    if path.startswith("src/gateway/"):
        return "gateway"
    for directory in _CHANNEL_DIRS:
        if path.startswith(f"src/{directory}/"):
            return "channels"
    for directory in _CORE_DIRS:
        if path.startswith(f"src/{directory}/"):
            return "core"
    return "unit"


def _test_files(test_patch: str) -> list[str]:
    return sorted(
        {
            path
            for path in _PLUS_LINE_RE.findall(test_patch or "")
            if _TEST_FILE_RE.search(path)
        }
    )


def _lanes(pr: PullRequest) -> list[str]:
    wanted = {_lane_for_path(path) for path in _test_files(pr.test_patch)}
    if not wanted:
        wanted = {"unit"}
    return [lane for lane in _LANE_ORDER if lane in wanted]


class Openclaw38292To32706ImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-38292_to_32706"

    def workdir(self) -> str:
        return "base-38292_to_32706"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    DO_NOT_TRACK=1 \\
    OPENCLAW_TELEMETRY_DISABLED=1 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    NODE_OPTIONS=--max-old-space-size=4096

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
        git \\
        python3 \\
        make \\
        g++ \\
        ca-certificates \\
    && apt-get clean \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable
{global_block}
WORKDIR /home/

RUN git config --global http.version HTTP/1.1 \\
    && git config --global http.postBuffer 524288000 \\
    && for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git clone --shallow-since=2026-02-25 "${{REPO_URL}}" /home/{repo}; then break; fi; \\
        echo "clone attempt ${{attempt}} failed; retrying in 15s" >&2; \\
        sleep 15; \\
    done \\
    && test -d /home/{repo}/.git
{clear_block}
CMD ["/bin/bash"]
"""


class Openclaw38292To32706ImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return Openclaw38292To32706ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        lanes = " ".join(_lanes(self.pr))

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
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

git remote add origin https://github.com/{org}/{repo}.git 2>/dev/null || true
git rev-parse --verify --quiet "{sha}^{{commit}}" >/dev/null 2>&1 \\
    || git fetch --depth=1 origin {sha} 2>/dev/null \\
    || git fetch origin 2>/dev/null || true
git checkout -f {sha}
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true || \\
    corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true

corepack pnpm canvas:a2ui:bundle || \\
    echo "prepare: a2ui bundle failed; the suites stub it themselves" >&2

git checkout -- .
git status --porcelain
bash /home/check_git_changes.sh

corepack pnpm exec vitest --version
node -e "require('./package.json'); console.log('DEPS_OK')"
""".format(repo=repo, org=org, sha=sha),
            ),
            File(
                ".",
                "run-suites.sh",
                """#!/bin/bash
set -uo pipefail

export CI=true
cd /home/{repo}

LANES="{lanes}"

for lane in $LANES; do
    case "$lane" in
        gateway)    set -- --config vitest.gateway.config.ts ;;
        channels)   set -- --config vitest.channels.config.ts ;;
        extensions) set -- --config vitest.extensions.config.ts ;;
        core)       set -- --config vitest.config.ts {core_scope} ;;
        unit)       set -- --config vitest.unit.config.ts ;;
        *)          echo "run-suites: unknown lane '$lane'" >&2; continue ;;
    esac

    out="/home/vitest-${{lane}}.json"
    rm -f "$out"

    echo "===== openclaw-lane: ${{lane}} ====="
    corepack pnpm exec vitest run "$@" \\
        --reporter=verbose --reporter=json --outputFile="$out" \\
        --silent=passed-only \\
        || echo "===== openclaw-lane-failed: ${{lane}} ====="

    echo "-----BEGIN_VITEST_JSON lane=${{lane}}-----"
    if [ -f "$out" ]; then cat "$out"; fi
    echo
    echo "-----END_VITEST_JSON-----"
done

exit 0
""".format(repo=repo, lanes=lanes, core_scope=_CORE_SCOPE.get(self.pr.number, _CORE_SCOPE_DEFAULT)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

bash /home/run-suites.sh
""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
""".format(repo=repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
""".format(repo=repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Openclaw38292To32706ImageDefault needs an Image dependency")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("openclaw", "openclaw_38292_to_32706")
class Openclaw38292To32706(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Openclaw38292To32706ImageDefault(self.pr, self._config)

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

        repo_prefix = f"/home/{self.pr.repo}/"

        for match in _JSON_FENCE_RE.finditer(test_log):
            lane = match.group(1)
            try:
                report = json.loads(match.group(2))
            except ValueError:
                continue

            for suite in report.get("testResults") or []:
                path = suite.get("name") or ""
                if repo_prefix in path:
                    path = path.split(repo_prefix, 1)[1]

                assertions = suite.get("assertionResults") or []
                if not assertions:
                    ident = f"vitest::{lane}::{path}::<suite failed to load>"
                    if suite.get("status") == "passed":
                        passed_tests.add(ident)
                    else:
                        failed_tests.add(ident)
                    continue

                seen: dict[str, int] = {}
                for assertion in assertions:
                    name = assertion.get("fullName") or assertion.get("title") or ""
                    name = " ".join(name.split())
                    seen[name] = seen.get(name, 0) + 1
                    suffix = "" if seen[name] == 1 else f"#{seen[name]}"
                    ident = f"vitest::{lane}::{path}::{name}{suffix}"

                    status = assertion.get("status")
                    if status == "passed":
                        passed_tests.add(ident)
                    elif status == "failed":
                        failed_tests.add(ident)
                    else:
                        skipped_tests.add(ident)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
