import re
import xml.etree.ElementTree as ET
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "fastify"
REPO = "fastify"

_INTERVAL_NAME = "fastify_7121_to_6830"

_NODE_IMAGE = "node:24-bookworm"
_BASE_TAG = "base-node24-npm"
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
export NODE_OPTIONS="--max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false"""

INSTALL = (
    "npm install --ignore-scripts --no-audit --no-fund || "
    "npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps"
)

REFRESH_DEPS = """if ! git diff --quiet -- package.json; then
    echo "dependencies: package.json was modified by an applied patch, refreshing node_modules"
    npm install --ignore-scripts --no-audit --no-fund
fi"""

TEST_RUN = """run_tests() {
    local xml="/tmp/node-test-results.xml"
    local status=0
    rm -f "$xml"
    node --test --test-timeout=60000 \\
        --test-reporter=spec --test-reporter-destination=stdout \\
        --test-reporter=junit --test-reporter-destination="$xml" \\
        "$@" || status=$?
    echo "run_tests: node --test exit status $status"
    echo "###NODE_TEST_XML_START###"
    cat "$xml"
    echo ""
    echo "###NODE_TEST_XML_END###"
}"""

_TEST_PATH = re.compile(r"^test/.*\.test\.(?:js|mjs)$")


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

_XML_BLOCK = re.compile(
    r"###NODE_TEST_XML_START###\s*(<\?xml.*?</testsuites>)\s*###NODE_TEST_XML_END###",
    re.DOTALL,
)


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
test -x node_modules/.bin/borp
node -e "const f = require('./fastify.js'); if (typeof f !== 'function') throw new Error('fastify entry point did not load')"
node -e "require('undici'); require('split2'); require('proxyquire'); require('process-warning'); require('fastify-plugin')"
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{RUN_ENV}
{REFRESH_DEPS}

{TEST_RUN}

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

run_tests $TARGETS
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{APPLY_TEST}
{RUN_ENV}
{REFRESH_DEPS}

{TEST_RUN}

TARGETS="{targets}"

if [ -z "$TARGETS" ]; then
    echo "test-run.sh: the gold test patch carries no runnable test file"
    exit 0
fi

run_tests $TARGETS
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd {_REPO_DIR}
{APPLY_FIX}
{RUN_ENV}
{REFRESH_DEPS}

{TEST_RUN}

TARGETS="{targets}"

if [ -z "$TARGETS" ]; then
    echo "fix-run.sh: the gold test patch carries no runnable test file"
    exit 0
fi

run_tests $TARGETS
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
class FASTIFY_7121_TO_6830(Instance):
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
        match = _XML_BLOCK.search(clean_log)
        if match:
            try:
                root = ET.fromstring(match.group(1))
            except ET.ParseError:
                root = None

            prefix = f"{_REPO_DIR}/"

            def visit(node, suites: list[str]) -> None:
                for child in node:
                    tag = child.tag
                    if tag == "testsuite":
                        name = (child.get("name") or "").strip()
                        visit(child, suites + [name] if name else suites)
                    elif tag == "testcase":
                        path = (child.get("file") or "").strip()
                        if path.startswith(prefix):
                            path = path[len(prefix) :]
                        title = (child.get("name") or "").strip()
                        parts = [path] + suites
                        if title and title != path:
                            parts.append(title)
                        full = " > ".join(part for part in parts if part)
                        if not full:
                            continue
                        kinds = {grand.tag for grand in child}
                        if "failure" in kinds or "error" in kinds:
                            failed.add(full)
                        elif "skipped" in kinds:
                            skipped.add(full)
                        elif child.get("failure") is not None:
                            failed.add(full)
                        else:
                            passed.add(full)

            if root is not None:
                visit(root, [])

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
