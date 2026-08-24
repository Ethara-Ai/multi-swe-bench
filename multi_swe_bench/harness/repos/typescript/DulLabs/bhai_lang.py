import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# DulLabs/bhai-lang PR #226 ("feat: Add support for if-else-if ladders").
# npm-workspaces + Turborepo monorepo; the two testable packages are
# packages/parser and packages/interpreter, each running Jest via ts-jest
# (jest.config.js -> preset: ts-jest). Both use jest ^27 / ts-jest ^27.
#
# Cross-package build dependency (the load-bearing detail):
#   packages/parser  -> "main": "dist/index.js"
#   packages/interpreter depends on "bhai-lang-parser" and imports the BUILT module.
# So the parser must be BUILT (tsup) before the interpreter tests can resolve it,
# and because fix.patch edits parser SOURCE, the parser must be REBUILT after each
# patch is applied (mirrors openmc rebuilding its native core after the fix).
# The parser's OWN tests use ts-jest on source, so they need no build.
#
# Turbo (root devDep "turbo": "latest") is intentionally NOT used to drive the
# build/test — we call the npm workspace scripts directly for reproducibility.


class BhaiLangImageBase(Image):
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
        # engines: node>=14, npm>=7 (packageManager npm@8.1.4). node:18 is LTS,
        # non-EOL, satisfies npm>=7, and runs the 2021-era jest 27 stack fine.
        return "node:18"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class BhaiLangImageDefault(Image):
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
        return BhaiLangImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# npm-workspaces monorepo: install all workspaces (symlinks bhai-lang-parser into
# node_modules). `|| true` keeps a transient/arch install hiccup non-fatal; the
# hard gate below then fails the build if the toolchain isn't actually wired up.
npm ci || npm install || true
# Build the parser once so its dist/ exists (the interpreter imports the built
# bhai-lang-parser). Each run stage rebuilds it after applying patches.
npm run build --workspace=bhai-lang-parser || true

# Hard gate: a missing jest or an unresolvable bhai-lang-parser would otherwise
# yield empty output -> 0/0/0 (invalid instance). Under `set -e` this stops the
# build if either is absent.
npx jest --version
node -e "require.resolve('bhai-lang-parser')"

""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Shared test flow (same in all 3 stages; only the applied patches differ).
# Rebuild the parser first so the interpreter, which imports the built
# bhai-lang-parser, sees any patched grammar; ts-jest handles each package's own
# source. Then run Jest in both testable packages.
set -e
cd /home/{repo}

npm run build --workspace=bhai-lang-parser || true

echo "===== parser tests ====="
(cd packages/parser && npx jest --ci --runInBand --verbose) || true
echo "===== interpreter tests ====="
(cd packages/interpreter && npx jest --ci --runInBand --verbose) || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
bash /home/run_tests.sh

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch \
  || git apply --3way --whitespace=nowarn /home/test.patch || true
bash /home/run_tests.sh

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch \
  || git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch || true
bash /home/run_tests.sh

""".format(repo=self.pr.repo),
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


@Instance.register("DulLabs", "bhai-lang")
class BhaiLang(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BhaiLangImageDefault(self.pr, self._config)

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
        # Jest reporter output (jest ^27, ts-jest). Handles both the suite lines
        # (PASS/FAIL <file>) and the individual test lines (✓/✕/○ <name>), plus the
        # "Tests:" summary. ANSI is stripped first.
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_suite_pass = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*m?s\))?\s*$")
        re_suite_fail = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*m?s\))?\s*$")

        re_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_skip = re.compile(
            r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s+\(\d+\s*m?s\))?\s*$"
        )

        current_suite = ""

        for line in test_log.splitlines():
            clean = ansi_escape.sub("", line)

            m = re_suite_pass.match(clean)
            if m:
                suite_path = m.group(1).strip()
                if suite_path.endswith((".ts", ".js", ".tsx", ".jsx")):
                    current_suite = suite_path
                    passed_tests.add(f"SUITE:{suite_path}")
                continue

            m = re_suite_fail.match(clean)
            if m:
                suite_path = m.group(1).strip()
                if suite_path.endswith((".ts", ".js", ".tsx", ".jsx")):
                    current_suite = suite_path
                    failed_tests.add(f"SUITE:{suite_path}")
                    passed_tests.discard(f"SUITE:{suite_path}")
                continue

            m = re_pass.match(clean)
            if m:
                test_name = m.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                passed_tests.add(test_name)
                continue

            m = re_fail.match(clean)
            if m:
                test_name = m.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue

            m = re_skip.match(clean)
            if m:
                test_name = m.group(1).strip()
                if current_suite:
                    test_name = f"{current_suite} > {test_name}"
                skipped_tests.add(test_name)
                continue

        # Enforce TestResult disjointness invariants.
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
