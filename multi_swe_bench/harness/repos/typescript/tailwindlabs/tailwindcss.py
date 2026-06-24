"""tailwindlabs/tailwindcss harness config, conformed to the hardened image.py.

A single per-PR image whose dependency() returns a *string* base image, so the
shared Image.dockerfile() owns the build: install apt deps, clone
"${REPO_URL}", checkout "${BASE_COMMIT}", run extra_setup() (deps install),
then the _HARDENING_BLOCK that strips every other ref/commit so the fix can't
be recovered from git history.

ROUTING: this dataset carries no `number_interval` and no `tag`, so
Instance.create() resolves the key to "{org}/{repo}" = "tailwindlabs/tailwindcss".
We therefore register ONE generic class and dispatch the tooling era internally.
(The previous per-PR-interval registrations never matched and left all 117
instances unroutable.)

DISPATCH IS BY RELEASE VERSION, NOT pr.number. These are release-window
bundles whose pr.number (first PR in the window) is not monotonic with the
release line, but base.label is always "v<start>..v<end>". We parse the start
version and pick tooling from it. Timeline verified against the repo:

    < v2.2      node:18   npm install            + jest      (v0/v1/v2.0-2.1)
    v2.2.x      node:18   npm install + babelify + jest       (src/ -> lib/)
    v3.x        node:18   npm install + generate + jest       (plugin list)
    >= v4.0     node:20   pnpm install           + vitest     (monorepo)
"""

from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Never let a lockfile in the patch disturb the installed node_modules tree.
_APPLY_EXCLUDES = (
    "--exclude='*pnpm-lock.yaml' "
    "--exclude='*yarn.lock' "
    "--exclude='*package-lock.json'"
)

# Tooling-era boundaries, keyed on the base (start) release version.
_V_GENERATE = (3, 0, 0)   # npm+babelify+jest -> npm+generate+jest
_V_VITEST = (4, 0, 0)     # jest (npm) -> vitest (pnpm monorepo)


def _start_version(label: str) -> tuple[int, int, int]:
    """Parse the start version from a base.label like 'v3.1.4..v3.1.5'."""
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", label or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _era(pr: PullRequest) -> str:
    # ALL of v0/v1/v2 ship a `babelify` script (babel src/ -> lib/) and the
    # suites import the compiled lib/, so every phase must rebuild lib/ AFTER
    # the patch is applied. v3 dropped lib/ (tests import src/) and uses a
    # `generate` plugin-list step. v4 is the pnpm/vitest monorepo.
    v = _start_version(pr.base.label)
    if v < _V_GENERATE:
        return "jest_build"
    if v < _V_VITEST:
        return "jest_generate"
    return "vitest"


# ---------------------------------------------------------------------------
# Shared parse_log — handles Jest (v0-v3) and Vitest (v4) output
# ---------------------------------------------------------------------------


