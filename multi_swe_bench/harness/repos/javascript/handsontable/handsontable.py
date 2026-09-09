import re

from multi_swe_bench.harness.image import DockerfileEnhancer, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "handsontable"
REPO = "handsontable"

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_FILE = r"\S+\.[cm]?jsx?"
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
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
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
ENV CHROME_BIN=/usr/bin/chromium
ENV NODE_OPTIONS=--max-old-space-size=4096"""


APT_SECURITY_MIRROR = (
    "RUN set -eux; \\\n"
    "    sed -i '/-security/d' /etc/apt/sources.list; \\\n"
    "    ! grep -q '\\-security' /etc/apt/sources.list; \\\n"
    "    grep -q 'deb.debian.org/debian ' /etc/apt/sources.list"
)


CHROMIUM_ALIAS = (
    "RUN set -eux; \\\n"
    "    ln -sf /usr/bin/chromium /usr/bin/chromium-browser; \\\n"
    "    /usr/bin/chromium-browser --version"
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
        + "\n"
        + DockerfileEnhancer._PROXY_ARGS,
        DockerfileEnhancer._ENV_BLOCK,
        "ENV CI=true",
        _LABELS.format(org=ORG, repo=REPO),
        DockerfileEnhancer._CERT_SYMLINKS,
        "WORKDIR /home/",
        APT_SECURITY_MIRROR,
        apt_command,
        CHROMIUM_ALIAS,
    ]

    if extra_env:
        sections.append(extra_env)

    # A plain `git clone` of this repo is ~790MB in one pack and proved fragile
    # in-container: the transfer stalled and died in `index-pack` after 80min
    # (exit 128), taking the whole build with it. Harden it rather than shrink
    # history -- the PR layers check out arbitrary base commits and the hardening
    # block runs `git gc`, so a shallow or blob-filtered clone is not safe here.
    #   * compression 0 + a large postBuffer keeps the pack streaming steadily
    #   * low-speed abort turns an indefinite hang into a fast, retryable failure
    #   * three attempts, cleaning up the partial clone between each
    #   * a final assert so a silent partial clone can never reach the PR layers
    sections.append(
        f'RUN set -eux; \\\n'
        f'    git config --global core.compression 0; \\\n'
        f'    git config --global http.postBuffer 1048576000; \\\n'
        f'    git config --global http.lowSpeedLimit 1000; \\\n'
        f'    git config --global http.lowSpeedTime 120; \\\n'
        f'    for i in 1 2 3; do \\\n'
        f'        git clone "${{REPO_URL}}" /home/{REPO} && break; \\\n'
        f'        echo "clone attempt $i failed; retrying" >&2; \\\n'
        f'        rm -rf /home/{REPO}; \\\n'
        f'    done; \\\n'
        f'    git -C /home/{REPO} rev-parse --verify HEAD; \\\n'
        f'    test "$(git -C /home/{REPO} rev-list --all --count)" -gt 10000'
    )
    sections.append(f"WORKDIR /home/{REPO}")
    sections.append('CMD ["/bin/bash"]')

    return "\n\n".join(sections) + "\n"


def pr_dockerfile(image: Image, harden: bool = False) -> str:
    """Render the PR layer on top of the shared era base.

    ``harden=True`` emits the git detach/scrub/assert block as Dockerfile RUN
    steps instead of leaving it to ``prepare.sh``. It is opt-in per era so the
    eras processed in earlier phases keep byte-identical Dockerfiles.
    """
    base = image.dependency()

    sections = [f"FROM {base.image_full_name()}"]

    if image.global_env:
        sections.append(image.global_env)

    sections.append(
        f"ARG BASE_COMMIT={image.pr.base.sha}\n"
        "ENV BASE_COMMIT=${BASE_COMMIT}\n"
        "ARG TARGETARCH\n"
        "ARG BUILDARCH\n"
        "ENV TARGETARCH=${TARGETARCH}\n"
        "ENV BUILDARCH=${BUILDARCH}"
    )

    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    if copy_commands:
        sections.append(copy_commands.rstrip("\n"))

    if harden:
        # Some PR base commits are unreachable from any ref upstream (the base
        # branch was force-pushed after collection). `git clone` in the era base
        # therefore does not carry them, and the canonical checkout below would
        # fail with "reference is not a tree". GitHub still serves such objects
        # by explicit SHA, so fetch on demand first. The guard keeps this a no-op
        # (and offline-safe) whenever the commit is already present, so the
        # common path is unchanged. No --depth: a shallow boundary here would
        # break the `git gc --prune=now --aggressive` inside the hardening block.
        sections.append(
            'RUN set -eux; \\\n'
            '    git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null \\\n'
            '    || git fetch --no-tags origin "${BASE_COMMIT}"; \\\n'
            '    git cat-file -e "${BASE_COMMIT}^{commit}"'
        )

        # WORKDIR is /home/<repo> (set by the base image), and BASE_COMMIT is an
        # ENV a few lines up, so the canonical block runs as-is here.
        sections.append(Image._HARDENING_BLOCK.strip("\n"))

    sections.append("RUN bash /home/prepare.sh")

    if image.clear_env:
        sections.append(image.clear_env)

    return "\n\n".join(sections) + "\n"


CHECKOUT = """git reset --hard && git checkout ${BASE_COMMIT}"""

HARDENING = """git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD
        git remote remove origin 2>/dev/null || true
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d
        git reflog expire --expire=now --all
        git reflog expire --expire-unreachable=now --all
        git gc --prune=now --aggressive
        rm -f .git/objects/info/alternates
    '
