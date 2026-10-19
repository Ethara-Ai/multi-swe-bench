import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "handsontable"
REPO = "handsontable"

_ERA = "handsontable_10655_to_8906"

_WORKSPACE = f"cd /home/{REPO}/{REPO}"


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_FILE = r"\S+\.[cm]?[jt]sx?"
_DUR = r"(?:\s*\(\s*\d+(?:\.\d+)?\s*m?s\s*\))?"

_PASS_MARKS = "✓✔√"
_FAIL_MARKS = "✕✗×✘"

_SUITE_MARKER = re.compile(r"^##SUITE##\s+(\S+)\s*$")
_JEST_HEADER = re.compile(r"^(?:PASS|FAIL)\s+(" + _FILE + r")")
_JEST_SUMMARY = re.compile(
    r"^(?:Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites)"
)
_JEST_MARK = re.compile(
    r"^(?P<indent>\s+)(?P<mark>[✓✔√✕✗×✘○✎])\s+"
    r"(?:skipped\s+|todo\s+)?(?P<name>.+?)" + _DUR + r"$"
)
_JEST_DESCRIBE = re.compile(
    r"^(?P<indent>\s+)"
    r"(?P<name>[^\s●✓✔√✕✗×✘○✎].*?)\s*$"
)

_JASMINE_START = re.compile(r"^Running \d+ specs?\.\s*$")
_JASMINE_SPEC = re.compile(
    r"^(?P<indent>\s*)(?P<name>\S.*?):\s(?P<state>passed|failed|pending)$"
)
_JASMINE_FAILURES = re.compile(r"^Failures:\s*$")
_JASMINE_COUNTS = re.compile(r"^\d+ specs?, \d+ failures?")

_TYPES_LINE = re.compile(r"^TYPES (PASS|FAIL) (\S+)$")

_NOT_DESCRIBE = re.compile(
    r"^(?:at\s|console\.|expect\(|Expected|Received|Difference|Error|\d+\s*\||\^|\d+\)\s)"
    r"|\([^()]*:\d+:\d+\)$"
)


def _status_for(mark: str) -> str:
    if mark in _PASS_MARKS:
        return "pass"
    if mark in _FAIL_MARKS:
        return "fail"
    return "skip"


def parse_handsontable_log(log: str) -> TestResult:
    status: dict[str, str] = {}
    describes: list[tuple[int, str]] = []
    suite_tag = ""
    current_file = ""
    mode = ""
    skip_indent = None

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw.rstrip())
        stripped = line.strip()

        marker = _SUITE_MARKER.match(stripped)
        if marker:
            suite_tag = marker.group(1)
            describes = []
            current_file = ""
            mode = ""
            skip_indent = None
            continue

        types_line = _TYPES_LINE.match(stripped)
        if types_line:
            status["types::" + types_line.group(2)] = (
                "pass" if types_line.group(1) == "PASS" else "fail"
            )
            continue

        if _JASMINE_START.match(stripped):
            mode = "jasmine"
            describes = []
            continue

        header = _JEST_HEADER.match(stripped)
        if header:
            mode = "jest"
            current_file = header.group(1)
            describes = []
            skip_indent = None
            continue

        if not mode or not stripped:
            continue

        indent = len(line) - len(line.lstrip())

        if mode == "jasmine_failures":
            if _JASMINE_COUNTS.match(stripped):
                mode = ""
            continue

        if mode == "jasmine":
            if _JASMINE_FAILURES.match(stripped):
                mode = "jasmine_failures"
                describes = []
                continue

            if _JASMINE_COUNTS.match(stripped) or stripped.startswith("Finished in "):
                mode = ""
                describes = []
                continue

            spec = _JASMINE_SPEC.match(line)
            if spec:
                indent = len(spec.group("indent"))
                while describes and describes[-1][0] >= indent - 2:
                    describes.pop()
                chain = [name for _, name in describes] + [spec.group("name").strip()]
                test_id = suite_tag + "::" + " > ".join(chain)
                state = spec.group("state")
                status[test_id] = {"passed": "pass", "failed": "fail"}.get(state, "skip")
                continue

            if _NOT_DESCRIBE.search(stripped):
                continue

            while describes and describes[-1][0] >= indent:
                describes.pop()
            describes.append((indent, stripped))
            continue

        if skip_indent is not None:
            if indent > skip_indent:
                continue
            skip_indent = None

        if _JEST_SUMMARY.match(stripped):
            mode = ""
            describes = []
            continue

        if stripped.startswith("●"):
            skip_indent = indent
            continue

        mark = _JEST_MARK.match(line)
        if mark:
            indent = len(mark.group("indent"))
            while describes and describes[-1][0] >= indent:
                describes.pop()
            chain = [name for _, name in describes] + [mark.group("name").strip()]
            status[current_file + "::" + " > ".join(chain)] = _status_for(
                mark.group("mark")
            )
            continue

        describe = _JEST_DESCRIBE.match(line)
        if describe:
            name = describe.group("name").strip()
            if _NOT_DESCRIBE.search(name):
                continue
            while describes and describes[-1][0] >= indent:
                describes.pop()
            describes.append((indent, name))

    passed_tests = {k for k, v in status.items() if v == "pass"}
    failed_tests = {k for k, v in status.items() if v == "fail"}
    skipped_tests = {k for k, v in status.items() if v == "skip"}

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


