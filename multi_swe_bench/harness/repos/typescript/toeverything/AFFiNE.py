import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

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
        return "base"

    def workdir(self) -> str:
        return "base"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git jq python3 build-essential pkg-config libssl-dev curl ca-certificates
RUN corepack enable

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
# Playwright Chromium the WHOLE vitest invocation aborts (browserType.launch
# fails) -> 0/0/0 -> invalid report. Install the repo-pinned Chromium so
# those projects run. Bundled Chromium works on amd64 AND arm64.
yarn playwright install --with-deps chromium \\
  || npx --yes playwright install --with-deps chromium \\
  || yarn playwright install chromium || true

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
        #   per-case:   "✓ / × / ↓  <@proj> [(br)]  <relpath> > suite > name"
        #   case fail:  "FAIL  <@proj> [(br)]  <relpath> > suite > name"
        #   file fail:  "FAIL  <@proj> [(br)]  <relpath> [ full/repo/path ]"
        _br = r"(?:\([a-z]+\)[ \t]+)?"
        re_case = re.compile(
            r"^[ \t>]*([✓×✗↓])[ \t]+(@\S+)[ \t]+" + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*>[ \t]*(.+)$"
        )
        re_test_fail = re.compile(
            r"^[ \t]*FAIL[ \t]+(@\S+)[ \t]+" + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*>[ \t]*(.+)$"
        )
        re_file_fail = re.compile(
            r"^[ \t]*FAIL[ \t]+(@\S+)[ \t]+" + _br
            + r"(\S+\.(?:spec|test)\.[cm]?[jt]sx?)[ \t]*\[[ \t]*\S+[ \t]*\]\s*$"
        )

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).rstrip()
            if not line:
                continue
            stripped = line.strip()

            m = re_case.match(line)
            if m:
                mark, proj, path, name = m.groups()
                name = dur_re.sub("", name).strip()
                key = f"{proj} {path} > {name}"
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
                    failed_tests.add(f"{proj} {path}")
                    continue
                mt = re_test_fail.match(line)
                if mt:
                    proj, path, name = mt.groups()
                    name = dur_re.sub("", name).strip()
                    failed_tests.add(f"{proj} {path} > {name}")
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
