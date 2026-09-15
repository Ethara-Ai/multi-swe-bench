import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

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


class OpenclawImageBase(Image):
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

        return f"""FROM {image_name}

{self.global_env}

ENV DO_NOT_TRACK=1
ENV OPENCLAW_TELEMETRY_DISABLED=1
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git python3 make g++ ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable

RUN git config --global http.version HTTP/1.1 \\
    && git config --global http.postBuffer 524288000 \\
    && for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{self.pr.repo}; \\
        if git clone --shallow-since=2026-01-01 "${{REPO_URL}}" /home/{self.pr.repo}; then break; fi; \\
        echo "clone attempt ${{attempt}} failed; retrying in 15s" >&2; \\
        sleep 15; \\
    done \\
    && test -d /home/{self.pr.repo}/.git

# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenclawImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return OpenclawImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if _touches_ui(self.pr):
            ui_setup = """
if corepack pnpm --dir ui exec playwright install --with-deps chromium; then
    touch /home/.openclaw-ui-lane
else
    echo "prepare: chromium unavailable; the ui lane will be skipped" >&2
fi
"""
        else:
            ui_setup = ""

        e2e_targets = _e2e_targets(self.pr)
        if e2e_targets:
            e2e_lane = """
if [ -f vitest.e2e.config.ts ] && grep -q 'e2e\\.test\\.ts' vitest.config.ts; then
    run_lane e2e vitest_run --config vitest.e2e.config.ts {targets}
fi
""".format(targets=" ".join(f"'{path}'" for path in e2e_targets))
        else:
            e2e_lane = ""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{base_sha}"
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true || \\
    corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true

corepack pnpm canvas:a2ui:bundle || \\
    echo "prepare: a2ui bundle failed; the suites stub it themselves" >&2
{ui_setup}""".format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha, ui_setup=ui_setup
                ),
            ),
            File(
                ".",
                "run-suites.sh",
                """#!/bin/bash
set -uo pipefail

export CI=true
cd /home/{repo}

vitest_run() {{
    corepack pnpm exec vitest run --reporter=verbose --silent=passed-only "$@"
}}

run_lane() {{
    label="$1"
    shift
    echo "===== openclaw-lane: ${{label}} ====="
    "$@" || echo "===== openclaw-lane-failed: ${{label}} (exit $?) ====="
}}

if [ -f vitest.unit.config.ts ] && [ -f vitest.extensions.config.ts ] \\
        && [ -f vitest.gateway.config.ts ]; then
    run_lane unit vitest_run --config vitest.unit.config.ts
    run_lane extensions vitest_run --config vitest.extensions.config.ts
    run_lane gateway vitest_run --config vitest.gateway.config.ts
else
    run_lane unit vitest_run --config vitest.config.ts
fi
{e2e_lane}
if [ -f /home/.openclaw-ui-lane ]; then
    run_lane ui corepack pnpm --dir ui exec vitest run \\
        --config vitest.config.ts --reporter=verbose --silent=passed-only
fi

exit 0
""".format(repo=self.pr.repo, e2e_lane=e2e_lane),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "fix-run: fix.patch conflicts; retrying without CHANGELOG.md" >&2
    if ! git apply --whitespace=nowarn --exclude=CHANGELOG.md /home/fix.patch; then
        echo "Error: git apply fix.patch failed" >&2
        exit 1
    fi
fi

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("OpenclawImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("openclaw", "openclaw")
class Openclaw(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_lane = re.compile(r"^=+\s*openclaw-lane:\s*(\S+)\s*=+$")

        timing = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s|m))?"
        re_pass = re.compile(rf"^\s*[✓✔]\s+(.+?){timing}\s*$")
        re_fail = re.compile(rf"^\s*[×✕✗]\s+(.+?){timing}\s*$")
        re_skip = re.compile(rf"^\s*[↓○◌]\s+(.+?){timing}\s*$")

        re_test_name = re.compile(r"\.test\.[cm]?[jt]sx?\s+>\s+\S")

        lane = ""

        def qualify(name: str) -> str:
            name = re.sub(r"\s+", " ", name).strip()
            return f"ui > {name}" if lane == "ui" else name

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()

            banner = re_lane.match(line.strip())
            if banner:
                lane = banner.group(1)
                continue

            m = re_fail.match(line)
            if m and re_test_name.search(m.group(1)):
                failed_tests.add(qualify(m.group(1)))
                continue

            m = re_pass.match(line)
            if m and re_test_name.search(m.group(1)):
                passed_tests.add(qualify(m.group(1)))
                continue

            m = re_skip.match(line)
            if m and re_test_name.search(m.group(1)):
                skipped_tests.add(qualify(m.group(1)))

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
