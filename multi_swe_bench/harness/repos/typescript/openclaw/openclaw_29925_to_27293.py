"""openclaw/openclaw harness for the 10-PR bundle #27293 - #29925.

The ten PRs all sit on `main` between 2026-02-26 and 2026-02-28, two weeks after
the range the sibling `openclaw.py` was written for (#1437 - #7610), and the
toolchain is identical at every one of them::

    engines.node        >=22.12.0          packageManager   pnpm@10.23.0
    vitest              ^4.0.18            test entry       scripts/test-parallel.mjs
    vitest configs      vitest.config.ts, vitest.{unit,extensions,gateway,e2e}.config.ts
    submodules          none               lockfile         pnpm-lock.yaml

All five vitest configs are present at all ten base commits, so the three-lane
split (unit / extensions / gateway) always applies and the run scripts never
fall back to the single-config path.

ARCHITECTURE: single-commit-fetch (the third shape the Dockerfile QC blesses).
The base ships the toolchain and *no clone*; each PR image fetches only its own
commit inside prepare.sh. That is a deliberate choice for this repo rather than
a shortcut:

  * openclaw's history is 90,890 commits on main across 5,557 refs -- 267 MB as
    a blobless clone, well over a gigabyte with blobs. A full-history shared base
    would push that into all ten PR images.
  * A depth-1 fetch of a base commit is 27 MB and one commit, measured.
  * Nothing in the graded path reads history. The only `prepare` hook is
    `git config core.hooksPath`, guarded by `|| exit 0`, and there are no
    submodules, so a shallow tree is indistinguishable from a full one here.
  * Nothing leaks. The fetch layer only ever contained one commit, so the fix
    commit is not recoverable from a lower layer -- the disclosure the QC asks
    for under A6.

The consequence for grading: the base carries no `${BASE_COMMIT}` reference and
no scrub, the PR layer owns the pin (prepare.sh) and the prune, and the four
canonical prune assertions hold trivially because only one commit was fetched.

BASE TAG: `base-27293_to_29925`. The two sibling openclaw configs both tag their
base plain `base`, so reusing that name would collide -- one tag, three different
image bodies, last build wins.

All twenty patches were verified to apply at their own base commit, so no
per-PR patch exclusions are needed here (unlike openclaw.py, which carries a
CHANGELOG.md retry for its #1450).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "openclaw_29925_to_27293"
_BASE_TAG = "base-27293_to_29925"
_LO, _HI = 27293, 29925

_UI_PATH_RE = re.compile(r"^diff --git a/ui/", re.MULTILINE)
_E2E_TARGET_RE = re.compile(
    r"^diff --git a/(\S+\.e2e\.test\.[cm]?[jt]sx?) b/", re.MULTILINE
)


def _touches_ui(pr: PullRequest) -> bool:
    return bool(
        _UI_PATH_RE.search(pr.test_patch or "")
        or _UI_PATH_RE.search(pr.fix_patch or "")
    )


def _e2e_targets(pr: PullRequest) -> list[str]:
    return sorted(set(_E2E_TARGET_RE.findall(pr.test_patch or "")))


# ---------------------------------------------------------------------------
# Dockerfiles.  Rendered with __PLACEHOLDER__ tokens and str.replace() so no
# shell ${...} has to be brace-escaped.
#
# The base follows the QC's shared-base reference: syntax directive on line 1
# (the DockerfileEnhancer opt-out), the ARG/ENV/LABEL/cert-farm preamble, the
# shard-wide toolchain, and nothing that expands ${BASE_COMMIT}.  BASE_COMMIT is
# declared only to silence BuildKit's unused-arg warning, since the harness
# passes it to every image whose dependency is a string.
#
# The cert farm runs before the first network RUN: apt and the pnpm registry
# calls both need the injected CA to already be trusted.
# ---------------------------------------------------------------------------

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    DO_NOT_TRACK=1 \
    OPENCLAW_TELEMETRY_DISABLED=1 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NODE_OPTIONS=--max-old-space-size=4096

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable

RUN git config --global --add safe.directory '*'

CMD ["/bin/bash"]
"""


# The PR layer owns the pin and the prune, which is what the shared-base split
# is for.  WORKDIR is load-bearing: the prune and submodule blocks below issue
# bare `git` commands with no `cd` of their own, and prepare.sh's `git init`
# needs the directory to exist.
#
# The prune block deliberately contains no `git reset`, no `git clean` and no
# path-scoped checkout: prepare.sh has already installed node_modules and may
# have touched tracked files, and any of those would undo that work.  HEAD is
# already detached at the base commit, so the opening assertion verifies that
# rather than re-establishing it, and no clean-tree check runs after this point.
_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

WORKDIR /home/__REPO__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