def tailwindcss_parse_log(test_log: str) -> TestResult:
    """Parse Jest/Vitest test output for Tailwind CSS.

    Jest (v0-v3):
        PASS tests/basic-usage.test.js
          ✓ test name (511 ms)
          ✗ test name
    Vitest (v4):
         ✓ |tailwindcss| src/utilities.test.ts > test name
         ✗ |tailwindcss| src/utilities.test.ts > test name
         Test Files  1 passed (1)
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    # Jest patterns
    re_jest_pass_suite = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$")
    re_jest_fail_suite = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$")
    re_jest_pass_test = re.compile(r"^\s*[✔✓√]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$")
    re_jest_fail_test = re.compile(r"^\s*[×✕✗✘✖]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$")
    re_jest_skip_test = re.compile(r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$")
    re_jest_fail_indicator = re.compile(r"^\s*●\s+(.+?)\s+›\s+(.+)$")

    # Vitest patterns
    re_vitest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
    re_vitest_fail = re.compile(r"^\s*[×✗]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
    re_vitest_skip = re.compile(r"^\s*[-↓]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$")
    re_vitest_fail_file = re.compile(r"^\s*FAIL\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|mjs))")
    re_vitest_summary_passed = re.compile(r"^\s*Test Files\s+(\d+)\s+passed")
    re_vitest_summary_failed = re.compile(r"^\s*Test Files\s+(\d+)\s+failed")

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        m = re_jest_pass_suite.match(line)
        if m:
            passed_tests.add(m.group(1).strip())
            continue

        m = re_jest_fail_suite.match(line)
        if m:
            failed_tests.add(m.group(1).strip())
            passed_tests.discard(m.group(1).strip())
            continue

        m = re_jest_pass_test.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_jest_fail_test.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_jest_skip_test.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_jest_fail_indicator.match(line)
        if m:
            name = "{suite} > {test}".format(
                suite=m.group(1).strip(), test=m.group(2).strip()
            )
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_pass.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_vitest_fail.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_skip.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_vitest_fail_file.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_summary_passed.match(line)
        if m and not passed_tests:
            passed_tests.add("__vitest_suite_passed__")
            continue

        m = re_vitest_summary_failed.match(line)
        if m and not failed_tests:
            failed_tests.add("__vitest_suite_failed__")
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# TWO-TIER images: ONE shared base per node-era (clone once + warm the common
# dependency cache, NO base commit, reused by every PR of that era) and a thin
# per-PR image on top (FROM base, checkout THIS PR's base commit, install only
# the PR-specific deps, then harden git history). Mirrors the approved tfsec
# pattern. Avoids per-PR base images (no duplicated clone/dep work, less time,
# less storage).
# ---------------------------------------------------------------------------


def _base_spec(pr: PullRequest) -> tuple[str, str, str]:
    """(node_image, base_tag_suffix, warm_ref) for the SHARED base of pr's era.

    Two bases, keyed by node version (the only hard incompatibility — v0-v3 is
    node:18 + npm/jest, v4 is node:20 + pnpm/vitest). Each clones the repo once
    and warms the dependency cache from a representative ref so every PR of the
    era reuses it. `warm_ref=""` means warm at the default branch (v4 for vitest).
    """
    if _era(pr) == "vitest":
        return ("node:20", "vitest", "")
    return ("node:18", "jest", "v3.4.18")


class TailwindcssImageBase(Image):
    """SINGLE shared base per node-era (tag `base-jest` / `base-vitest`), built
    ONCE and reused as the FROM parent of every per-PR image of that era. It
    clones the full repo history + keeps `origin` (so each per-PR image can
    `git checkout` its own base commit) and warms the npm/pnpm cache from a
    representative ref. It carries NO per-PR base commit and NO git hardening.

    The leading `# syntax=docker/dockerfile:1.6` makes DockerfileEnhancer.enhance()
    return this Dockerfile VERBATIM (it early-returns when the directive is
    present). That deliberately (a) stops the enhancer rewriting the clone into a
    `checkout ${BASE_COMMIT}` + history-strip that would pin this shared base to
    whichever PR built it first, and (b) omits all proxy/cert/MITM injection. The
    hardening is applied PER-PR in TailwindcssImageDefault, after that PR's
    base commit is checked out -- keeping this base reusable by every PR.
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
        return _base_spec(self.pr)[0]

    def image_tag(self) -> str:
        return f"base-{_base_spec(self.pr)[1]}"

    def workdir(self) -> str:
        return f"base-{_base_spec(self.pr)[1]}"

    def files(self) -> list[File]:
        return []

    def _warm_install(self) -> str:
        if _base_spec(self.pr)[0] == "node:20":
            return (
                "corepack enable 2>&1 || true; "
                "corepack prepare pnpm@9.6.0 --activate 2>&1 || true; "
                "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm@9.6.0 2>&1 || true; "
                "pnpm install --no-frozen-lockfile --ignore-scripts 2>&1 || true"
            )
        return "npm install --ignore-scripts 2>&1 || true"

    def dockerfile(self) -> str:
        node_image, _suffix, warm_ref = _base_spec(self.pr)
        org, repo = self.pr.org, self.pr.repo
        checkout = (
            f"git checkout {warm_ref} 2>/dev/null || true; \\\n    "
            if warm_ref else ""
        )
        # Emitted verbatim (leading `# syntax` -> enhancer early-returns), so NO
        # proxy build-args, NO proxy/SSL ENVs, NO CA-cert symlinks, NO MITM mount.
        # `ca-certificates` is the standard CA bundle needed for HTTPS git clone /
        # npm, not injected proxy/cert config.
        return f"""# syntax=docker/dockerfile:1.6
FROM {node_image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} shared base image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget jq \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
# Warm the SHARED dependency cache from a representative ref so the common deps
# are downloaded ONCE here instead of by every PR. Per-SHA differences are
# filled in by each PR's prepare.sh. All best-effort (`|| true`) so a transient
# install issue never fails the shared base build.
RUN {checkout}{self._warm_install()}

CMD ["/bin/bash"]
"""


class TailwindcssImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # --- image plumbing ------------------------------------------------
    def dependency(self) -> Image:
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves our dockerfile() verbatim.
        # So no proxy/cert/MITM injection, and the hardening below is applied by
        # hand (anchored on HEAD), which is what lets the base stay shared.
        return TailwindcssImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Defense-in-depth against re-fetching the fix from GitHub by URL. The
    # hardening block deletes the cloned repo's `origin`, but a model could still
    # run `git fetch https://github.com/<org>/<repo> <future_sha>` to pull the
    # commits that come AFTER the base (where the fix lives). We blackhole every
    # github URL scheme at the git --system level so any git transport to github
    # is rewritten to an unroutable address and fails fast. (Authoritative block
    # is still eval-time network isolation, `docker run --network none`; this is
    # the belt-and-suspenders that survives even a networked run.)
    _GIT_NET_LOCKDOWN = (
        "RUN BH=\"https://0.0.0.0:1/\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"https://github.com/\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"http://github.com/\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"git://github.com/\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"ssh://git@github.com/\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"git@github.com:\"; \\\n"
        "    git config --system url.\"$BH\".insteadOf \"https://codeload.github.com/\"; \\\n"
        "    git config --system protocol.allow never; \\\n"
        "    git config --system protocol.file.allow always; \\\n"
        "    git config --system --unset-all credential.helper 2>/dev/null || true"
    )

    def _harden(self) -> str:
        """Git-history hardening for the per-PR image, applied AFTER prepare.sh
        has checked out THIS PR's base commit -> the commit to KEEP is the
        current HEAD (BASE_COMMIT is not a build-arg in FROM-an-image builds).
        The shared base deliberately keeps full history + origin; this strips the
        remote and every ref/commit not reachable from HEAD, so the evaluated
        agent cannot recover the fix from git log/show. Mirrors the harness
        _HARDENING_BLOCK, anchored on HEAD."""
        repo = self.pr.repo
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
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
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

    def dockerfile(self) -> str:
        # Two-tier: FROM the SHARED base (no proxy/cert/MITM — see
        # TailwindcssImageBase). dependency() returns an Image, so
        # DockerfileEnhancer.enhance() early-returns and leaves this verbatim.
        # Order: stage patches + prepare.sh and RUN it (checkout this PR's base
        # commit + install ONLY the PR-specific deps) -> git network lockdown ->
        # per-PR git-history hardening -> COPY the eval scripts last (so editing
        # them reuses the cached prepare layer).
        base = self.dependency()
        name, tag = base.image_name(), base.image_tag()
        return f"""FROM {name}:{tag}

COPY test.patch /home/test.patch
COPY fix.patch /home/fix.patch
COPY prepare.sh /home/prepare.sh
RUN bash /home/prepare.sh

{self._GIT_NET_LOCKDOWN}

{self._harden()}

COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh

CMD ["/bin/bash"]
"""

    # --- era-specific shell ------------------------------------------------
    def _test_files(self) -> str:
        # Select only real JS/TS test modules. The tailwind suites carry sibling
        # ".test.css"/".test.html" *fixtures* (e.g. tests/jit/basic-usage.test.css)
        # that must NOT be handed to the test runner. Also exclude integrations/*
        # suites: they spawn the tailwind CLI / framework watchers (webpack, vite,
        # postcss --watch) that hang and need network, so they can never yield a
        # valid F2P in a sealed offline image. Only 2 of 117 PRs touch integration
        # tests exclusively (they end up F2P=0 — a dataset limit, not a harness bug).
        test_re = re.compile(r"\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs|mts|cts)$")
        files = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch):
            if not test_re.search(m):
                continue
            if m.startswith("integrations/") or "/integrations/" in m:
                continue
            files.append(m)
        return " ".join(files)

    def _test_cmd(self) -> str:
        # Every phase is wrapped in `timeout` so a test that spawns a watcher or
        # CPU-loops (the v0-v2 purgeUnusedStyles suite does) can never hang the
        # eval; --kill-after force-kills stragglers. jest --forceExit closes any
        # open handles after the suite completes. 180s is ~3x the slowest real
        # suite observed (<60s) but bounds the purge CPU-loop fast — that loop
        # never finishes regardless, so a shorter cap loses no signal and saves
        # ~5h across the 9 purge PRs (it runs once per attempt × phase).
        wrap = "NODE_OPTIONS=--max_old_space_size=4096 timeout --kill-after=30 180"
        if _era(self.pr) == "vitest":
            return (
                f"{wrap} npx vitest run --reporter=verbose "
                "--hideSkippedTests --testTimeout=60000"
            )
        # NOTE: no jest --testTimeout — it was only added in jest 22, and the
        # v0/v1 era ships jest ~20 which aborts with "Unrecognized option".
        # The outer `timeout` wrapper is the real hang backstop anyway (jest's
        # per-test timeout can't interrupt a CPU-bound suite). --forceExit is
        # honored by every jest version here.
        return f"{wrap} npx jest --verbose --no-coverage --forceExit"

    def _pm_install(self) -> str:
        if _era(self.pr) == "vitest":
            # Bootstrap pnpm FIRST, then install. node:20 bundles corepack;
            # fall back to a global npm install if corepack is unavailable.
            # Without this, `pnpm install` no-ops with "command not found" and
            # root devDeps declared with the catalog: protocol (lightningcss)
            # plus plain ones (dedent) never land, breaking every suite that
            # imports them. lightningcss ships prebuilt platform binaries as
            # optional deps, so --ignore-scripts (which skips the heavy Rust
            # oxide native build) still leaves it importable.
            return (
                "corepack enable 2>&1 || true\n"
                "corepack prepare pnpm@9.6.0 --activate 2>&1 || true\n"
                "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm@9.6.0 2>&1 || true\n"
                "pnpm install --no-frozen-lockfile --ignore-scripts 2>&1 || true"
            )
        return "npm install --ignore-scripts 2>&1 || true"

    def _added_deps(self) -> list[str]:
        """`name@version` for every dependency the patches ADD to package.json.

        Installed directly (npm install --no-save) so a fix that introduces a
        new runtime dep (color, @fullhuman/postcss-purgecss, postcss-nested, …)
        is present even when the prefetch patch-apply fails on an old bundle.
        Safe for the babel-7 eras: it only pulls the NEW isolated packages and
        never re-resolves the existing @babel tree (which is what broke v2.2).
        Only `+` lines whose value looks like a version spec are taken, so
        script/meta entries are ignored; the package's own "version" is skipped.
        """
        skip = {"version", "name", "description", "license", "main", "module",
                "types", "typings", "homepage", "author", "type", "private",
                "repository", "bugs", "keywords", "engines", "bin", "scripts"}
        ver_re = re.compile(r'^\s*\+\s*"([^"]+)"\s*:\s*"([^"]+)"')
        deps: dict[str, str] = {}
        for patch in (self.pr.test_patch, self.pr.fix_patch):
            in_pkg = False
            for line in patch.splitlines():
                if line.startswith("diff --git"):
                    in_pkg = line.rstrip().endswith("/package.json")
                    continue
                if not in_pkg or not line.startswith("+") or line.startswith("+++"):
                    continue
                m = ver_re.match(line)
                if not m:
                    continue
                name, ver = m.group(1), m.group(2)
                if name in skip:
                    continue
                if re.match(r"^[\^~>=<*\d]", ver) or ver.startswith(
                    ("github:", "git+", "git:", "file:", "npm:", "workspace:", "link:")
                ):
                    deps[name] = ver
        return [f"{n}@{v}" for n, v in deps.items()]

    def _build_cmd(self) -> str:
        """Re-run AFTER each git apply so generated/compiled artifacts reflect
        the patched src. v0-v2 import the babel-compiled lib/; v3 imports an
        autogenerated plugin list; v4 needs neither."""
        era = _era(self.pr)
        if era == "jest_build":
            # babel src/ -> lib/. The v2.2 postbabelify ncc step needs the
            # legacy OpenSSL provider on node:18 (harmless for v0/v1).
            return "NODE_OPTIONS=--openssl-legacy-provider npm run babelify 2>&1 || true"
        if era == "jest_generate":
            return (
                "npm run generate 2>&1 || npm run generate:plugin-list 2>&1 "
                "|| npm run pretest 2>&1 || true"
            )
        return ""  # vitest

    def _prepare_body(self) -> str:
        install = self._pm_install()
        # vitest/v4 uses pnpm, which PRUNES to match the current package.json on
        # every install. The pre-fetch dance below (install against the patched
        # manifest, then restore base) would therefore strip base-only deps like
        # `dedent`, so v4 gets a single plain install (its fixes rarely add deps).
        if _era(self.pr) == "vitest":
            return install
        # npm (jest eras) only ADDS packages, never prunes — so we PRE-FETCH the
        # deps the patches add (offline-safe): apply both patches, install, then
        # restore pristine base (node_modules is git-ignored, so the new packages
        # persist into the sealed image). Then warm the base build. Eval phases
        # run with no network.
        #
        # The prefetch apply is VERSION-SPLIT on how the toolchain reacts to npm
        # re-resolving `^` ranges:
        #   * v1+ (babel 7, `@babel/*` scoped): MUST apply the patched lockfile so
        #     npm installs the fix's *pinned* versions. Excluding it (package.json
        #     =fix vs lock=base) lets npm re-resolve to latest and pull a @babel
        #     plugin needing babel 7.22 into v2.2's pinned 7.14.6 — breaks all suites.
        #   * v0.x (babel 6, `babel-core`): has no scoped @babel plugins to mis-
        #     resolve, and its ancient lockfile doesn't drive a clean `npm install`
        #     of the new runtime deps. So we EXCLUDE the lockfile and let npm
        #     re-resolve package.json fresh — that's the only path that actually
        #     installs postcss-nested/postcss-selector-parser for it.
        if _start_version(self.pr.base.label) < (1, 0, 0):
            apply_line = (
                "git apply --whitespace=nowarn {excl} /home/test.patch "
                "/home/fix.patch 2>/dev/null || true"
            ).format(excl=_APPLY_EXCLUDES)
        else:
            apply_line = (
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch "
                "2>/dev/null \\\n"
                "  || git apply --whitespace=nowarn --reject /home/test.patch "
                "/home/fix.patch 2>/dev/null || true"
            )
        prefetch = (
            "# pre-fetch patch-added dependencies, then restore the pristine base\n"
            "{apply_line}\n"
            "{install}\n"
            "git reset --hard 2>/dev/null || true\n"
            "git clean -fd 2>/dev/null || true"
        ).format(apply_line=apply_line, install=install)
        lines = [install, prefetch]
        # Belt-and-suspenders: install fix-ADDED deps that are still MISSING
        # after the prefetch (e.g. v1.4's `color` / `@fullhuman/postcss-purgecss`
        # when the patch-apply failed on an old bundle). The resolve-check is
        # essential: the parser also catches version *bumps* of already-present
        # toolchain deps (jest@27, postcss@8 on v2.2), and re-installing those
        # would re-resolve the pinned @babel tree and break the suite. Installing
        # only the genuinely-absent packages avoids that. --no-save keeps
        # package.json pristine; node_modules survives the reset (git-ignored).
        added = self._added_deps()
        if added:
            deps_str = " ".join("'{}'".format(d) for d in added)
            # Collect the genuinely-missing deps FIRST, then install them in ONE
            # npm call. A per-dep loop must NOT be used: each `npm install
            # --no-save` prunes the previously-added --no-save package as
            # extraneous, so only the last would survive. The resolve-check keeps
            # already-present toolchain bumps (jest/postcss on v2.2) untouched so
            # the pinned @babel tree is never re-resolved. --legacy-peer-deps lets
            # old scoped plugins (@fullhuman/postcss-purgecss@2) install despite
            # strict peer conflicts.
            lines.append(
                "_miss=\"\"\n"
                "for dep in " + deps_str + "; do\n"
                "  nm=\"${dep%@*}\"\n"
                "  node -e \"require.resolve('$nm')\" >/dev/null 2>&1 "
                "|| _miss=\"$_miss $dep\"\n"
                "done\n"
                "[ -n \"$_miss\" ] && npm install --no-save --ignore-scripts "
                "--legacy-peer-deps $_miss 2>&1 || true"
            )
        build = self._build_cmd()
        if build:
            lines.append(build)
        return "\n".join(lines)

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_files = self._test_files()
        test_cmd = self._test_cmd()
        build_cmd = self._build_cmd()
        # Build line runs after each git apply so lib/ (or the plugin list) is
        # regenerated from the PATCHED src before the suite imports it.
        build_line = (build_cmd + "\n") if build_cmd else ""
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Two-tier: the SHARED base cloned the repo + warmed the dep cache but is NOT at
# this PR's commit. Here we check out THIS PR's base commit (full history is
# present from the base clone), then install ONLY the PR-specific deps (reusing
# the base's warmed npm/pnpm cache). The per-PR git-history hardening runs after
# this, in the Dockerfile.
set -e
cd /home/{repo}
git reset --hard || true
git checkout --force {base_sha} 2>/dev/null \\
  || git checkout --force --detach {base_sha}