_SYNTAX = DockerfileEnhancer.SYNTAX_DIRECTIVE

_LABELS = (
    'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
    '      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
    '      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
    '      org.opencontainers.image.authors="https://www.ethara.ai/"'
)

_DEFAULT_PACKAGES = [
    "ca-certificates",
]

CHROMIUM_VERSION = "120.0.6099.224-1~deb11u1"

CHROME_PACKAGES = [
    f"chromium={CHROMIUM_VERSION}",
    "fonts-liberation",
    "fonts-dejavu-core",
]

CHROMIUM_ENV = """ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV NODE_OPTIONS=--max-old-space-size=4096"""


APT_SECURITY_MIRROR = (
    "RUN set -eux; \\\n"
    "    sed -i '/-security/d' /etc/apt/sources.list; \\\n"
    "    ! grep -q '\\-security' /etc/apt/sources.list; \\\n"
    "    grep -q 'deb.debian.org/debian ' /etc/apt/sources.list"
)


def base_dockerfile(image: Image, extra_packages: list[str], extra_env: str = "") -> str:
    base_img = image.dependency()
    packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + extra_packages)
    apt_command = image._get_apt_update_command(packages_str, base_img)

    sections = [
        _SYNTAX,
        f"FROM {base_img}",
        DockerfileEnhancer._TARGETARCH_ARG
        + "\n"
        + f'ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"\n'
        + "ARG BASE_COMMIT\n"
        + "\n"
        + DockerfileEnhancer._PROXY_ARGS,
        DockerfileEnhancer._ENV_BLOCK,
        _LABELS.format(org=ORG, repo=REPO),
        DockerfileEnhancer._CERT_SYMLINKS,
        APT_SECURITY_MIRROR,
        apt_command,
    ]

    if extra_env:
        sections.append(extra_env)

    sections.append("RUN git config --global --add safe.directory '*'")
    sections.append("WORKDIR /home/")
    sections.append(f'RUN git clone "${{REPO_URL}}" /home/{REPO}')
    sections.append(f"WORKDIR /home/{REPO}")
    sections.append('CMD ["/bin/bash"]')

    return "\n\n".join(sections) + "\n"


def pr_dockerfile(image: Image, harden: bool = False) -> str:
    base = image.dependency()

    sections = [f"FROM {base.image_full_name()}"]

    if image.global_env:
        sections.append(image.global_env)

    sections.append(f'ARG BASE_COMMIT="{image.pr.base.sha}"')

    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    if copy_commands:
        sections.append(copy_commands.rstrip("\n"))

    sections.append("RUN bash /home/prepare.sh")

    if harden:
        sections.append(f"WORKDIR /home/{REPO}")
        sections.append(Image._HARDENING_BLOCK.strip("\n"))

    if image.clear_env:
        sections.append(image.clear_env)

    return "\n\n".join(sections) + "\n"



BASE_ENV = f"""ENV CI=true
{CHROMIUM_ENV}"""


CHECK_GIT_CHANGES = """#!/bin/bash
set -eo pipefail

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

SHEBANG = "#!/bin/bash\nset -eo pipefail"

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi"""

