import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""

# honeybadger-vue's devDependencies pull in browser-automation packages
# (chromedriver, selenium-server) whose postinstall hooks download binaries from
# the network.  Those binaries are only needed by the e2e suite, which this
# harness never runs, and the downloads are the single most common cause of a
# flaky/failing image build.  Skipping them keeps `npm ci` hermetic.
_INSTALL_ENV = """export CHROMEDRIVER_SKIP_DOWNLOAD=true
export SELENIUM_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_DOWNLOAD=true
export npm_config_audit=false
export npm_config_fund=false
export CI=true
export NODE_ENV=test
"""

# `npm test` -> `npm run unit` -> `jest --config test/unit/jest.conf.js --coverage`.
# Nested npm scripts do not forward `--` arguments, so the reporter flags are
# passed to `npm run unit` (the script that actually invokes jest) instead.
# --verbose makes jest print one line per test case (`✓`/`✕`/`○`), which is what
# parse_log() consumes; without it jest only prints per-file totals.
_TEST_CMD = "npm run unit -- --verbose --no-color --runInBand"


class HoneybadgerVueImageBase(Image):
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
        # honeybadger-vue is a Vue 2 library built with the vue-cli webpack
        # template; its CI (.github/workflows/nodejs.yml) runs `npm ci` on Node
        # 10/12/14. The committed package-lock.json is lockfileVersion 2, which
        # still carries the legacy `dependencies` block that npm 6 reads.
        # Node 16+ ships npm 8, whose strict peer-dependency resolution rejects
        # this tree outright (eslint@8 vs. eslint-config-standard@16, which
        # peer-depends on eslint@^7.12.1), so `npm ci` fails with ERESOLVE.
        # Node 14 ships npm 6, which resolves the tree exactly as upstream CI
        # does, and its engines field (`node >= 6.0.0`) is satisfied.
        return "node:14"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class HoneybadgerVueImageDefault(Image):
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
        return HoneybadgerVueImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{install_env}
npm ci || true

""".format(pr=self.pr, install_env=_INSTALL_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{install_env}
{test_cmd}

""".format(pr=self.pr, install_env=_INSTALL_ENV, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
{install_env}
{test_cmd}

""".format(pr=self.pr, install_env=_INSTALL_ENV, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
{install_env}
{test_cmd}

""".format(pr=self.pr, install_env=_INSTALL_ENV, test_cmd=_TEST_CMD),
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


@Instance.register("honeybadger-io", "honeybadger-vue")
class HoneybadgerVue(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HoneybadgerVueImageDefault(self.pr, self._config)

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

        # jest's verbose reporter nests test cases under their `describe` blocks
        # using indentation, e.g.
        #
        #   PASS test/unit/specs/HoneyBadgerVue.spec.js
        #     HoneybadgerVue
        #       ✓ should add an errorHandler (1 ms)
        #       when a component has props
        #         ✓ should pass the props in the error notification (59 ms)
        #
        # A bare test name is ambiguous both across describe blocks and across
        # spec files, so every level is joined with '::' into a pytest-style node
        # id -- the convention used elsewhere in this harness (see
        # javascript/tjw_lint/jest_serializer_vue_tjw.py):
        #
        #   test/unit/specs/HoneyBadgerVue.spec.js::HoneybadgerVue::should add an errorHandler
        #
        # The leading path comes from the PASS/FAIL header that opens each file
        # block; the middle segments are the enclosing describe() blocks.
        ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        # Test-suite result header; also resets the describe stack for the file.
        file_header_re = re.compile(r"^(?:PASS|FAIL|RUNS|SKIP|TODO)\b")
        # Pulls the spec path out of e.g.
        # "FAIL test/unit/specs/HoneyBadgerVue.spec.js (5.702 s)". The broad header
        # regex above still resets state for a header whose path we cannot parse.
        file_path_re = re.compile(
            r"^(?:PASS|FAIL|RUNS|SKIP|TODO)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\b"
        )
        # Trailing "(5 ms)" / "(1.2 s)" duration is optional: jest omits it for
        # sub-millisecond tests.
        case_re = re.compile(
            r"^(?P<indent>\s*)"
            r"(?P<marker>[✓✔√✕✖✗×○◯✎])"
            r"\s+(?P<name>.*?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )
        # Everything from the failure detail (`●`) / summary block onward is prose,
        # not test structure, so it must not feed the describe stack.
        summary_re = re.compile(
            r"^(?:Test Suites|Tests|Snapshots|Time|Ran all test suites|Summary of all failing tests)\b"
        )
        pass_markers = {"✓", "✔", "√"}
        fail_markers = {"✕", "✖", "✗", "×"}
        skip_markers = {"○", "◯", "✎"}

        stack: list[tuple[int, str]] = []
        current_file = ""
        name_counts: dict[str, int] = {}
        in_file = False
        in_details = False

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if file_header_re.match(line):
                path_match = file_path_re.match(line)
                current_file = path_match.group(1) if path_match else ""
                stack = []
                in_file = True
                in_details = False
                continue

            if summary_re.match(stripped):
                in_file = False
                in_details = True
                continue

            # `● suite › test` failure details and `console.log`/`console.error`
            # capture blocks both trail the test list inside a file block.
            if stripped.startswith("●") or stripped.startswith("console."):
                in_details = True
                continue

            if not in_file or in_details:
                continue

            match = case_re.match(line)
            if match:
                indent = len(match.group("indent").expandtabs(2))
                name = match.group("name").strip()
                # jest prefixes non-executed cases with their reason.
                for prefix in ("skipped ", "todo "):
                    if name.startswith(prefix):
                        name = name[len(prefix) :].strip()
                if not name:
                    continue

                segments = [title for depth, title in stack if depth < indent]
                segments.append(name)
                if current_file:
                    segments.insert(0, current_file)
                base_name = "::".join(segments)
                # Two tests can share a fully-qualified name inside one file;
                # disambiguate by occurrence so they do not collapse into a single
                # id (--runInBand keeps that order stable across the 3 stages).
                seen = name_counts.get(base_name, 0) + 1
                name_counts[base_name] = seen
                test_name = base_name if seen == 1 else f"{base_name}#{seen}"

                marker = match.group("marker")
                if marker in pass_markers:
                    passed_tests.add(test_name)
                elif marker in fail_markers:
                    failed_tests.add(test_name)
                elif marker in skip_markers:
                    skipped_tests.add(test_name)
                continue

            # Any other indented line inside a file block is a `describe` header.
            indent = len(line[: len(line) - len(line.lstrip())].expandtabs(2))
            if indent <= 0:
                continue
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, stripped))

        # A test id can show up in more than one bucket (a retried test reported
        # both ways, or the same name in two spec files). Resolve precedence the
        # way the rest of the harness does -- fail > skip > pass -- so a flaky
        # test is never credited as passing, and TestResult's disjointness
        # invariants (test_result.py:82-93) always hold.
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
