import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "shadcn-ui"
REPO = "ui"
REPO_DIR = f"/home/{REPO}"

_INTERVAL_NAME = "ui_11074_to_10644"

_NODE_IMAGE = "node:22-bookworm"
_BASE_TAG = "base-11074_to_10644"

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

RUN_ENV = f"""export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export PATH={REPO_DIR}/node_modules/.bin:$PATH
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export DO_NOT_TRACK=1
export NEXT_TELEMETRY_DISABLED=1
export TURBO_TELEMETRY_DISABLED=1
export NEXT_PUBLIC_APP_URL=http://localhost:4000
export NEXT_PUBLIC_V0_URL=https://v0.dev
export REGISTRY_URL=http://localhost:4000/r"""

INSTALL = "pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile"

BUILD_WORKSPACE = 'pnpm --filter "./packages/*" run --if-present build'

DEPS_GATE = f"""test -d {REPO_DIR}/node_modules
test -x {REPO_DIR}/node_modules/.bin/vitest
node --version
pnpm --version
vitest --version
echo "DEPS_OK\""""

VITEST_HELPERS = """vitest_root() {
    local dir="$1"
    while [ -n "$dir" ] && [ "$dir" != "." ] && [ "$dir" != "/" ]; do
        if [ -f "$dir/vitest.config.ts" ] \\
            || [ -f "$dir/vitest.config.mts" ] \\
            || [ -f "$dir/vitest.config.js" ] \\
            || [ -f "$dir/vitest.config.mjs" ]; then
            printf '%s\\n' "$dir"
            return 0
        fi
        dir=$(dirname "$dir")
    done
    printf '%s\\n' "."
}

run_targets() {
    local status=0
    local roots=()
    local rel=()
    local root known candidate target seen

    for target in "$@"; do
        candidate=$(vitest_root "$(dirname "$target")")
        seen=0
        for known in "${roots[@]}"; do
            if [ "$known" = "$candidate" ]; then
                seen=1
            fi
        done
        if [ "$seen" -eq 0 ]; then
            roots+=("$candidate")
        fi
    done

    for root in "${roots[@]}"; do
        rel=()
        for target in "$@"; do
            candidate=$(vitest_root "$(dirname "$target")")
            if [ "$candidate" != "$root" ]; then
                continue
            fi
            if [ "$root" = "." ]; then
                rel+=("$target")
            else
                rel+=("${target#"$root"/}")
            fi
        done
        if [ "${#rel[@]}" -eq 0 ]; then
            continue
        fi
        printf '=== VITEST ROOT: %s ===\\n' "$root"
        if ! ( cd "$root" && vitest run --reporter=tap-flat --passWithNoTests "${rel[@]}" ); then
            status=1
        fi
        printf '=== VITEST ROOT DONE: %s ===\\n' "$root"
    done

    printf '=== VITEST EXIT STATUS: %s ===\\n' "$status"
}"""


_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_EXCLUDED_PREFIXES = ("packages/tests/",)
_EXCLUDED_SUBSTRINGS = ("/fixtures/", "/__fixtures__/", "/templates/", ".browser.test.")


def test_targets(test_patch: str) -> list[str]:
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if not path or path == "/dev/null":
            continue
        if path.startswith(_EXCLUDED_PREFIXES):
            continue
        if any(token in path for token in _EXCLUDED_SUBSTRINGS):
            continue
        if not _TEST_FILE_RE.search(path):
            continue
        if path not in targets:
            targets.append(path)
    return sorted(targets)


def targets_array(test_patch: str) -> str:
    targets = test_targets(test_patch)
    if not targets:
        return "TEST_TARGETS=()"
    joined = " ".join(shlex.quote(target) for target in targets)
    return f"TEST_TARGETS=( {joined} )"


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

_ROOT_MARKER = re.compile(r"^=== VITEST ROOT: (?P<root>\S+) ===$")

_TAP_RESULT = re.compile(
    r"^(?P<status>not ok|ok) \d+ - (?P<name>.*?)"
    r"(?: # (?P<comment>SKIP|TODO|time=[0-9.]+ms))?$"
)


def _tap_unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(value[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


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
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \\
    NEXT_TELEMETRY_DISABLED=1 \\
    TURBO_TELEMETRY_DISABLED=1 \\
    DO_NOT_TRACK=1 \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true

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

RUN corepack enable

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {REPO_DIR} && \\
    cd {REPO_DIR} && git rev-parse HEAD > /dev/null
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
        targets = targets_array(self.pr.test_patch)
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh
{RUN_ENV}
{INSTALL}
{BUILD_WORKSPACE}
{DEPS_GATE}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{RUN_ENV}
{targets}
{VITEST_HELPERS}
EXISTING_TARGETS=()
for target in "${{TEST_TARGETS[@]}}"; do
    if [ -e "$target" ]; then
        EXISTING_TARGETS+=("$target")
    fi
done

if [ "${{#EXISTING_TARGETS[@]}}" -eq 0 ]; then
    echo "run.sh: no gold test target exists at the base commit; nothing to run"
    exit 0
fi

run_targets "${{EXISTING_TARGETS[@]}}"
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{APPLY_TEST}
{RUN_ENV}
{targets}
{VITEST_HELPERS}
if [ "${{#TEST_TARGETS[@]}}" -eq 0 ]; then
    echo "test-run.sh: gold test patch carries no runnable vitest target"
    exit 0
fi

run_targets "${{TEST_TARGETS[@]}}"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd {REPO_DIR}
{APPLY_FIX}
{RUN_ENV}
{targets}
{VITEST_HELPERS}
if [ "${{#TEST_TARGETS[@]}}" -eq 0 ]; then
    echo "fix-run.sh: gold test patch carries no runnable vitest target"
    exit 0
fi

run_targets "${{TEST_TARGETS[@]}}"
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
WORKDIR {REPO_DIR}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {REPO_DIR}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{clear_env}"""


@Instance.register(ORG, REPO)
class UI_11074_TO_10644(Instance):
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

        current_root = "."
        for line in ANSI_ESCAPE.sub("", log).splitlines():
            line = line.rstrip()

            marker = _ROOT_MARKER.match(line)
            if marker:
                current_root = marker.group("root")
                continue

            match = _TAP_RESULT.match(line)
            if not match:
                continue

            name = _tap_unescape(match.group("name")).strip()
            if " > " not in name:
                continue
            if current_root not in (".", ""):
                name = f"{current_root}/{name}"

            comment = match.group("comment") or ""
            if match.group("status") == "not ok":
                failed.add(name)
            elif comment in ("SKIP", "TODO"):
                skipped.add(name)
            else:
                passed.add(name)

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


_ALIAS_NIS = [
    _INTERVAL_NAME,
    "10644-10967-11074",
]

for _ni in _ALIAS_NIS:
    Instance.register(ORG, _ni)(UI_11074_TO_10644)
