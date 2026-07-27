import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# A git commit SHA charset (full or abbreviated). Validated before raw
# interpolation into a generated Dockerfile RUN command -- see the comment on
# ImageDefault.dockerfile() for why this path doesn't go through an ARG.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe commit sha for Dockerfile interpolation: {sha!r}")
    return sha


# ---------------------------------------------------------------------------
# Era: vercel-labs/agent-browser PRs <= 563 (pre "Rust native rewrite").
#
# At these base commits the daemon/library is TypeScript and the regression
# tests added by the dataset's test_patch are vitest specs (`src/**/*.test.ts`,
# `test/**/*.test.ts`).  The repo also carries a thin Rust `cli/` wrapper, but
# the test command exercised here is the TS suite.
#
# Discovery (Docker, host arch arm64, verified):
#   * Toolchain is constant across the whole era (PR 3 / v0.6.0 .. PR 563 /
#     v0.15.x): pnpm (pnpm-lock.yaml, no packageManager field), node 20,
#     vitest ^4, playwright/playwright-core ^1.57.
#   * `pnpm install` then `pnpm exec playwright install --with-deps chromium`
#     installs the bundled Chromium (works on amd64 *and* arm64, unlike
#     Chrome-for-Testing).  Browser specs (browser.test.ts etc.) pass headless.
#   * `pnpm exec vitest run --reporter=verbose` emits one line per test:
#       ` ✓ src/foo.test.ts > suite > name 1ms`   (pass)
#       ` × src/foo.test.ts > suite > name 3ms`    (fail)
#       ` ↓ src/foo.test.ts > suite > name`        (skipped/todo)
#   * Full suite verified green (481 passed | 17 skipped) and the PR
#     test_patch + fix_patch apply cleanly with `git apply --whitespace=nowarn`.
#
# Two-stage build (base image + per-PR image), added so the (network-bound)
# clone and the (slow) pnpm-install/Chromium-download only happen once per
# era, cached across every PR, instead of once per PR:
#   * ImageBase clones the repo at whatever HEAD currently is (no commit
#     pinned) and warms pnpm/Chromium. It is commit-agnostic and reused
#     as-is for every PR image via normal Docker layer/tag caching -- it is
#     NEVER shipped standalone, only ever as the FROM of a PR image.
#   * ImageDefault (the per-PR image) is FROM ImageBase, then re-derives the
#     commit-correct state: checks out BASE_COMMIT and re-runs the FULL
#     _HARDENING_BLOCK (strip every other ref/remote, expire reflogs,
#     gc --prune) against it, exactly as the single-stage build did. This is
#     the one detail that matters: a prior version of this registry (see
#     commit e7ba6835) chained to a base image the same way but never
#     re-applied hardening in the child, so the child image inherited the
#     base image's full, unstripped git history -- letting an agent read the
#     real fix straight out of `git log`/`git show`. dependency() returning
#     an Image (rather than a string) makes Image.dockerfile() refuse to run
#     ("Subclass must override dockerfile() or return a string from
#     dependency()"), which is exactly why that gap was possible; the fix is
#     that this class's own dockerfile() below re-applies hardening itself,
#     not that chaining is unsafe in general.
# ---------------------------------------------------------------------------