# prepare.sh owns acquisition and provisioning, and runs at BUILD time so the
# shipped image is already installed before any graded stage starts.
#
# The fetch is depth-1 against this PR's own commit: GitHub serves an arbitrary
# reachable SHA that way, which was verified against this repo, and it is what
# keeps a 90k-commit history out of the image. The retry loop is not decoration
# -- openclaw's pack is large enough that GitHub drops the transfer often enough
# to matter, and HTTP/1.1 keeps it off the multiplexed path those decode errors
# come from.
#
# The install is frozen to the commit's own pnpm-lock.yaml, so it resolves the
# PR's era rather than today's registry. It is retried rather than tolerated:
# nothing here is wrapped in `|| true` except the a2ui bundle, which the canvas
# suites stub for themselves when it is absent.
#
# The closing gate is deliberately not just "does package.json parse". It also
# resolves vitest, which is the test-only dependency every graded lane needs: a
# partial install that leaves the runner missing would otherwise produce an
# empty report that the harness reads as "0 failures" rather than a broken image.
_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git init -q .
git config --local advice.detachedHead false
git config --local http.version HTTP/1.1
git config --local http.postBuffer 524288000
git remote add origin "https://github.com/__ORG__/__REPO__.git" 2>/dev/null || true

fetched=0
for attempt in 1 2 3 4 5; do
    if git fetch --depth 1 --no-tags origin "__BASE_SHA__"; then
        fetched=1
        break
    fi
    echo "prepare: fetch attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$fetched" -eq 1

git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

installed=0
for attempt in 1 2 3; do
    if corepack pnpm install --frozen-lockfile --config.engine-strict=false; then
        installed=1
        break
    fi
    echo "prepare: pnpm install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1

corepack pnpm canvas:a2ui:bundle || \
    echo "prepare: a2ui bundle failed; the canvas suites stub it themselves" >&2
__UI_SETUP__
node -e "require('./package.json'); console.log('DEPS_OK: package.json')"
test -x node_modules/.bin/vitest
corepack pnpm exec vitest --version > /dev/null
echo "DEPS_OK"
"""


_UI_SETUP = r"""
if corepack pnpm --dir ui exec playwright install --with-deps chromium; then
    touch /home/.openclaw-ui-lane
else
    echo "prepare: chromium unavailable; the ui lane will be skipped" >&2
fi
"""


# --retry=2 is load-bearing, not tidiness. A handful of this suite's tests are
# timing-dependent and occasionally hit the repo's 240s testTimeout; because the
# three stages are separate processes, a test that times out in one stage and
# passes in the others manufactures a transition that has nothing to do with the
# PR. That cost pr-27910 its whole instance -- an unrelated plugin-sdk test timed
# out only in the fix stage, which the harness read as a pass-to-fail and marked
# the instance invalid. Retrying a failed test twice before recording it removes
# that class of false transition, and costs nothing when nothing is flaky.
#
# One shared lane driver, invoked identically by all three graded scripts, so
# the three stages cannot disagree about what was run. Each lane is allowed to
# fail without aborting the others -- a failing lane is a result, not a build
# error -- and the banner it prints is what parse_log uses to attribute a test
# to its lane.
#
# The lanes mirror what .github/workflows/ci.yml runs at these commits. All ten
# base commits carry the three-config split, so the single-config branch below
# is unreachable for this bundle and kept only so the script stays correct if
# the range is ever widened backwards.
_RUN_SUITES_SH = r"""#!/bin/bash
set -uo pipefail

export CI=true
cd /home/__REPO__

vitest_run() {
    corepack pnpm exec vitest run --reporter=verbose --silent=passed-only --retry=2 "$@"
}

run_lane() {
    label="$1"
    shift
    echo "===== openclaw-lane: ${label} ====="
    "$@" || echo "===== openclaw-lane-failed: ${label} (exit $?) ====="
}

if [ -f vitest.unit.config.ts ] && [ -f vitest.extensions.config.ts ] \
        && [ -f vitest.gateway.config.ts ]; then
    run_lane unit vitest_run --config vitest.unit.config.ts
    run_lane extensions vitest_run --config vitest.extensions.config.ts
    run_lane gateway vitest_run --config vitest.gateway.config.ts
else
    run_lane unit vitest_run --config vitest.config.ts
fi
__E2E_LANE__
if [ -f /home/.openclaw-ui-lane ]; then
    run_lane ui corepack pnpm --dir ui exec vitest run \
        --config vitest.config.ts --reporter=verbose --silent=passed-only
fi

exit 0
"""


_E2E_LANE = r"""
if [ -f vitest.e2e.config.ts ] && grep -q 'e2e\.test\.ts' vitest.config.ts; then
    run_lane e2e vitest_run --config vitest.e2e.config.ts __E2E_TARGETS__
