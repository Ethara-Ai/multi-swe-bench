import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FirecrawlImageBase(Image):
    """Level 1: toolchain + system-dependency base image (shared by every PR).

    ``dependency()`` returns a *string* (the Node base image) and this Dockerfile
    carries NO ``# syntax`` directive, so the pipeline's DockerfileEnhancer
    engages and prepends the ``# syntax``/ARG/ENV/LABEL infra block.

    IMPORTANT: this image must NOT clone the repository. A shared
    string-dependency image that performs a ``git clone`` is force-pinned to a
    single ``${BASE_COMMIT}`` and history-stripped by the enhancer, which would
    break ``git checkout`` for every other PR sharing the base. So the clone +
    per-commit dependency install live in FirecrawlImageDefault (whose
    dependency() is an Image, and is therefore left verbatim by the enhancer).
    This image only provides the Node toolchain + the apt packages firecrawl's
    native deps need, and enables pnpm via corepack.
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
        return "node:20"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {image_name}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*
# Pin ONE node-20-safe pnpm (9.x) and disable pnpm's self-version-management.
# Do NOT use corepack: firecrawl's apps/api carries a `packageManager` pin only
# in newer commits; older commits have none, so corepack would default to the
# latest pnpm (11.x) which requires Node 22 and crashes on node:20
# (ERR_UNKNOWN_BUILTIN_MODULE: node:sqlite). A fixed pnpm@9 reads every era's
# lockfile (with --no-frozen-lockfile) and runs on node:20.
RUN npm install -g pnpm@9 \\
    && pnpm config set manage-package-manager-versions false

CMD ["/bin/bash"]
"""


class FirecrawlImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return FirecrawlImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
            # Shared test invocation, sourced by all three stage scripts.
            #  - runs the apps/api jest suite via the locally installed binary
            #    (avoids the packageManager mismatch of `pnpm test`);
            #  - ignores e2e suites: they need a live API + Postgres/Redis/auth
            #    keys and can't pass in a bare build. Unit tests are the runnable,
            #    service-free target;
            #  - transformIgnorePatterns lets jest transpile ESM-only deps
            #    (e.g. uuid v13) instead of choking on `Unexpected token export`.
            File(
                ".",
                "test_lib.sh",
                """#!/bin/bash
run_firecrawl_tests() {
    cd /home/[[REPO_NAME]]/apps/api || return 1
    if [ ! -x node_modules/.bin/jest ]; then
        pnpm install --no-frozen-lockfile || true
    fi
    # --verbose: jest prints a "✓/✕ <name>" line for EVERY test (pass and fail).
    # Without it, jest emits only "●" bullets for failures + summary counts, so
    # passing tests would have no name for parse_log/report to record as NONE->PASS.
    node_modules/.bin/jest --ci --verbose --colors=false \\
        --testPathIgnorePatterns='/node_modules/' --testPathIgnorePatterns='e2e' \\
        --transformIgnorePatterns='node_modules/(?!(uuid|nanoid|@sindresorhus|got)/)'
}
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
source /home/test_lib.sh
run_firecrawl_tests
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
source /home/test_lib.sh
cd /home/[[REPO_NAME]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply (test.patch) failed" >&2
    exit 1
fi
run_firecrawl_tests
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
source /home/test_lib.sh
cd /home/[[REPO_NAME]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply (test.patch + fix.patch) failed" >&2
    exit 1
fi
run_firecrawl_tests
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline and installs
        # the workspace deps at that commit. Because this image's dependency() is
        # an Image, the DockerfileEnhancer returns the Dockerfile verbatim -- the
        # clone + hardening below are kept as written (pinning here is correct:
        # it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

# firecrawl has NO root package.json -- the tested TypeScript project is
# apps/api (its own package.json + pnpm-lock.yaml + jest config). Install there.
# `|| true`: a native/optional postinstall script may fail on some revisions,
# but the JS deps (incl. jest/ts-jest) still land; the run scripts re-install
# if node_modules is somehow absent.
WORKDIR /home/{self.pr.repo}/apps/api
RUN pnpm install --no-frozen-lockfile || true

WORKDIR /home/{self.pr.repo}

COPY {copy_files} /home/

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


class FirecrawlFirecrawlInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FirecrawlImageDefault(self.pr, self._config)

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
        # Parse real jest output. Two signals, in priority order:
        #   1. per-test status glyph lines ("  ✓ name", "  ✕ name", "  ○ name")
        #      -> named tests, so f2p/n2p can be diffed across run/test/fix.
        #   2. suite-level markers ("PASS/FAIL <path>") + a failing-test bullet
        #      ("  ● suite > test") as a fallback when glyph lines are absent.
        # NO guess fallback: if nothing parses, counts stay 0 (an env/build
        # failure must not masquerade as a pass).
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1b\[[0-9;]*m")

        # jest status glyphs: ✓/✔ pass, ✕/✗/× fail, ○/↓ skip/todo
        glyph_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
        glyph_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
        glyph_skip = re.compile(r"^\s*[○↓]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
        bullet_fail = re.compile(r"^\s*●\s+(.+?)\s*$")

        for raw in log.splitlines():
            line = ansi.sub("", raw).rstrip()
            m = glyph_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue
            m = glyph_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue
            m = glyph_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue
            m = bullet_fail.match(line)
            if m:
                # "suite > test" failure header (also emitted for suites that
                # fail to run). Ignore jest's own "Console"/"Validation Error".
                name = m.group(1).strip()
                if name and not name.lower().startswith(("console", "validation error")):
                    failed_tests.add(name)

        # A test that both passed and failed (retries) counts as failed.
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


# ---------------------------------------------------------------------------
# number_interval routing (dash-joined prs_in_bundle) -- registry-scoped logic.
#
# The delivered dataset carries `number_interval` as the dash-joined PR bundle
# (e.g. "1014-1032"), not a plain repo key. Instead of copying every bundle
# string into the registry, routing is expressed as LOGIC: firecrawl is a
# single-toolchain (node:20) repo, so any dash-joined firecrawl bundle simply
# dispatches to the one FirecrawlFirecrawlInstance. Should firecrawl ever split
# into Node eras, add ``Firecrawl_eras[upper_bound] = <Instance class>`` and the
# shared range-picker below routes each bundle by its anchor (lowest PR number)
# -- still no interval list ever lives in the registry.
#
# Two idempotent, firecrawl-scoped shims are installed at import time:
#   1. PullRequest.from_json -- fill an empty number_interval from prs_in_bundle
#      (dash-joined) so the value flows into the output dataset and drives routing.
#   2. Instance.create -- when number_interval is a dash-joined bundle (not a
#      registered key), dispatch firecrawl bundles to the covering era class.
# ---------------------------------------------------------------------------
import json as _fc_json
from multi_swe_bench.harness.pull_request import PullRequest as _FcPullRequest

# Single Node era: any anchor PR number routes here. The dict is keyed by the
# era's inclusive PR upper-bound; a very large sentinel means "covers all PRs".
Instance._firecrawl_eras = getattr(Instance, "_firecrawl_eras", {})
Instance._firecrawl_eras[10**12] = FirecrawlFirecrawlInstance


def _firecrawl_pick_era(pr):
    eras = getattr(Instance, "_firecrawl_eras", {})
    if not eras:
        return None
    ni = getattr(pr, "number_interval", "") or ""
    anchors = [int(tok) for tok in ni.split("-") if tok.isdigit()]
    if not anchors:
        return None
    anchor = min(anchors)
    for hi in sorted(eras):            # ascending PR upper-bound order
        if anchor <= hi:
            return eras[hi]
    return eras[max(eras)]             # newest era for anything past the last bound


if not getattr(_FcPullRequest, "_firecrawl_ni_shim", False):
    _fc_orig_from_json = _FcPullRequest.from_json.__func__

    def _fc_from_json(cls, json_str):
        pr = _fc_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "firecrawl"
                and getattr(pr, "repo", "") == "firecrawl"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_fc_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    _FcPullRequest.from_json = classmethod(_fc_from_json)
    _FcPullRequest._firecrawl_ni_shim = True


if not getattr(Instance, "_firecrawl_route_shim", False):
    _fc_orig_create = Instance.create.__func__

    def _fc_create(cls, pr, config, *args, **kwargs):
        try:
            return _fc_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == "firecrawl" and getattr(pr, "repo", "") == "firecrawl":
                era = _firecrawl_pick_era(pr)
                if era is not None:
                    return era(pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_fc_create)
    Instance._firecrawl_route_shim = True
