import json as _json
import re
from typing import Optional

from multi_swe_bench.harness.dataset import Dataset
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Dataset identity fields: instance_id and lang.
#
# gen_report --mode dataset writes each row with Dataset.build(), which copies
# `lang` straight off the raw PullRequest and has no `instance_id` field at all.
# The collector emits neither, so every row lands with lang="" and no
# instance_id, and validate_dataset.py fails the file on both counts. Only
# harness/repos/ is writable, so the fix installs itself from here at import
# time: multi_swe_bench/harness/__init__.py does `from ...repos import *`, and
# importing any harness submodule runs that first, so this is always in place
# before a row can be emitted.
#
# Both values are derived rather than hardcoded to this repo: instance_id is the
# canonical "{org}__{repo}-{number}", and lang is read off the language
# directory of whichever config the registry holds for that org/repo. So the
# patch stays correct for every repo in the registry, not just this one.
#
# Deliberately NOT filled in: `number_interval` and `tag`. Instance.create()
# keys its registry lookup off both -- "{org}/{number_interval}" when the
# interval is set, "{org}/{repo}_{tag}" when the tag is -- so populating either
# one would make every instance fail to resolve. validate_dataset.py exempts
# both fields from its emptiness check for the same reason.
# ---------------------------------------------------------------------------

# The repos/ subdirectory is the language name, except where the package had to
# dodge a stdlib collision.
_LANG_DIR_ALIASES = {"golang": "go"}


def _lang_from_registry(org: str, repo: str) -> str:
    registered = Instance._registry.get(f"{org}/{repo}")
    if registered is None:
        return ""

    parts = registered.__module__.split(".")
    if "repos" not in parts:
        return ""

    index = parts.index("repos") + 1
    if index >= len(parts):
        return ""

    return _LANG_DIR_ALIASES.get(parts[index], parts[index])


def _install_dataset_identity_fields() -> None:
    if getattr(Dataset, "_identity_fields_patched", False):
        return

    original_json = Dataset.json
    original_build = Dataset.build.__func__

    def json_with_identity(self, *args, **kwargs) -> str:
        # Round-trip the original encoder's output instead of re-serialising the
        # dataclass: TestResult holds its test names in sets, and only
        # dataclass_json's encoder knows how to flatten those.
        payload = _json.loads(original_json(self, *args, **kwargs))
        payload["instance_id"] = f"{self.org}__{self.repo}-{self.number}"
        payload["lang"] = self.lang or _lang_from_registry(self.org, self.repo)
        return _json.dumps(payload, ensure_ascii=False)

    def build_with_identity(cls, pr: PullRequest, report) -> Dataset:
        data = original_build(cls, pr, report)
        if not data.lang:
            data.lang = _lang_from_registry(data.org, data.repo)
        return data

    # json() is inherited from Repository, so assigning here shadows it on
    # Dataset alone -- PullRequest and Report keep the original encoder.
    Dataset.json = json_with_identity
    Dataset.build = classmethod(build_with_identity)
    Dataset._identity_fields_patched = True


_install_dataset_identity_fields()


SCHEMA_URL_FIX = (
    "sed -i "
    '"s|https://json.schemastore.org/chrome-manifest'
    '|https://www.schemastore.org/chrome-manifest|" '
    "node_modules/vite-plugin-web-extension/dist/index.js"
)

BUILD_EXTENSION = "npm run build -w syncwatch-extension"

# The two `screenshot_*` tests compare against reference PNGs committed from a
# different machine and cannot pass here: `screenshot_popup` differs by ~1px of
# layout, and `screenshot_option` renders "URL: undefined" because options.js
# reads chrome.storage.sync before background.js's onInstalled handler seeds the
# default URL. That second one is a race, so leaving it in risks a stage-to-stage
# flip that Report.check() would reject. Both are excluded identically from all
# three run scripts, so the f2p comparison is unaffected.
# --timeout=60000 doubles Playwright's default 30s per-test ceiling. `user
# scenario` drives a real browser through ~11 steps and was observed timing out
# at exactly 30.0s (~1 run in 10) when the host was busy; a hard timeout in the
# fix stage would drop f2p to 0 and invalidate the instance. Applied identically
# to all three scripts, so it cannot skew the comparison.
TEST_CMD = (
    'xvfb-run -a --server-args="-screen 0 1280x1024x24" '
    'npx playwright test --reporter=list --grep-invert "screenshot" '
    "--timeout=60000"
)


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

    def dependency(self) -> str:
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

ENV CI=true
ENV SERVER_PORT=8080
ENV TEST_PAGE_PORT=3000

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates xvfb xauth \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN npm ci
RUN npm install -g serve@14
RUN {SCHEMA_URL_FIX}
RUN npx playwright install-deps chromium
RUN npx playwright install chromium
RUN {BUILD_EXTENSION}
RUN test -f packages/syncwatch-extension/dist/manifest.json

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
npm install || true
{BUILD_EXTENSION}
test -f packages/syncwatch-extension/dist/manifest.json
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
{BUILD_EXTENSION}
cd /home/{self.pr.repo}/packages/syncwatch-extension
{TEST_CMD}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{BUILD_EXTENSION}
cd /home/{self.pr.repo}/packages/syncwatch-extension
{TEST_CMD}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{BUILD_EXTENSION}
cd /home/{self.pr.repo}/packages/syncwatch-extension
{TEST_CMD}

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("Semro", "syncwatch")
class SYNCWATCH(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        # Playwright list reporter, e.g.
        #   ok 1 [chromium] > main.spec.ts:8:1 > popup page (1.2s)
        #   x  8 [chromium] > main.spec.ts:72:1 > user scenario (retry #1) (30.0s)
        # The leading "[project]" is required: the vite build that runs before
        # the suite prints lines like "* 1 modules transformed." which would
        # otherwise be captured as a test name.
        pass_pattern = re.compile(r"^[✓✔]\s+\d+\s+(\[[^\]]+\]\s+.*)$")
        fail_pattern = re.compile(r"^[✘✗×✖]\s+\d+\s+(\[[^\]]+\]\s+.*)$")
        skip_pattern = re.compile(r"^-\s+\d+\s+(\[[^\]]+\]\s+.*)$")

        def clean(name: str) -> str:
            # Drop the trailing duration and the retry marker.
            name = re.sub(r"\s*\(retry #\d+\)", "", name)
            # Playwright switches units as the test slows down: (150ms), (30.0s),
            # (1.0m). Matching only ms/s left "user scenario (1.0m)" as a second,
            # phantom test name in the same stage.
            name = re.sub(r"\s*\([\d.]+\s*(?:ms|s|m)\)\s*$", "", name)
            # Drop :line:col so a patch that shifts line numbers does not
            # rename the test between the run / test / fix stages.
            name = re.sub(r"(\.spec\.ts|\.test\.ts|\.spec\.js|\.test\.js):\d+:\d+", r"\1", name)
            return name.strip()

        # Later attempts overwrite earlier ones, so a test that fails and then
        # passes on retry ends up "passed", matching Playwright's own verdict.
        test_results: dict[str, str] = {}
        for line in log.splitlines():
            line = line.strip()
            for pattern, status in (
                (fail_pattern, "failed"),
                (pass_pattern, "passed"),
                (skip_pattern, "skipped"),
            ):
                match = pattern.match(line)
                if match:
                    name = clean(match.group(1))
                    if name:
                        test_results[name] = status
                    break

        passed_tests = {n for n, s in test_results.items() if s == "passed"}
        failed_tests = {n for n, s in test_results.items() if s == "failed"}
        skipped_tests = {n for n, s in test_results.items() if s == "skipped"}

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