git reset --hard || true
{body}
""".format(repo=repo, base_sha=self.pr.base.sha, body=self._prepare_body()),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard HEAD || true
{build_line}{test_cmd} {test_files} 2>&1 || true
""".format(repo=repo, build_line=build_line, test_cmd=test_cmd, test_files=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard HEAD || true
git apply {excludes} --whitespace=nowarn /home/test.patch \\
  || git apply {excludes} --whitespace=nowarn --reject /home/test.patch || true
{build_line}{test_cmd} {test_files} 2>&1 || true
""".format(repo=repo, excludes=_APPLY_EXCLUDES, build_line=build_line, test_cmd=test_cmd, test_files=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard HEAD || true
git apply --3way {excludes} --whitespace=nowarn /home/test.patch /home/fix.patch \\
  || {{ git apply {excludes} --whitespace=nowarn --reject /home/test.patch || true; \\
        git apply {excludes} --whitespace=nowarn --reject /home/fix.patch || true; }}
{build_line}{test_cmd} {test_files} 2>&1 || true
""".format(repo=repo, excludes=_APPLY_EXCLUDES, build_line=build_line, test_cmd=test_cmd, test_files=test_files),
            ),
        ]


@Instance.register("tailwindlabs", "tailwindcss")
class Tailwindcss(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TailwindcssImageDefault(self.pr, self._config)

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
        return tailwindcss_parse_log(test_log)


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file edited).
#
# The output dataset jsonl's `number_interval` is written from the loaded
# PullRequest, but the bundle's PR list (`prs_in_bundle`) is dropped when the
# raw record is parsed into a PullRequest and the harness never derives it.
# As this must live ONLY in the registry, we install two small, idempotent,
# tailwindlabs-scoped shims at import time (this file is the only one changed):
#
#   1. PullRequest.from_json -- for tailwindlabs/tailwindcss records whose
#      number_interval is empty, fill it from the raw line's prs_in_bundle as
#      "146-147-150-155-157" (the EXACT PRs in the bundle, NOT a 146-157 range).
#      That value then flows straight into the output dataset record.
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `tailwindlabs/<that-list>`, which is not a registered key; fall back to
#      `tailwindlabs/tailwindcss` so the build still routes. Other repos are
#      unaffected: shim 1 only fills tailwindlabs/tailwindcss, and era-keyed
#      datasets keep their pre-set number_interval (only EMPTY values are
#      filled) whose `org/<era>` key is registered (fallback never triggers).
# ---------------------------------------------------------------------------
import json as _tw_json
from multi_swe_bench.harness.pull_request import PullRequest as _TWPullRequest

if not getattr(_TWPullRequest, "_tailwindlabs_ni_shim", False):
    _tw_orig_from_json = _TWPullRequest.from_json.__func__

    def _tw_from_json(cls, json_str):
        pr = _tw_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "tailwindlabs"
                and getattr(pr, "repo", "") == "tailwindcss"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_tw_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    _TWPullRequest.from_json = classmethod(_tw_from_json)
    _TWPullRequest._tailwindlabs_ni_shim = True

if not getattr(Instance, "_tailwindlabs_route_shim", False):
    _tw_orig_create = Instance.create.__func__

    def _tw_create(cls, pr, config, *args, **kwargs):
        try:
            return _tw_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == "tailwindlabs"
                and getattr(pr, "repo", "") == "tailwindcss"
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_tw_create)
    Instance._tailwindlabs_route_shim = True