RUNNER_PATH = """RUNNER="$(ls test/scripts/run-puppeteer.js test/scripts/run-puppeteer.mjs 2>/dev/null | head -n 1 || true)"
if [ -z "$RUNNER" ]; then
    echo "Error: puppeteer runner script not found" >&2
    exit 1
fi"""

LAUNCH_TIMEOUT_MS = 120000

RUNNER_PATCH = f"""{RUNNER_PATH}
sed -i -E 's/isVerbose: *(false|verboseReporting)/isVerbose: true/' "$RUNNER"
sed -i -E "s/'--no-sandbox'/'--no-sandbox', '--disable-dev-shm-usage'/" "$RUNNER"
# The runner was refactored inside this era: older base commits declare
# `const DEFAULT_INACTIVITY_TIMEOUT = <n>;` and pass it as `timeout:` to
# puppeteer.launch, while newer ones dropped both and rely on puppeteer's
# 30s default. Patch whichever shape is present so every base commit ends up
# with an explicit {LAUNCH_TIMEOUT_MS}ms launch timeout.
if grep -qE 'DEFAULT_INACTIVITY_TIMEOUT *= *[0-9]+;' "$RUNNER"; then
    sed -i -E 's/const DEFAULT_INACTIVITY_TIMEOUT = [0-9]+;/const DEFAULT_INACTIVITY_TIMEOUT = {LAUNCH_TIMEOUT_MS};/' "$RUNNER"
else
    # No constant and no `timeout:` key -- inject one into puppeteer.launch({{ ... }}).
    sed -i -E 's/(puppeteer\\.launch\\(\\{{)/\\1\\n    timeout: {LAUNCH_TIMEOUT_MS},/' "$RUNNER"
fi
if ! grep -q 'isVerbose: true' "$RUNNER"; then
    echo "Error: could not switch the jasmine reporter to verbose in ${{RUNNER}}" >&2
    exit 1
fi
if ! grep -q -- '--disable-dev-shm-usage' "$RUNNER"; then
    echo "Error: could not add --disable-dev-shm-usage to ${{RUNNER}}" >&2
    exit 1
fi
if ! grep -qE '(DEFAULT_INACTIVITY_TIMEOUT = {LAUNCH_TIMEOUT_MS};|timeout: {LAUNCH_TIMEOUT_MS},)' "$RUNNER"; then
    echo "Error: could not raise the chrome launch timeout in ${{RUNNER}}" >&2
    exit 1
fi"""

_LAUNCH_TEST = """node -e '
const puppeteer = require("puppeteer");
puppeteer
  .launch({ args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--headless", "--disable-gpu"] })
  .then(async (browser) => {
    console.log("puppeteer preflight ok: " + (await browser.version()));
    await browser.close();
  })
  .catch((error) => {
    console.error("puppeteer failed to launch chromium: " + error.message);
    process.exit(1);
  });
'"""

_CHROMIUM_PRESENT = """if ! "${PUPPETEER_EXECUTABLE_PATH}" --version >/dev/null 2>&1; then
    echo "Error: chromium at ${PUPPETEER_EXECUTABLE_PATH} is not runnable on $(uname -m)" >&2
    exit 1
fi"""

_RUNNER_ASSERTS = f"""{RUNNER_PATH}
if ! grep -q 'isVerbose: true' "$RUNNER"; then
    echo "Error: the jasmine reporter is not in verbose mode in ${{RUNNER}}" >&2
    exit 1
fi
if ! grep -q -- '--disable-dev-shm-usage' "$RUNNER"; then
    echo "Error: ${{RUNNER}} is missing --disable-dev-shm-usage" >&2
    exit 1
fi
if ! grep -qE '(DEFAULT_INACTIVITY_TIMEOUT = {LAUNCH_TIMEOUT_MS};|timeout: {LAUNCH_TIMEOUT_MS},)' "$RUNNER"; then
    echo "Error: ${{RUNNER}} still has the stock chrome launch timeout" >&2
    exit 1
fi"""

PUPPETEER_PREFLIGHT = f"""{_CHROMIUM_PRESENT}
{_LAUNCH_TEST}
{_RUNNER_ASSERTS}"""

_KERNEL_ARCH = "$(cat /proc/sys/kernel/arch 2>/dev/null || uname -m)"

