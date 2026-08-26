"""decentralized-identity/dwn-sdk-js harness config — Mocha + Chai, npm, TypeScript compiled to ESM."""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Single source of truth for the test invocation so run.sh / test-run.sh /
# fix-run.sh can never drift apart (a drift would make the f2p comparison
# meaningless because the three stages would execute different test sets).
#
# Mirrors the repo's own `test:node:ci` script:
#     npm run compile-validators && tsc && c8 mocha "dist/esm/tests/**/*.spec.js"
# with two additions:
#   * `--reporter spec`  -> pins the reporter parse_log() is written against,
#                           independent of any .mocharc / env reporter override.
#   * `--timeout 60000`  -> mocha's 2s default is not survivable under Docker
#                           (and far less under arm64 emulation) for a suite
#                           that does real key generation and LevelDB I/O.
#                           A too-short timeout produces nondeterministic
#                           failures that corrupt cross-stage status diffs.
TEST_CMD = 'npx c8 mocha --reporter spec --timeout 60000 "dist/esm/tests/**/*.spec.js" 2>&1'

# Build steps shared by all three run scripts.
#
# `npx tsc` is guarded with `|| true` because the test-only stage compiles the
# new tests against *unfixed* sources: with tsconfig `strict: true` the added
# `dataSize` filter is a type error until fix.patch lands. tsc still emits JS
# (tsconfig.json does not set `noEmitOnError`), so the suite is runnable — but
# an unguarded non-zero exit under `set -e` would kill the script before mocha
# ever starts, leaving an empty log and a 0/0/0 TestResult.
#
# The guard is deliberately limited to the compile step. `compile-validators`
# and the mocha command itself stay unguarded so a genuinely broken environment
# fails loudly instead of silently producing an empty report.
BUILD_STEPS = """\
npm run compile-validators
npx tsc || true"""

RUN_SCRIPT_TEMPLATE = """\
#!/bin/bash
set -eo pipefail
export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
{patch_step}{build_steps}

{test_cmd}
"""


def _run_script(repo: str, patch_step: str = "") -> str:
    """Render one of the three run scripts with an identical test command."""
    return RUN_SCRIPT_TEMPLATE.format(
        repo=repo,
        patch_step=f"{patch_step}\n" if patch_step else "",
        build_steps=BUILD_STEPS,
        test_cmd=TEST_CMD,
    )