fi"""


BASE_ENV = f"""ENV NPM_CONFIG_FUND=false
ENV NPM_CONFIG_AUDIT=false
ENV TZ=Europe/Warsaw
{CHROMIUM_ENV}"""


class ImageBase(Image):
    """Shared base for every handsontable era.

    The base only installs the toolchain and clones the repository, so the
    root-vs-monorepo layout difference (which lives in prepare.sh and the run
    scripts) does not reach it. An era that ever needs a different Node line
    subclasses this and overrides NODE_TAG and TAG.
    """

    NODE_TAG = "node:16-bullseye"
    TAG = "base-node16-chromium120"

    def __init__(self, pr: PullRequest, config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self):
        return self._config

    def dependency(self) -> str:
        return self.NODE_TAG

    def image_tag(self) -> str:
        return self.TAG

    def workdir(self) -> str:
        return self.TAG

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self, CHROME_PACKAGES, BASE_ENV)


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

PUPPETEER_PREFLIGHT_BUILD = f"""{_CHROMIUM_PRESENT}
if {_LAUNCH_TEST}; then
    :
elif [ "${{TARGETARCH:-}}" != "${{BUILDARCH:-}}" ]; then
    echo "Warning: chromium launch preflight failed while cross-building ${{BUILDARCH:-?}} -> ${{TARGETARCH:-?}}; emulation limitation, continuing" >&2
else
    echo "Error: puppeteer cannot launch chromium on native ${{TARGETARCH:-$(uname -m)}}" >&2
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

_ERAS = [
    (12119, "handsontable_12305_to_12119"),
    (8906, "handsontable_10655_to_8906"),
    (6544, "handsontable_8742_to_6544"),
    (4802, "handsontable_5643_to_4802"),
]


def resolve_era(pr: PullRequest) -> str:
    number = getattr(pr, "number", 0) or 0
    for lower_bound, era_key in _ERAS:
        if number >= lower_bound:
            return era_key
    return ""


if not getattr(Instance, "_handsontable_route_hook", False):
    _stock_create = Instance.create.__func__

    def _handsontable_create(cls, pr, config, *args, **kwargs):
        if getattr(pr, "org", "") == ORG and getattr(pr, "repo", "") == REPO:
            if not getattr(pr, "number_interval", ""):
                era_key = resolve_era(pr)
                if era_key and f"{pr.org}/{era_key}" in cls._registry:
                    return cls._registry[f"{pr.org}/{era_key}"](
                        pr, config, *args, **kwargs
                    )
        return _stock_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_handsontable_create)
    Instance._handsontable_route_hook = True