PUPPETEER_PREFLIGHT_BUILD = f"""{_CHROMIUM_PRESENT}
if {_LAUNCH_TEST}; then
    :
elif [ "{_KERNEL_ARCH}" != "$(uname -m)" ]; then
    echo "Warning: chromium cannot launch under $(uname -m) emulation on a {_KERNEL_ARCH} kernel; skipping the launch check" >&2
else
    echo "Error: puppeteer cannot launch chromium on native $(uname -m)" >&2
    exit 1
fi
{_RUNNER_ASSERTS}"""

RUN_SUITE = """run_suite() {
    local tag="$1"; shift
    local log="/tmp/suite-${tag}.log"
    echo "##SUITE## ${tag}"
    "$@" 2>&1 | tee "$log" || true
    if ! grep -qE '^(PASS|FAIL) |^Running [0-9]+ specs?\\.' "$log"; then
        echo "Error: suite ${tag} produced no test output" >&2
        exit 1
    fi
}"""


_TEST_COMMANDS = f"""{RUN_SUITE}

run_suite unit npm run test:unit -- --verbose

npm run build:walkontable
npm run test:walkontable.dump
run_suite walkontable npm run test:walkontable.puppeteer

npm run build:umd
npm run build:languages
npm run test:e2e.dump
run_suite e2e npm run test:e2e.puppeteer"""

_TYPES_SUITE = """run_types() {
    echo "##SUITE## types"
    local files rc=0 failed=0
    npx --no-install tsc -p ./test/types -t es2015 > /tmp/tsc.log 2>&1 || rc=$?
    cat /tmp/tsc.log
    files=$(npx --no-install tsc -p ./test/types -t es2015 --listFilesOnly | sed "s#^$PWD/##" | grep '\\.types\\.ts$' || true)
    if [ -z "$files" ]; then
        echo "Error: suite types listed no type-test files" >&2
        exit 1
    fi
    for f in $files; do
        if grep -qF "$f(" /tmp/tsc.log; then
            echo "TYPES FAIL $f"
            failed=$((failed+1))
        else
            echo "TYPES PASS $f"
        fi
    done
    if [ "$rc" -ne 0 ] && [ "$failed" -eq 0 ]; then
        echo "Error: tsc exited $rc without an error in any type-test file" >&2
        exit 1
    fi
}"""

_DIFF_FILE = re.compile(r"^diff --git a/(\S+)", re.M)


def _test_commands(pr: PullRequest) -> str:
    if not any(f.endswith(".types.ts") for f in _DIFF_FILE.findall(f"{pr.test_patch}")):
        return _TEST_COMMANDS
    return f"{_TEST_COMMANDS}\n\n{_TYPES_SUITE}\n\n{_WORKSPACE}\nrun_types"


_DEPS_GATE = f"""cd /home/{REPO}/{REPO}
node -e "require.resolve('jest')"
node -e "require.resolve('webpack')"
node -e "require.resolve('puppeteer')"
node -e "require.resolve('env-cmd')"
echo DEPS_OK"""


class ImageBase10655To8906(Image):
    NODE_TAG = "node:16-bullseye"
    TAG = "base-node16-chromium120"

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
        return self.NODE_TAG

    def image_tag(self) -> str:
        return self.TAG

    def workdir(self) -> str:
        return self.TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self, CHROME_PACKAGES, BASE_ENV)


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
        return ImageBase10655To8906(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
# --- Section 1: PIN -------------------------------------------------------
cd /home/{REPO}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags origin {self.pr.base.sha}
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

# --- Section 2: PROVISION -------------------------------------------------
export PHANTOMJS_PLATFORM=linux
export PHANTOMJS_ARCH=x64
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
n=0
until npm ci --legacy-peer-deps; do
    n=$((n+1))
    if [ "$n" -ge 3 ]; then echo "Error: npm ci failed 3 times" >&2; exit 1; fi
    echo "npm ci attempt $n failed; retrying" >&2
    sleep 15
done
{_WORKSPACE}
{RUNNER_PATCH}

# --- Section 3: GATE ------------------------------------------------------
{PUPPETEER_PREFLIGHT_BUILD}

{_DEPS_GATE}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_test_commands(self.pr)}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_test_commands(self.pr)}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_test_commands(self.pr)}
""",
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self, harden=True)


@Instance.register(ORG, _ERA)
class HANDSONTABLE_10655_TO_8906(Instance):
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
        return parse_handsontable_log(log)
