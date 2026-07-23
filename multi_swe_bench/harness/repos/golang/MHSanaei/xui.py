import json as _json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# === number_interval: dash-joined prs_in_bundle on the resolved jsonl ===
#
# FORMAT: the explicit dash-joined member list, NEVER a range. Bundles here are
# sparse -- pr-4064's bundle is [4064, 4065, 4069, 4073, 4076], so it must be
# "4064-4065-4069-4073-4076". A range like "4064-4076" would claim 4066/4067/
# 4068/4070..4072/4074/4075, which are not in the bundle.
#
# The raw jsonl carries `prs_in_bundle` but NO `number_interval`, and the
# dataclass_json loader drops unknown keys, so the registry classes never see the
# bundle and Dataset.build (which copies `number_interval=pr.number_interval`)
# would write "" into every resolved row.
#
# Setting pr.number_interval at load time would ALSO change the routing key
# (instance.py: name becomes "MHSanaei/4064-4065-4069-4073-4076"), so instead the
# value is stashed in a NON-field attr and stamped onto the OUTPUT row only,
# leaving routing on "MHSanaei/3x-ui". Every bundle value is additionally
# registered at the bottom of this file, so a future jsonl that DOES carry
# number_interval still resolves instead of raising "not registered".
#
# Two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness
# source), following the aquasecurity/tfsec and elastic/beats convention.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_xui_number_interval_patched", False):
    _xui_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _xui_from_json(cls, json_str):
        pr = _xui_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "MHSanaei"
                and raw.get("repo") == "3x-ui"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._xui_number_interval = "-".join(str(p) for p in raw["prs_in_bundle"])
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_xui_from_json)
    _pull_request.PullRequest._xui_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_xui_build_patched", False):
        _xui_orig_build = _Dataset.build.__func__

        def _xui_build(cls, pr, report):
            ds = _xui_orig_build(cls, pr, report)
            ni = getattr(pr, "_xui_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_xui_build)
        _Dataset._xui_build_patched = True
# ---------------------------------------------------------------------------

# Shared build/test prelude for every run script.
#
# The frontend build is CONDITIONAL on `frontend/` existing, because this repo
# changes its asset strategy across the release lines this dataset spans:
#
#   * <= v2.9.4 (PRs 3827/4064/4083/4117/4169): web/web.go embeds `assets`,
#     `html/*`, `translation/*`. Those are committed, so there is NO frontend/
#     directory and nothing to build. An unconditional `cd frontend` here aborts
#     the script under `set -e` before go test ever runs.
#   * >= v3.0.0 (PR 4223): web/web.go embeds `all:dist`, and frontend/ (Vite)
#     emits into web/dist, which is NOT committed. The build is mandatory there
#     or the go:embed fails to compile.
#
# --prefer-offline lets the warm npm cache primed in prepare.sh serve the
# install, so evaluation does not hard-depend on the registry being reachable.
#
# CGO_ENABLED=1 is REQUIRED, not a preference. The DB layer is
# gorm.io/driver/sqlite -> github.com/mattn/go-sqlite3, which is a cgo binding.
# Built with CGO_ENABLED=0 it still links, but every driver call returns
# "Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.
# This is a stub", so every DB-backed test (port-conflict, inbound-tag,
# client-ip) fails at InitDB in ALL of run/test/fix. Those permanently-failing
# tests can never be credited as F2P, which silently drops the very tests some
# PRs in this dataset are about. gcc comes from build-essential in the base.
ENV_SETUP = """export CI=true
export CGO_ENABLED=1
"""

BUILD_AND_TEST = """
cd /home/{repo}

if [ -d frontend ]; then
  (cd frontend && npm ci --prefer-offline --no-audit --no-fund && npm run build)
fi

go test -v -count=1 ./...
"""


class XuiImageBase(Image):
    """Level 1: toolchain-only base image (SINGLE, shared by every PR).

    This Dockerfile declares its own `# syntax` directive, which makes
    DockerfileEnhancer.enhance() return it VERBATIM. That is deliberate: the
    enhancer's auto-injected infra block carries proxy ARGs, MITM CA-secret
    handling and CA-certificate symlink rewrites, none of which this repo wants.
    Opting out means the build talks to the network directly and trusts the base
    image's own CA bundle. The cost of opting out is that the pieces of that
    block we DO want are no longer free, so they are spelled out below:
    ARG TARGETARCH, the REPO_URL/BASE_COMMIT build args the pipeline passes, the
    ENV defaults, and the OCI labels. (Skipping the enhancer also skips its
    clone-rewrite and hardening injection -- irrelevant here, since this image
    has no clone to rewrite. Per-PR clone + hardening live in XuiImageSolve.)

    IMPORTANT: this image must NOT clone the repository. image_tag() is the
    constant "base", so it is built once and reused by all PRs (build_dataset
    skips an image whose full name already exists) -- but the records carry
    different base.sha values, so a single shared checkout cannot serve them.
    Keeping the clone per-PR in XuiImageSolve lets each PR pin and harden its
    own base commit independently. This image only provides the Go + Node
    toolchain.

    Note this is also why the `# syntax` opt-out above must not be dropped
    casually while a clone is present: without it the enhancer rewrites any
    `RUN git clone ... /home/<repo>` in a string-dependency Dockerfile into a
    clone pinned to `${BASE_COMMIT}` plus Image._HARDENING_BLOCK, which would
    force-pin this shared image to whichever PR built first and strip history to
    that one commit, leaving the other 5 unable to check out their own base.sha.

    golang:1.26-bookworm: go.mod declares go 1.26.0 (v2.9.0) through 1.26.3
    (v3.0.1) across this dataset, so the 1.26 line covers every base commit.
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
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        # Validate before interpolating into the label / clone URL.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        default_packages = [
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
        packages_str = " \\\n    ".join(default_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # The `# syntax` directive opts this Dockerfile out of the enhancer's
        # proxy/MITM/CA-cert injection (see class docstring); everything the
        # enhancer would otherwise have contributed and we still want is declared
        # inline below. REPO_URL/BASE_COMMIT are declared but unused here: this
        # image does not clone, and build_dataset passes both as build args to
        # every string-dependency image, so declaring them keeps the build free
        # of "unused build argument" warnings.
        #
        # Node 22 is needed only by the v3.0.0+ Vite frontend, but it lives in the
        # shared base so the per-PR images stay cheap. No `git clone` here on
        # purpose (see docstring).
        return f"""# syntax=docker/dockerfile:1.6
FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

{apt_command}

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \\
    apt-get install -y --no-install-recommends nodejs && \\
    rm -rf /var/lib/apt/lists/*

{self.clear_env}

CMD ["/bin/bash"]
"""


class XuiImageSolve(Image):
    """Level 2: the AGENT-FACING image (tag ``pr-<n>-solve``). SHIPPABLE.

    This is the only tier that may be handed to an agent/trajectory team. It
    holds the repository checked out at ``${BASE_COMMIT}``, history-hardened,
    with the base-state Go/npm caches warmed -- and NOTHING else. Critically it
    contains NO ``fix.patch``, NO ``test.patch`` and none of the run scripts
    that reference them.

    WHY A SEPARATE TIER: Docker layers are additive, so a `COPY` of the gold
    patches is permanent -- deleting them in a later layer leaves the bytes in
    the earlier layer, still extractable from the `docker save`/OCI tar that
    actually ships. The only way the patches are absent from an artifact is for
    that artifact's layers to never have contained them. Because this image sits
    BELOW XuiImageDefault in the dependency chain, the ``pr-<n>-solve`` tar
    contains solve's layers alone, while the patches live exclusively in the
    layers XuiImageDefault adds on top.

    That matters because the entire git-history hardening below (which removes
    the PR's own fix commit and all future history so the solution cannot be
    read back out of git) is defeated by a single ``cat /home/fix.patch``. An RL
    agent reliably finds the highest-reward action available, so a readable gold
    patch does not merely leak -- it poisons the training signal.

    build_dataset walks ``instance.dependency()`` -> ``.dependency()`` until it
    reaches a string, building and exporting EVERY Image tier it passes, so this
    image is built and gets its own tar with no harness change.
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

    def dependency(self) -> Image:
        return XuiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-solve"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}-solve"

    def files(self) -> list[File]:
        # ONLY the base-state cache warmer. Adding any patch or any script that
        # reads one would defeat this tier's entire purpose (see class docstring).
        return [
            File(
                ".",
                "warm-base.sh",
                """#!/bin/bash
set -e
{env_setup}
# Warm the BASE-state Go build/module cache and the npm cache so the agent gets
# a fast, offline-capable `go test` at the base commit. Runs BEFORE the
# hardening block strips git history. Failures are tolerated: a cold cache is a
# performance problem, not a correctness one.
cd /home/{repo}

if [ -d frontend ]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build) || true
fi

go mod download all || true
go test -v -count=1 ./... || true

""".format(repo=self.pr.repo, env_setup=ENV_SETUP),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history and checks out ${BASE_COMMIT} itself. Pinning is correct
        # here precisely because it is per-PR rather than on the shared base.
        # ARG default carries the sha because build_dataset/run_evaluation only
        # pass REPO_URL/BASE_COMMIT build args for string-dependency images.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/warm-base.sh

"""

        # Anti-reward-hacking hardening: detach at ${BASE_COMMIT}, drop origin,
        # delete every ref, expire reflogs, gc/repack, drop alternates, assert.
        # Removes the PR's own fix commit (and all future history) from the
        # image, so the solution cannot be read back out of git. Concatenated
        # raw rather than through an f-string so its ${BASE_COMMIT}/%(refname)
        # tokens stay literal. Runs last, after the cache warm.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


class XuiImageDefault(Image):
    """Level 3: the GRADING image (tag ``pr-<n>``). NEVER SHIP THIS.

    Layers the gold ``fix.patch``/``test.patch`` and the run scripts on top of
    the solve image, plus a fix-state dependency pre-warm. Used by
    build_dataset (dataset generation) and run_evaluation (grading, which
    bind-mounts the agent's patch over ``/home/fix.patch``).

    Because this tier never reaches an agent, the fix-state pre-warm here is
    safe: it makes grading hermetic (several fix patches edit go.mod/go.sum and
    would otherwise download modules mid-grade, so a network blip could flip a
    genuine pass into a scored failure) without leaking the fix's dependency
    set to anyone being trained.
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

    def dependency(self) -> Image:
        return XuiImageSolve(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
                "check_git_changes.sh",
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
{env_setup}
# The repo is already cloned, checked out at ${{BASE_COMMIT}} and HARDENED in
# the solve image; base-state caches are warm there too. prepare.sh adds only
# the FIX-STATE dependency pre-warm: several fix patches edit go.mod/go.sum (and
# frontend/package.json), so modules the fix introduces are absent from the
# base-state cache and would be downloaded mid-grade -- making a graded run
# network-dependent, where a transient proxy.golang.org failure turns a genuine
# pass into a scored failure. Warming them here makes grading hermetic.
#
# This tier never ships to an agent, so applying the gold patches to populate
# the cache leaks nothing. The tree is reverted afterwards so the graded run
# still starts from a clean base checkout: `git checkout -- .` undoes tracked
# edits and `git clean -fd` removes added files, while ignored build outputs
# (node_modules, web/dist) are deliberately KEPT (-x omitted) so the base-state
# warm survives. The env must match the run scripts' exactly or the cgo-enabled
# objects the evaluation needs are not the ones cached here.
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

if git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
  go mod download all || true
  if [ -d frontend ]; then
    (cd frontend && npm ci --prefer-offline --no-audit --no-fund) || true
  fi
  git checkout -- . || true
  git clean -fd || true
else
  echo "prepare: gold patches do not apply cleanly; skipping fix-state warm"
fi

bash /home/check_git_changes.sh
go test -v -count=1 ./... || true

""".format(repo=self.pr.repo, env_setup=ENV_SETUP),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
{env_setup}{build_and_test}""".format(
                    env_setup=ENV_SETUP,
                    build_and_test=BUILD_AND_TEST.format(repo=self.pr.repo),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
{env_setup}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{build_and_test}""".format(
                    repo=self.pr.repo,
                    env_setup=ENV_SETUP,
                    build_and_test=BUILD_AND_TEST.format(repo=self.pr.repo),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
{env_setup}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_and_test}""".format(
                    repo=self.pr.repo,
                    env_setup=ENV_SETUP,
                    build_and_test=BUILD_AND_TEST.format(repo=self.pr.repo),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # NO clone and NO hardening here: the solve image below already cloned,
        # checked out ${BASE_COMMIT} and ran Image._HARDENING_BLOCK, and this
        # image inherits that hardened /home/<repo> through its layers. Re-running
        # the hardening would be a no-op at best; re-cloning would restore the
        # full history this pipeline exists to strip. BASE_COMMIT is re-declared
        # only so the scripts can reference it (ENV does not cross the FROM).
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("MHSanaei", "3x-ui")
class Xui(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return XuiImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # Dedup: tests reported as both passed and failed end up as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a PR can carry must resolve to Xui. The
# 6 Go records of MHSanaei/3x-ui route via the "MHSanaei/3x-ui" key registered
# above (the raw jsonl carries no number_interval field); these keys cover a
# regenerated jsonl that does carry it. Explicit member lists, never ranges --
# the bundles are sparse.
_BUNDLE_NIS_XUI = [
    "3827-3884-3887-3888-3889-3916-3919-3931-3936-3937-3938-3939-3940-3941-3942-3947-3948-3966-3971-3980-3990-3997-4004-4045",  # pr-3827 (24 PRs)
    "4064-4065-4069-4073-4076",  # pr-4064 (5 PRs)
    "4083-4085-4086-4087-4092-4094",  # pr-4083 (6 PRs)
    "4117-4123-4124",  # pr-4117 (3 PRs)
    "4169-4179-4183-4202-4205-4206-4207-4209-4213",  # pr-4169 (9 PRs)
    "4223-4244-4247-4259",  # pr-4223 (4 PRs)
]

for _ni in _BUNDLE_NIS_XUI:
    Instance.register("MHSanaei", _ni)(Xui)