class ImageBase(Image):
    """Shared, commit-agnostic base image for the TS era.

    Clones the repo once and warms the pnpm/Chromium caches. Carries no
    BASE_COMMIT and no hardening -- it is never evaluated against directly,
    only used as the FROM of ImageDefault, which re-hardens per commit.
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

    def dependency(self) -> str:
        return "node:20-bookworm"

    def image_tag(self) -> str:
        # Constant (not PR-derived): every TS-era PR resolves the same tag,
        # so Docker/the harness build this once and every PR image reuses it.
        # Named after the era's PR range (matches this file's own name), not
        # the toolchain, per convention.
        return "base-0-563"

    def workdir(self) -> str:
        return "base-0-563"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Syntax directive prefix makes DockerfileEnhancer.enhance() skip its
        # proxy ARGs / CA-cert symlinks / MITM mount (same reasoning as
        # ImageDefault.dockerfile() below) so this base image stays exactly
        # what's written here.
        repo = self.pr.repo
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM node:20-bookworm
ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV CI=true
# Containers run as root, where $HOME defaults to /root -- some of this
# repo's own tests assert their home-relative fallback path contains "home"
# or "Users" (see connection.rs's test_get_socket_dir_home_fallback in the
# Rust era; harmless here but kept for parity/consistency across both base
# images), which /root never satisfies. Not evaluation-relevant either way
# for the TS era, just avoids the two base images diverging for no reason.
ENV HOME=/home/{repo}
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (TS-era base)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    dbus \\
    dbus-x11 \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@9

# rm -rf first: makes this step idempotent against a stale/reused BuildKit
# cache layer that already has something at this path (observed on the
# arm64 leg of multi-arch builds: `git clone` failing with "destination
# path ... already exists and is not an empty directory" even though
# nothing earlier in this Dockerfile creates it) -- rebuilding this same
# tag repeatedly with different content across the base image's lifetime is
# exactly the kind of history that produces a stale cache hit like this.
RUN rm -rf /home/{repo} && git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}
RUN pnpm install || true
RUN pnpm exec playwright install --with-deps chromium || npx --yes playwright install --with-deps chromium || true

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

    def dockerfile(self) -> str:
        # dependency() returns an Image, so the shared Image.dockerfile()
        # refuses to run and this class must build the Dockerfile itself.
        # REPO_URL/BASE_COMMIT never arrive as build-args in this case (the
        # harness only injects those when dependency() is a string), so the
        # commit is baked in directly rather than via an ARG -- validated
        # first (_safe_sha) since it's raw string interpolation.
        #
        # Image._HARDENING_BLOCK is reused verbatim (imported, not
        # hand-copied) so this can never silently drift from the canonical
        # definition in image.py; "${BASE_COMMIT}" is substituted with the
        # literal, validated sha since no ARG declares it here.
        base = self.dependency()
        repo = self.pr.repo
        sha = _safe_sha(self.pr.base.sha)
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base.image_full_name()}

WORKDIR /home/{repo}
RUN git fetch origin || true

{hardening}

RUN if [ -f .gitmodules ]; then \\
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

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
COPY prepare.sh /home/prepare.sh
RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Warm the pnpm install + bundled Chromium at image-build time so the eval
# runs don't need network. The repo is already checked out at ${{BASE_COMMIT}}
# and hardened by ImageDefault.dockerfile() above, so this script no longer
# performs any git checkout itself. pnpm/Chromium are already warmed once in
# the base image's HEAD snapshot; this re-run picks up whatever changed
# between that snapshot and this commit's actual manifest. Steps are allowed
# to fail (|| true) because their only purpose here is to populate
# node_modules + the browser cache; the real pass/fail signal comes from the
# run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true
pnpm install || true
# Bundled Chromium + its OS deps (works on amd64 and arm64).
pnpm exec playwright install --with-deps chromium || npx --yes playwright install --with-deps chromium || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# Headless Chromium needs a D-Bus session bus; without one, browser launches
# intermittently crash with SIGSEGV ("Failed to connect to the bus: Failed to
# connect to socket /run/dbus/system_bus_socket: No such file or directory").
# Verified fix (PR 373, TS era): the exact same launch that crashed before
# passes cleanly once this daemon is running. || true: a daemon that's
# already running (or fails to start for an unrelated reason) shouldn't fail
# the whole test run -- the real pass/fail signal is the test suite itself.
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
        ]


@Instance.register("vercel-labs", "agent_browser_0_to_563")
class AGENT_BROWSER_0_TO_563(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        # vitest@4 verbose reporter, ANSI stripped (leading space trimmed):
        #   "✓ src/foo.test.ts > suite > name 1ms"           -> passed
        #   "↓ src/foo.test.ts > suite > name"               -> skipped/todo
        #   "FAIL  src/foo.test.ts > suite > name"           -> failed
        # Failures are NOT printed with an inline "×" by the verbose reporter;
        # they only appear in the trailing "Failed Tests" block prefixed with
        # "FAIL  " (and no duration).  "×"/"✗" are still accepted defensively.
        # The "<file>.test.ts > ..." shape (note the " > ") excludes the
        # file-level summary lines such as "✓ src/foo.test.ts (37 tests) 13ms".
        dur_re = re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$")
        body_re = r"(\S+\.(?:test|spec)\.tsx?\s+>\s+.+)$"
        pass_re = re.compile(r"^[✓]\s+" + body_re)
        skip_re = re.compile(r"^[↓·]\s+" + body_re)
        fail_re = re.compile(r"^(?:FAIL|FAILED|[×✗])\s+" + body_re)

        for raw in test_log.splitlines():
            clean = ansi_re.sub("", raw).strip()
            if not clean:
                continue

            m = fail_re.match(clean)
            if m:
                failed_tests.add(dur_re.sub("", m.group(1)).strip())
                continue
            m = pass_re.match(clean)
            if m:
                passed_tests.add(dur_re.sub("", m.group(1)).strip())
                continue
            m = skip_re.match(clean)
            if m:
                skipped_tests.add(dur_re.sub("", m.group(1)).strip())
                continue

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