fi
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

bash /home/run-suites.sh
"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
"""


class OpenclawBundleImageBase(Image):
    """Shard-wide base: toolchain only, pinned to nothing.

    No clone and no ${BASE_COMMIT} reference, so one tag serves all ten PRs and
    none of them can be stripped out of it. Acquisition happens per PR, inside
    prepare.sh.
    """

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
        # engines.node is >=22.12.0 at every base commit in the range.
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class OpenclawBundleImageDefault(Image):
    """Per-PR layer: fetch + install (prepare.sh), then pin and prune."""

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
        return OpenclawBundleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        ui_setup = _UI_SETUP if _touches_ui(self.pr) else ""

        targets = _e2e_targets(self.pr)
        if targets:
            e2e_lane = _E2E_LANE.replace(
                "__E2E_TARGETS__", " ".join(f"'{path}'" for path in targets)
            )
        else:
            e2e_lane = ""

        prepare = self._render(_PREPARE_SH).replace("__UI_SETUP__", ui_setup)
        run_suites = self._render(_RUN_SUITES_SH).replace("__E2E_LANE__", e2e_lane)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run-suites.sh", run_suites),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return self._render(_DEFAULT_DOCKERFILE).replace(
            "__BASE_IMAGE__", image.image_full_name()
        ).replace("__COPY_COMMANDS__", copy_commands)


# ---------------------------------------------------------------------------
# parse_log
#
# vitest 4's verbose reporter prints one line per test, prefixed with a status
# glyph and suffixed with an optional duration:
#
#     ✓ src/config/models-config.test.ts > resolves provider from env  12ms
#     × src/cron/service.test.ts > passes heartbeat target             4ms
#     ↓ src/gateway/http.test.ts > binds socket [skipped]
#
# Only lines that carry a `<file>.test.ts > ` marker are counted, which is what
# keeps vitest's summary lines and the repo's own console output from being
# mistaken for results. The lane banners printed by run-suites.sh namespace the
# `ui` lane, whose suites can share leaf names with the main tree.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LANE_RE = re.compile(r"^=+\s*openclaw-lane:\s*(\S+)\s*=+$")
_TIMING = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s|m))?"
_PASS_RE = re.compile(rf"^\s*[✓✔]\s+(.+?){_TIMING}\s*$")
_FAIL_RE = re.compile(rf"^\s*[×✕✗]\s+(.+?){_TIMING}\s*$")
_SKIP_RE = re.compile(rf"^\s*[↓○◌]\s+(.+?){_TIMING}\s*$")
_TEST_NAME_RE = re.compile(r"\.test\.[cm]?[jt]sx?\s+>\s+\S")


def openclaw_bundle_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)
    lane = ""

    def qualify(name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip()
        return f"ui > {name}" if lane == "ui" else name

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()

        banner = _LANE_RE.match(line.strip())
        if banner:
            lane = banner.group(1)
            continue

        match = _FAIL_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            failed_tests.add(qualify(match.group(1)))
            continue

        match = _PASS_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            passed_tests.add(qualify(match.group(1)))
            continue

        match = _SKIP_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            skipped_tests.add(qualify(match.group(1)))

    # TestResult.__post_init__ requires the three sets to be pairwise disjoint.
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


@Instance.register("openclaw", _INTERVAL_NAME)
class OPENCLAW_29925_TO_27293(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawBundleImageDefault(self.pr, self._config)

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
        return openclaw_bundle_parse_log(test_log)


# ---------------------------------------------------------------------------
# Routing
#
# The raw dataset carries no number_interval, and the generated dataset carries
# the bare PR number, so neither reaches the interval key above on its own.
# Both fall back to "openclaw/openclaw", which openclaw.py already owns for its
# own #1437 - #7610 bundle.
#
# So this module wraps that key rather than replacing it: a PR inside this
# bundle's range routes here, and anything else is handed to whoever held the
# bare name first. Registering the numeric keys covers a record that has been
# round-tripped through the generated dataset. Both are additive -- nothing that
# resolved before resolves differently now.
#
# This module must be imported AFTER openclaw.py for the delegation to find the
# incumbent; the package __init__ does that.
# ---------------------------------------------------------------------------

_INCUMBENT = Instance._registry.get("openclaw/openclaw")


@Instance.register("openclaw", "openclaw")
class OpenclawDispatch(Instance):
    """Not an implementation -- a router. __new__ returns someone else's instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return OPENCLAW_29925_TO_27293(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"openclaw/openclaw#{pr.number} is outside {_LO}-{_HI} and no other "
            f"openclaw adapter is registered under the bare name"
        )


for _number in range(_LO, _HI + 1):
    Instance._registry.setdefault(f"openclaw/{_number}", OPENCLAW_29925_TO_27293)
del _number