class DwnSdkJsImageBase(Image):
    """Base Docker image: node:18-bookworm with the repo cloned.

    dwn-sdk-js declares `engines.node: ">= 18"`; build-essential/python3 are
    required by the native addons pulled in through level / abstract-level.
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

    def dependency(self) -> str | Image:
        return "node:18-bookworm"

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DwnSdkJsImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    The project compiles TypeScript to dist/esm/ before running mocha against
    the compiled JS.  The pipeline in every stage is:

        npm run compile-validators  (generate JSON-schema validators)
        tsc                         (compile TS -> dist/esm/)
        c8 mocha "dist/esm/tests/**/*.spec.js"
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
        return DwnSdkJsImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# Install dependencies. `|| true` is required: native addons (level /
# abstract-level) can fail to build on arm64 and that is not fatal for the
# JS-only test path.
npm ci || npm install || true

# Build only what tests need (validators + TypeScript compilation).
# Deliberately skip the full "npm run build", which also runs esbuild for the
# CJS/browser bundles - esbuild's native Go binary crashes under QEMU
# cross-arch emulation.
npm run compile-validators
npx tsc
""",
            ),
            File(
                ".",
                "run.sh",
                _run_script(self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                _run_script(
                    self.pr.repo,
                    "git apply --whitespace=nowarn /home/test.patch",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    self.pr.repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
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


# --- Mocha spec-reporter parsing ------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Trailing duration mocha appends to slow tests, e.g. "... (234ms)" / "(2s)".
# Must be stripped: the value differs between stages, and a test whose name
# carries its own runtime is a *different* name in every stage.
_DURATION_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|m)\)\s*$")

# Applied to the de-indented line; indentation is tracked separately.
_PASS_RE = re.compile(r"^[\u2713\u2714\u221a]\s+(.*\S)\s*$")
_FAIL_RE = re.compile(r"^\d+\)\s+(.*\S)\s*$")
_PENDING_RE = re.compile(r"^-\s+(.*\S)\s*$")
_SUMMARY_RE = re.compile(r"^\d+\s+(?:passing|failing|pending)\b")

# Lines that must never be mistaken for a describe() header.
_NOISE_RE = re.compile(
    r"^(?:at\s|npm\s|>\s|\+\s|\||-{3,}|={3,}|Error\b|\w*Error:|\d+%)"
)

_MAX_SUITE_DEPTH = 12


@Instance.register("decentralized-identity", "dwn-sdk-js")
class DECENTRALIZED_IDENTITY_DWN_SDK_JS(Instance):
    """Harness instance for decentralized-identity/dwn-sdk-js — Mocha + Chai."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DwnSdkJsImageDefault(self.pr, self._config)

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
        """Parse Mocha spec-reporter output into suite-qualified test sets.

        The spec reporter indents two spaces per nesting level and prints a
        test two levels deeper than its describe() header::

            RecordsQueryHandler.handle()
              filters
                ✓ should be able to query by dataSize (234ms)
                1) should reject an invalid range
                - pending case

            7 passing (4s)
            1 failing

            1) RecordsQueryHandler.handle()
                 should reject an invalid range:
               AssertionError: ...

        Two properties are essential and both come from the indentation:

        * **Uniqueness** — the bare it() text is not unique in this repo
          (the same titles run under several suites), so every name is
          qualified with its full describe() path: ``a > b > test``.
        * **Cross-stage stability** — failures are taken from the *inline*
          ``N) title`` lines emitted during the run, where the suite stack is
          known, and NOT from the epilogue failure list, whose header line
          carries only the top-level suite title. Using the epilogue would
          make the same test appear under one name when it fails and another
          when it passes, which is exactly the NONE→FAIL anomaly that
          invalidates a Report.

        Everything after the ``N passing`` / ``N failing`` summary is ignored
        so the epilogue and the c8 coverage table cannot inject fake names.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log)

        # (indent, title) for the currently open describe() blocks.
        suite_stack: list[tuple[int, str]] = []
        in_epilogue = False
        # The validation specs repeat one it() title at root level, where no
        # describe() path can separate the copies; merged into a set, a fix
        # flipping only the second copy would show no transition at all.
        # The occurrence index is stage-stable: mocha walks files in glob order
        # and tests in definition order.
        occurrences: dict[str, int] = {}

        def qualify(indent: int, title: str) -> str:
            while suite_stack and suite_stack[-1][0] >= indent:
                suite_stack.pop()
            name = " > ".join([t for _, t in suite_stack] + [title])
            seen = occurrences.get(name, 0) + 1
            occurrences[name] = seen
            return name if seen == 1 else f"{name} #{seen}"

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            # The summary marks the end of live test output. Anything after it
            # (failure details, coverage table) is not a test result line.
            if _SUMMARY_RE.match(stripped):
                in_epilogue = True
                continue
            if in_epilogue:
                continue

            indent = len(line) - len(line.lstrip())

            m = _PASS_RE.match(stripped)
            if m:
                passed_tests.add(qualify(indent, _DURATION_RE.sub("", m.group(1))))
                continue

            m = _FAIL_RE.match(stripped)
            if m:
                failed_tests.add(qualify(indent, _DURATION_RE.sub("", m.group(1))))
                continue

            m = _PENDING_RE.match(stripped)
            if m:
                skipped_tests.add(qualify(indent, _DURATION_RE.sub("", m.group(1))))
                continue

            # Anything else that is indented like a spec-reporter suite header
            # opens a new describe() scope. Top-level describes start at column
            # 2, so column-0 output (stray console logs, npm chatter) can never
            # clobber the stack.
            if indent >= 2 and indent % 2 == 0 and not _NOISE_RE.match(stripped):
                title = _DURATION_RE.sub("", stripped)
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()
                if len(suite_stack) < _MAX_SUITE_DEPTH:
                    suite_stack.append((indent, title))

        # TestResult invariants: the three sets must be pairwise disjoint.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
