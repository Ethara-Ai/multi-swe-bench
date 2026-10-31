import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "isomorphic-git"
REPO = "isomorphic-git"

_INTERVAL_NAME = "isomorphic_git_2461_to_2369"

_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-node22-npm"
_REPO_DIR = f"/home/{REPO}"

_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

SHEBANG = "#!/bin/bash\nset -eo pipefail"

CHECK_GIT_CHANGES = """#!/bin/bash
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

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

RUN_ENV = """export CI=true
export TEST_ENV=node
export NODE_OPTIONS="--experimental-vm-modules --max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false"""

INSTALL = "npm ci || npm install"

BUILD = "node_modules/.bin/rollup -c --no-treeshake"

SERVERS = """wait_port() {
    local port="$1"
    local i=0
    while [ "$i" -lt 30 ]; do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    return 1
}

start_servers() {
    GIT_HTTP_MOCK_SERVER_PORT=8888 \\
    GIT_HTTP_MOCK_SERVER_ROOT=__tests__/__fixtures__ \\
        nohup node_modules/.bin/git-http-mock-server > /tmp/git-http-mock-server.log 2>&1 &
    nohup node_modules/.bin/cors-proxy run > /tmp/cors-proxy.log 2>&1 &
    wait_port 8888 || echo "start_servers: git-http-mock-server is not reachable on port 8888" >&2
    wait_port 9999 || echo "start_servers: cors-proxy is not reachable on port 9999" >&2
}"""

JEST_RUN = """run_jest() {
    local json="/tmp/jest-results.json"
    local status=0
    rm -f "$json"
    node --experimental-vm-modules node_modules/jest/bin/jest.js \\
        --ci \\
        --verbose \\
        --runInBand \\
        --coverage=false \\
        --reporters=default \\
        --passWithNoTests \\
        --json \\
        --outputFile="$json" \\
        --runTestsByPath "$@" || status=$?
    echo "run_jest: jest exit status $status"
    echo "###JEST_JSON_START###"
    cat "$json"
    echo ""
    echo "###JEST_JSON_END###"
}"""

_TEST_PATH = re.compile(r"^__tests__/(?:server-only\.)?test-[^/]+\.js$")


def test_targets(test_patch: str) -> str:
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if path in ("", "/dev/null"):
            continue
        if not _TEST_PATH.match(path):
            continue
        if path not in targets:
            targets.append(path)
    return " ".join(sorted(targets))


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_JSON_BLOCK = re.compile(
    r"###JEST_JSON_START###\s*(\{.*?\})\s*###JEST_JSON_END###", re.DOTALL
)

_PASSED_STATUS = {"passed"}
_FAILED_STATUS = {"failed"}
_SKIPPED_STATUS = {"pending", "skipped", "todo", "disabled"}


class ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
{global_env}
WORKDIR /home/

{apt_command}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {_REPO_DIR} && cd {_REPO_DIR} && git rev-parse HEAD >/dev/null
{clear_env}
CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets = test_targets(self.pr.test_patch)
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh
{RUN_ENV}
{INSTALL}
{BUILD}
test -f node_modules/jest/bin/jest.js
test -x node_modules/.bin/git-http-mock-server
test -x node_modules/.bin/cors-proxy
test -f http/node/index.js
test -f http/node/package.json
test -f src/internal-apis.js
node -e "require('./package.json'); require.resolve('jest'); require.resolve('jest-junit')"
node --input-type=module -e "const m = await import('./src/index.js'); if (typeof m.commit !== 'function') throw new Error('src/index.js did not load')"
node --input-type=module -e "const m = await import('./http/node/index.js'); if (typeof m.request !== 'function') throw new Error('built http/node/index.js did not load')"
node node_modules/jest/bin/jest.js --version
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{RUN_ENV}
{BUILD}
{SERVERS}

{JEST_RUN}

TARGETS=""
for f in {targets}; do
    if [ -f "$f" ]; then
        TARGETS="$TARGETS $f"
    fi
done

if [ -z "$TARGETS" ]; then
    echo "run.sh: no gold test target exists at the base commit; nothing to run"
    exit 0
fi

start_servers
run_jest $TARGETS
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{APPLY_TEST}
{RUN_ENV}
{BUILD}
{SERVERS}

{JEST_RUN}

TARGETS="{targets}"

if [ -z "$TARGETS" ]; then
    echo "test-run.sh: the gold test patch carries no runnable test file"
    exit 0
fi

start_servers
run_jest $TARGETS
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{APPLY_FIX}
{RUN_ENV}
{BUILD}
{SERVERS}

{JEST_RUN}

TARGETS="{targets}"

if [ -z "$TARGETS" ]; then
    echo "fix-run.sh: the gold test patch carries no runnable test file"
    exit 0
fi

start_servers
run_jest $TARGETS
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
WORKDIR {_REPO_DIR}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


@Instance.register(ORG, REPO)
@Instance.register(ORG, _INTERVAL_NAME)
class ISOMORPHIC_GIT_2461_TO_2369(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        clean_log = ANSI_ESCAPE.sub("", log)
        match = _JSON_BLOCK.search(clean_log)
        if match:
            try:
                report = json.loads(match.group(1))
            except ValueError:
                report = {}

            for suite in report.get("testResults") or []:
                path = (suite.get("name") or "").strip()
                prefix = f"{_REPO_DIR}/"
                if path.startswith(prefix):
                    path = path[len(prefix) :]

                assertions = suite.get("assertionResults") or []
                if not assertions:
                    if suite.get("status") == "failed" and path:
                        failed.add(path)
                    continue

                for assertion in assertions:
                    parts = [path]
                    parts.extend(assertion.get("ancestorTitles") or [])
                    title = (assertion.get("title") or "").strip()
                    if title:
                        parts.append(title)
                    name = " > ".join(part for part in parts if part)
                    if not name:
                        continue

                    status = assertion.get("status")
                    if status in _PASSED_STATUS:
                        passed.add(name)
                    elif status in _FAILED_STATUS:
                        failed.add(name)
                    elif status in _SKIPPED_STATUS:
                        skipped.add(name)

        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
