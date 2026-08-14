import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for
# toeverything/AFFiNE.
#
# Each record is a release-line BUNDLE. The raw JSONL carries `prs_in_bundle`
# (e.g. [10358, 10376, 10379, ...]) but an EMPTY/absent `number_interval`. The
# required output format is the dash-JOINED explicit bundle list
# ("10358-10376-10379-...") — NOT a "start-end" range, which would wrongly
# imply every PR in between is part of the bundle.
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "toeverything/10358-10376-..."), which is not
#     registered → every instance creation fails ("Instance ... not registered").
#
# So, following the aquasecurity/tfsec + grafana/mimir convention, we do two
# import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source,
# no impact on any other repo):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_affine_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_affine_number_interval_patched", False):
    _affine_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _affine_from_json(cls, json_str):
        pr = _affine_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "toeverything"
                and raw.get("repo") == "AFFiNE"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._affine_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_affine_from_json)
    _pull_request.PullRequest._affine_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_affine_build_patched", False):
        _affine_orig_build = _Dataset.build.__func__

        def _affine_build(cls, pr, report):
            ds = _affine_orig_build(cls, pr, report)
            ni = getattr(pr, "_affine_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_affine_build)
        _Dataset._affine_build_patched = True
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# toeverything/AFFiNE  (lht release-line bundle dataset)
#
# Toolchain is UNIFORM across the whole dataset (PRs #8964-#14518, releases
# v0.20 - v0.26): Node 22 + Yarn 4 (Berry, vendored under .yarn/releases) +
# vitest. `.nvmrc` 22.x, package.json `packageManager` yarn@4.6.0..4.12.0,
# root `scripts.test` == "vitest --run" at every base commit checked.
# => single registration `toeverything/AFFiNE`, no era split. This is also
# required: the JSONL has no `number_interval`/`tag`, so Instance.create()
# (instance.py:41-48) routes every record to "toeverything/AFFiNE"; a ranged
# multi-file split would make all records unroutable.
#
# Test scope: the tests are run scoped to the *.spec/*.test files that the
# PR's test.patch adds/modifies (top-level `tests/` Playwright e2e workspace
# excluded - those need a full running app + browser). The blocksuite vitest
# unit/integration specs are the runnable bulk. Specs needing `@affine/native`
# (electron) or a database (packages/backend/server) fail *consistently* in
# both base and fix runs in this minimal container -> they do not create a
# false fix-vs-base delta (same env-constraint reasoning as the camel/
# agent-browser lht configs).
#
# Path-casing: the JSONL `repo` is mixed-case "AFFiNE". DockerfileEnhancer.
# _standardize_repo_fetch() rewrites `RUN git clone <url> /home/AFFiNE` (single
# URL token, derived from pr.repo) into the parameterized ${REPO_URL}/
# ${BASE_COMMIT} form and a `/home/AFFiNE` WORKDIR. To stay consistent with
# that (and per the fetch-pr-dependencies guide's mixed-case rule), the local
# checkout path is the hardcoded constant REPO_DIR everywhere - never
# `{pr.repo}` - and the clone line carries NO extra flags (a `--filter=...`
# flag makes the enhancer regex skip the line, leaving the fetch
# non-standardized).
# ---------------------------------------------------------------------------

# Must match GitHub's exact casing == the path the (enhancer-rewritten)
# `git clone .../AFFiNE` + `WORKDIR /home/AFFiNE` create.
REPO_DIR = "AFFiNE"


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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return f"base-{self.pr.base.sha[:12]}"

    def workdir(self) -> str:
        return f"base-{self.pr.base.sha[:12]}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        # Resilient in-container git clone. The full AFFiNE clone repeatedly
        # died mid-transfer with `GnuTLS recv error (-9): Error decoding the
        # received TLS packet` inside BuildKit. Root causes for that on large
        # clones: HTTP/2 multiplexing bugs, a too-small http.postBuffer, and
        # libcurl's low-speed timeout aborting a briefly-stalled transfer.
        # Configure git GLOBALLY *before* the clone so the clone command itself
        # stays a single bare `git clone <url> /home/AFFiNE` (no extra flags) —
        # required so DockerfileEnhancer._standardize_repo_fetch() still rewrites
        # it into the ${REPO_URL}/${BASE_COMMIT} form (a flag on the clone line
        # makes the enhancer regex skip it, leaving the fetch non-standardized).
        git_resiliency = (
            "RUN git config --global http.version HTTP/1.1 \\\n"
            "    && git config --global http.postBuffer 1048576000 \\\n"
            "    && git config --global http.lowSpeedLimit 0 \\\n"
            "    && git config --global http.lowSpeedTime 999999 \\\n"
            "    && git config --global core.compression 0 \\\n"
            "    && git config --global fetch.retry true \\\n"
            "    && git config --global submodule.fetchJobs 1"
        )

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git jq python3 build-essential pkg-config libssl-dev curl ca-certificates
RUN corepack enable

{git_resiliency}

{code}

{self.clear_env}

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

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

export CI=true
export HUSKY=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export YARN_ENABLE_HARDENED_MODE=0
corepack enable >/dev/null 2>&1 || true

cd /home/AFFiNE
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
yarn install --immutable || yarn install || true
# Many blocksuite vitest projects (e.g. @blocksuite/integration-test,
# @blocksuite/std in earlier releases) run in vitest BROWSER mode; without a
# Playwright browser the WHOLE vitest invocation aborts (browserType.launch
# fails) -> 0/0/0 -> invalid report. Install the repo-pinned browsers so
# those projects run. Bundled builds work on amd64 AND arm64.
# ALL THREE browsers are required, not just Chromium. A single missing
# executable aborts the ENTIRE vitest run, not just the project that needs it,
# so a partial install is indistinguishable from a broken instance. pr-14329
# scored 127 passed at baseline (12 spec files) but 0/0/0 in the test and fix
# stages once the test patch widened the set to 37 files: first
# "firefox-1482/firefox/firefox" was missing, and after adding firefox the very
# next run failed on "webkit-2158/pw_run.sh". Playwright ships exactly
# chromium/firefox/webkit, so installing the lot ends the whack-a-mole instead
# of trading one missing binary for the next. Bare `install --with-deps` takes
# all three; ~350 MB per arch, which is cheap against a 60-min rebuild.
yarn playwright install --with-deps \\
  || npx --yes playwright install --with-deps \\
  || yarn playwright install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# baseline: scoped specs at the base commit (no patches applied). New spec
# files may not exist yet -> vitest exits non-zero; that is expected and
# harmless (docker_util.run captures the log regardless of exit code; the
# vitest command is last so set -e does not truncate its output).
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=8192"

cd /home/AFFiNE
SPECS=$(grep '^+++ b/' /home/test.patch | sed 's#^+++ b/##' \\
  | grep -E '\\.(spec|test)\\.[cm]?[jt]sx?$' \\
  | grep -vE '^tests/' | sort -u) || true
echo "SCOPED_SPECS<<EOF"
echo "$SPECS"
echo "EOF"
if [ -z "$SPECS" ]; then
  echo "No vitest-scoped spec files in test.patch; nothing to run."
  exit 0
fi
yarn vitest --run $SPECS --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=8192"

cd /home/AFFiNE
# The lht bundle stores binary files (*.snap/*.png/*.pdf/*.docx/*.zip/...)
# as GIT-binary patches WITHOUT a full index line -> `git apply` rejects the
# WHOLE patch atomically, which under `set -eo pipefail` would abort before
# vitest and yield (0,0,0). Exclude every binary type so the text hunks
# apply; --reject is a belt-and-suspenders fallback so an unforeseen binary
# can never abort the run. Patch-apply is setup (not the test command), so
# the `|| true` tail is allowed here (Check 3C only forbids it on tests).
git apply --whitespace=nowarn --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/test.patch \\
  || git apply --whitespace=nowarn --reject --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/test.patch \\
  || true
# Re-link workspaces after patching. The lht bundles rename whole package
# DIRECTORIES (blocksuite/affine/blocks/block-root -> .../root) while keeping
# the package NAME (@blocksuite/affine-block-root). node_modules/@blocksuite/*
# are symlinks to the old directories, so every rename leaves them dangling
# and vite reports the workspaces as "imported but could not be resolved".
# The spec file then fails to LOAD -- a whole-file failure, not an assertion --
# identically in the test and fix stages, so no test transitions f2p and the
# instance is dropped as unresolved. Re-linking costs ~3s: the resolution and
# fetch steps are already satisfied from the image's yarn cache.
# yarn.lock is deliberately excluded from the patch above, so it still points
# at the pre-rename workspace paths and this install MUST rewrite it. CI=true
# makes Yarn 4 enable immutable installs, which rejects that with
# "YN0028: The lockfile would have been modified by this install" and leaves
# every symlink dangling -- so the relink has to opt out explicitly.
export HUSKY=0 YARN_ENABLE_HARDENED_MODE=0 YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install >/dev/null 2>&1 || yarn install || true
SPECS=$(grep '^+++ b/' /home/test.patch | sed 's#^+++ b/##' \\
  | grep -E '\\.(spec|test)\\.[cm]?[jt]sx?$' \\
  | grep -vE '^tests/' | sort -u) || true
echo "SCOPED_SPECS<<EOF"
echo "$SPECS"
echo "EOF"
if [ -z "$SPECS" ]; then
  echo "No vitest-scoped spec files in test.patch; nothing to run."
  exit 0
fi
yarn vitest --run $SPECS --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=8192"

cd /home/AFFiNE
# Apply test.patch then fix.patch SEQUENTIALLY, each binary-excluded with a
# --reject fallback and non-fatal tail (see test-run.sh rationale). Binary
# *.snap/*.png/*.pdf/... patches in the lht bundle lack a full index line and
# would otherwise abort the whole `git apply` -> fix=(0,0,0) -> invalid.
git apply --whitespace=nowarn --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/test.patch \\
  || git apply --whitespace=nowarn --reject --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/test.patch \\
  || true
git apply --whitespace=nowarn --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/fix.patch \\
  || git apply --whitespace=nowarn --reject --exclude='*.lock' --exclude='yarn.lock' --exclude='pnpm-lock.yaml' --exclude='package-lock.json' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.webp' --exclude='*.svg' --exclude='*.pdf' --exclude='*.docx' --exclude='*.xlsx' --exclude='*.snap' --exclude='*.bin' --exclude='*.icns' --exclude='*.ico' --exclude='*.zip' --exclude='*.ydoc' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' --exclude='*.mp4' --exclude='*.webm' --exclude='*.mp3' --exclude='*.node' --exclude='*.wasm' --exclude='apollo-ios-cli' /home/fix.patch \\
  || true
# Re-link workspaces after patching -- see test-run.sh for the full rationale.
# Needed in BOTH stages: the fix patch renames the remaining package
# directories, so the node_modules symlinks dangle here too.
# yarn.lock is deliberately excluded from the patch above, so it still points
# at the pre-rename workspace paths and this install MUST rewrite it. CI=true
# makes Yarn 4 enable immutable installs, which rejects that with
# "YN0028: The lockfile would have been modified by this install" and leaves
# every symlink dangling -- so the relink has to opt out explicitly.
export HUSKY=0 YARN_ENABLE_HARDENED_MODE=0 YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install >/dev/null 2>&1 || yarn install || true
SPECS=$(grep '^+++ b/' /home/test.patch | sed 's#^+++ b/##' \\
  | grep -E '\\.(spec|test)\\.[cm]?[jt]sx?$' \\
  | grep -vE '^tests/' | sort -u) || true
echo "SCOPED_SPECS<<EOF"
echo "$SPECS"
echo "EOF"
if [ -z "$SPECS" ]; then
  echo "No vitest-scoped spec files in test.patch; nothing to run."
  exit 0
fi
yarn vitest --run $SPECS --reporter=verbose

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("toeverything", "AFFiNE")
class AFFiNE(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        dur_re = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s\s*$")

        # vitest 3 verbose, ANSI-stripped, real captured formats. Node-env
        # projects: "<mark>  <@proj>  <relpath>.spec.ts > suite > name 2ms".
        # vitest BROWSER-mode projects add a browser badge after the project:
        # "<mark>  <@proj> (chromium)  <relpath>.spec.ts > suite > name 31ms".
        # The "(<browser>)" is non-capturing so the test KEY stays identical
        # across stages (the browser is stable per project) and uniform with
        # node-env keys.
        #
        # The "<@proj>" badge is OPTIONAL: vitest only prints it for NAMED
        # projects. Specs under packages/frontend/** belong to an unnamed
        # project, so their lines start straight at the path --
        # "✓ packages/frontend/core/.../pan-tool.spec.ts > suite > name 1ms".
        # Requiring the badge scored every such run (0,0,0), which is
        # indistinguishable from "this instance has no runnable test" and got
        # the instance dropped as unresolved even with a clean transition:
        # pr-13725 had 3 tests failing in the test stage and the same 3 passing
        # in the fix stage, and was thrown away.
        #   per-case:   "✓ / × / ↓  [<@proj>] [(br)]  <relpath> > suite > name"
        #   case fail:  "FAIL  [<@proj>] [(br)]  <relpath> > suite > name"
        #   file fail:  "FAIL  [<@proj>] [(br)]  <relpath> [ full/repo/path ]"
        _br = r"(?:\([a-z]+\)[ \t]+)?"
        _proj = r"(?:(@\S+)[ \t]+)?"
        re_case = re.compile(
            r"^[ \t>]*([✓×✗↓])[ \t]+" + _proj + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*>[ \t]*(.+)$"
        )
        re_test_fail = re.compile(
            r"^[ \t]*FAIL[ \t]+" + _proj + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*>[ \t]*(.+)$"
        )
        re_file_fail = re.compile(
            r"^[ \t]*FAIL[ \t]+" + _proj + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*\[[ \t]*\S+[ \t]*\]\s*$"
        )

        def _key(proj, path, name=None):
            # An unnamed project contributes no badge. The path is stable
            # across the run/test/fix stages, which is all a key has to
            # guarantee, so omit the badge rather than emitting a literal
            # "None" that would differ from nothing at all.
            head = f"{proj} {path}" if proj else path
            return f"{head} > {name}" if name else head

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).rstrip()
            if not line:
                continue
            stripped = line.strip()

            m = re_case.match(line)
            if m:
                mark, proj, path, name = m.groups()
                name = dur_re.sub("", name).strip()
                key = _key(proj, path, name)
                if mark == "✓":            # ✓ pass
                    passed_tests.add(key)
                elif mark == "↓":          # ↓ skipped
                    skipped_tests.add(key)
                else:                           # × / ✗ fail
                    failed_tests.add(key)
                continue

            if stripped.startswith("FAIL"):
                mf = re_file_fail.match(line)
                if mf:
                    proj, path = mf.groups()
                    failed_tests.add(_key(proj, path))
                    continue
                mt = re_test_fail.match(line)
                if mt:
                    proj, path, name = mt.groups()
                    name = dur_re.sub("", name).strip()
                    failed_tests.add(_key(proj, path, name))
                    continue

        # a case can appear both inline (×) and in the "Failed Tests" detail
        # block (FAIL) - the set dedupes; keep failures out of passed.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Defensive bundle-interval routing keys. The backfilled input dataset carries
# `number_interval` = dash-joined `prs_in_bundle`, so Instance.create routes on
# f"toeverything/{number_interval}" (instance.py) with NO fallback to the repo
# key. Register the AFFiNE class under every bundle interval derived from the
# dataset's prs_in_bundle so those records resolve; the plain
# @Instance.register("toeverything","AFFiNE") above still covers any row that
# omits number_interval.
def _register_affine_bundle_intervals() -> None:
    import os

    dataset_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "..",
        "dataset",
        "toeverything__AFFiNE_lht_final.jsonl",
    )
    dataset_path = os.path.normpath(dataset_path)
    if not os.path.isfile(dataset_path):
        return
    seen: set[str] = set()
    with open(dataset_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = _json.loads(line)
            except ValueError:
                continue
            if raw.get("org") != "toeverything" or raw.get("repo") != "AFFiNE":
                continue
            bundle = raw.get("prs_in_bundle") or []
            interval = "-".join(str(p) for p in bundle)
            if interval and interval not in seen:
                seen.add(interval)
                Instance.register("toeverything", interval)(AFFiNE)


_register_affine_bundle_intervals()
