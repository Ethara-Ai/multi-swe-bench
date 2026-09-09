"""google-gemini/gemini-cli, PRs 16965-17490 (9 instances).

Selected by ``number_interval``: ``Instance.create`` resolves
``f"{pr.org}/{pr.number_interval}"`` whenever that field is non-empty, so the
names registered at the bottom of this file are interval names, not the repo
name. ``pr.repo`` stays the real ``gemini-cli``, which is what the clone URL and
the ``/home/<repo>`` paths are built from.

Two registrations, as elsewhere in this tree: the readable interval name
``gemini_cli_17490_to_16965`` and, in ``_BUNDLE_NIS``, the hyphen-joined bundle string the raw
dataset carries in ``number_interval``.

Self-contained on purpose: every image, script and parser this interval needs is
in this file, so an era that diverges is edited here without touching its
neighbours. Interval boundaries follow the largest gaps in PR numbering across
the dataset.

Image split, following ``python/vllm_project/vllm_omni.py``: one shared base for
the repo, one image per PR. ``ImageBase`` clones and stops -- it never checks out
and never prunes history, because a single base tag is shared by every PR in a
run while each PR has its own ``base.sha``. Pinning there strips the other PRs'
commits out of the clone, and the hardening also removes the remote so nothing
could be refetched; measured on this dataset, pinning the base to PR 16087's sha
leaves 13 of the 20 base shas unreachable. The checkout and the history hardening
both live in ``ImageDefault``.

Two ``DockerfileEnhancer`` interactions are load-bearing (see ``harness/image.py``):

* ``ImageBase.dependency()`` is a **str**, so its Dockerfile IS processed, and
  ``_standardize_repo_fetch`` / ``_inject_final_sanitize`` would rewrite the clone
  and append a ``BASE_COMMIT``-pinned hardening block. Emitting
  ``# syntax=docker/dockerfile:1.6`` as the first line makes ``enhance()`` return
  the file verbatim, which is why the infrastructure block is spelled out in full
  below rather than left to the enhancer.
* ``ImageDefault.dependency()`` is an **Image**, so ``enhance()`` returns early
  and injects nothing. ``Image._HARDENING_BLOCK`` is therefore applied by hand so
  the fix commit and later history cannot be read back out of git (reward
  hacking). That block opens with ``git checkout --detach "${BASE_COMMIT}"``, so
  it performs this PR's checkout as well as the pruning.

Repo facts this era is built on:

* Every commit in range carries ``package-lock.json``; there is no pnpm or yarn
  lock and no ``packageManager`` field, so ``npm ci`` is the reproducible install.
* Root ``"test"`` is ``npm run test --workspaces --if-present``, ``"workspaces"``
  is ``["packages/*"]``, ``"engines"`` requires node >= 20.
* ``packages/core`` publishes ``"main": "dist/index.js"`` with no ``"exports"``
  map, so ``packages/cli`` resolves ``@google/gemini-cli-core`` to ``dist/`` and
  its specs cannot import it until core is built. The repo's own ``preflight``
  runs ``build`` before ``test:ci``, so ``prepare.sh`` does too.
* Each workspace owns its ``vitest.config.ts`` and there is no root vitest
  config, so the run scripts group targets by workspace and start vitest inside
  each one.
* ``packages/cli/vitest.config.ts`` enables coverage and a junit reporter writing
  ``junit.xml``; both are overridden on the command line.

Test scoping: ``integration-tests/`` and ``evals/`` drive a real model endpoint
and need credentials plus network egress, and the full workspace suite is far
larger than any one PR touches, so the run scripts execute only the spec files
the gold ``test_patch`` touches. ``run.sh`` runs before that patch is applied and
filters to files that already exist at ``base.sha`` -- a file the patch creates
yields no baseline result, which is the correct ``NONE`` baseline.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "google-gemini"
REPO = "gemini-cli"

_INTERVAL_NAME = "gemini_cli_17490_to_16965"

# node:20-bookworm, not slim: transitive dependencies build native addons through
# node-gyp on install. Every commit in range declares "engines": node >= 20.
_NODE_IMAGE = "node:20-bookworm"
_BASE_TAG = "base-node20-npm"

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


# --------------------------------------------------------------------- scripts

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

# Exported by every run script. Free of braces so it can be dropped into the
# f-string templates below.
RUN_ENV = """export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false
export DO_NOT_TRACK=1
export GEMINI_CLI_DISABLE_TELEMETRY=1"""

# package-lock.json is present at every commit in range, so npm ci is the
# reproducible install. Fall back to npm install only if the lock and the
# manifests have drifted at this particular commit.
INSTALL = "npm ci || npm install"

# packages/core resolves to dist/, so core must be built before packages/cli
# specs can import it.
BUILD = "npm run build || npm run build:packages || true"

# There is no root vitest config, so targets are grouped by workspace and vitest
# is started from inside each with the paths made workspace-relative. The marker
# line lets parse_log put the "packages/<name>/" prefix back on. Contains literal
# braces, so it is only ever interpolated as a VALUE, never format()ed.
RUN_TARGETS = """run_targets() {
    local pkg rel f
    for pkg in $(printf '%s\\n' "$@" | cut -d/ -f1-2 | sort -u); do
        rel=""
        for f in "$@"; do
            case "$f" in
                "$pkg"/*) rel="$rel ${f#"$pkg"/}" ;;
            esac
        done
        [ -n "$rel" ] || continue
        echo "=== VITEST PACKAGE: $pkg ==="
        ( cd "$pkg" && npx vitest run --reporter=verbose \\
            --coverage.enabled=false --passWithNoTests $rel ) 2>&1
    done
}"""


# ---------------------------------------------------------------- test scoping

# Only real spec files are handed to vitest. The gold test patches also touch
# helper modules (packages/cli/src/test-utils/createExtension.ts), snapshots
# (__snapshots__/*.snap) and CI workflow yaml; those travel with the patch but
# are not runnable targets.
_TEST_FILE = re.compile(r"^.+\.(?:test|spec)\.[cm]?[jt]sx?$")

# Suites that cannot run in this image: they need credentials and network egress.
_EXCLUDED_PREFIXES = ("integration-tests/", "evals/", "memory-tests/", "perf-tests/")


def test_targets(test_patch: str) -> str:
    """Space-joined repo-relative spec files the gold test patch adds or modifies.

    Reads the ``+++ b/`` side so files the patch creates are included
    (``get_modified_files`` reads the ``a/`` side and drops them). Falls back to
    the two workspaces every target in this dataset lives in, rather than to the
    whole repo, if a patch carries no recognisable spec file at all.
    """
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if path in ("/dev/null", ""):
            continue
        if path.startswith(_EXCLUDED_PREFIXES):
            continue
        if not _TEST_FILE.match(path.rsplit("/", 1)[-1]):
            continue
        if path not in targets:
            targets.append(path)
    return " ".join(sorted(targets)) if targets else "packages/core packages/cli"


# ---------------------------------------------------------------- log parsing

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Emitted by RUN_TARGETS before each workspace's vitest invocation.
_PKG_MARKER = re.compile(r"^=== VITEST PACKAGE: (?P<pkg>\S+) ===$")

# vitest's verbose reporter, one line per test:
#     +/- src/utils/paths.test.ts > tildeifyPath > replaces $HOME 3ms
# The same reporter also prints a per-FILE summary ("src/x.test.ts (12 tests)
# 340ms"); those carry no " > " and are rejected below, so a file is never
# counted as a test. Failure detail lines begin with an arrow and never match.
_RESULT = re.compile(
    r"^(?P<mark>[✓✔×✗✘↓⇓·○])\s+"
    r"(?P<name>\S.*?)"
    r"(?:\s+\[(?:skipped|todo)\])?"
    r"(?:\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\))?"
    r"(?:\s+\d+(?:\.\d+)?\s*m?s)?$"
)

_PASS_MARKS = "✓✔"
_FAIL_MARKS = "×✗✘"
_SKIP_MARKS = "↓⇓·○"


# --------------------------------------------------------------------- images

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

        # The leading syntax directive keeps DockerfileEnhancer from rewriting
        # the clone or pinning this shared image to one PR's BASE_COMMIT, so the
        # infrastructure block it would otherwise contribute is written out here.
        # BASE_COMMIT is declared but deliberately unused: build_dataset passes
        # it to every image whose dependency() is a str, and an undeclared build
        # arg is a build warning.
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
    CI=1 \\
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

RUN corepack enable || true

RUN git clone "${{REPO_URL}}" /home/{REPO}
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
cd /home/{REPO}
bash /home/check_git_changes.sh
{RUN_ENV}
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
{INSTALL}
{BUILD}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{RUN_ENV}
{RUN_TARGETS}
# Baseline: runs before test.patch, so a target the patch creates does not exist
# yet. Filter to what is present rather than letting vitest exit on a bad path.
TARGETS=""
for f in {targets}; do
    if [ -e "$f" ]; then
        TARGETS="$TARGETS $f"
    fi
done

if [ -z "$TARGETS" ]; then
    echo "run.sh: no gold test target exists at the base commit; nothing to run"
    exit 0
fi

run_targets $TARGETS || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{RUN_ENV}
{RUN_TARGETS}
run_targets {targets} || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{RUN_ENV}
{RUN_TARGETS}
run_targets {targets} || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Chains to a base Image rather than a str, so DockerfileEnhancer returns
        # this verbatim and injects nothing; the hardening block is applied by
        # hand. It opens with `git checkout --detach "${BASE_COMMIT}"`, so it
        # performs this PR's checkout as well as pruning the full history
        # inherited from the shared base. The repo is cloned once in that base,
        # so this layer never clones and needs no REPO_URL.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
{copy_commands}
WORKDIR /home/{REPO}

{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


# ------------------------------------------------------------------- instance

@Instance.register(ORG, _INTERVAL_NAME)
class GEMINI_CLI_17490_TO_16965(Instance):
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

        current_pkg = ""
        for line in ANSI_ESCAPE.sub("", log).splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            marker = _PKG_MARKER.match(stripped)
            if marker:
                current_pkg = marker.group("pkg")
                continue

            m = _RESULT.match(stripped)
            if not m:
                continue

            name = m.group("name").strip()
            # The per-file summary line carries no " > "; counting it would add a
            # phantom "test" per file.
            if " > " not in name:
                continue
            if current_pkg and not name.startswith(current_pkg + "/"):
                name = f"{current_pkg}/{name}"

            mark = m.group("mark")
            if mark in _PASS_MARKS:
                passed.add(name)
            elif mark in _FAIL_MARKS:
                failed.add(name)
            elif mark in _SKIP_MARKS:
                skipped.add(name)

        # A retried test can be reported twice with different marks. Worst wins:
        # TestResult.__post_init__ rejects any overlap between the three sets.
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


_BUNDLE_NIS = [
    "16965-17305-17327-17393-17453-17454-17470-17476-17490",
]

for _ni in _BUNDLE_NIS:
    Instance.register(ORG, _ni)(GEMINI_CLI_17490_TO_16965)
