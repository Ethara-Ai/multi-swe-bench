"""Repo config for ``webdriverio/expect-webdriverio``.

REGISTRATION
------------
The raw dataset carries no ``number_interval`` and no ``tag``, so
``Instance.create()`` (instance.py:40-51) builds the key ``webdriverio/expect-webdriverio``.
That is the single key registered here; era selection happens *inside* the
instance, keyed off the PR number.

ERA BOUNDARY (evidence, not guesswork)
--------------------------------------
The repo migrated its unit suite from Jest to Vitest in commit ``4c0b4f06``
("start rewriting some of the tests to vitest", 2022-08-10). That same commit
deletes ``jest.config.js`` and adds ``vitest.config.ts``::

    git log origin/main --diff-filter=A -- vitest.config.ts  -> 4c0b4f06 2022-08-10
    git log origin/main --diff-filter=D -- jest.config.js    -> 4c0b4f06 2022-08-10

Highest PR merged before that commit is #820 (2022-07-25); lowest merged after
is #889 (2022-10-05). Nothing lands in 821-888, so any boundary in that window
is exact. ``JEST_ERA_MAX_PR = 820``.

VERIFICATION STATUS
-------------------
Jest era (PR <= 820) is verified end-to-end on ``node:20-bookworm`` against the
only PR in the dataset, #487 (base ``dda42f7c``)::

    run  : 31 suites / 351 tests pass, exit 0
    test : test/utils.test.ts fails to compile (ts-jest TS2554), 347 pass, exit 1
    fix  : 31 suites / 364 tests pass, exit 0

``npm install`` succeeds on node 20 (repo has ``package-lock.json``, no
``engines`` field; CI of that era used node 10/12/14, all long EOL). No build
step is required: ``jest.config.js`` uses the ``ts-jest`` preset and compiles
``src/`` on the fly.

Vitest era (PR >= 821) is NOT verified — no PR from that era is present in any
dataset in hand. It is retained so the key keeps routing rather than crashing;
treat its scripts and its half of ``parse_log`` as UNVERIFIED until a
post-#888 instance is run.

TEST-NAME SHAPE (why parse_log is hierarchical)
-----------------------------------------------
Jest's ``--verbose`` reporter prints only the leaf ``it()``/``test()`` title,
indented under its ``describe()`` chain. Leaf titles in this repo collide
massively — "expect message", "does not pass", "exact passes" and friends recur
across nearly every matcher suite. Measured on the real ``run`` log: 351 result
lines collapse to 126 distinct leaf titles, i.e. 225 collisions. Collapsing
those would destroy the per-stage status of every colliding test.

``parse_log`` therefore reconstructs the full path from indentation --
``file > describe > ... > leaf`` -- and strips the trailing ``(N ms)`` duration,
which varies between stages and would otherwise make the same test appear under
three different names across the three stages (report.py:92-102 unions names
across stages; a mismatch manufactures phantom NONE entries and can trip
Report.check rule 4). With the hierarchical names, all 351/347/364 result lines
map to distinct entries: 0 collisions in every stage.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# At and below this PR number the suite is Jest + ts-jest; above it, Vitest.
# See ERA BOUNDARY in the module docstring for the commits this is derived from.
JEST_ERA_MAX_PR = 820

_JEST_TEST_CMD = "npx jest --no-coverage --verbose"
_VITEST_TEST_CMD = "npx vitest run --reporter=verbose"

_RUN_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
"""


def _prepare_script(pr: PullRequest) -> str:
    # `set -e` (not `-eo pipefail`): the `|| true` on install is what absorbs a
    # non-fatal native-module build failure on arm64, and there is no pipeline.
    return f"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

npm install --no-audit --no-fund || true
"""


def _run_script(pr: PullRequest, test_cmd: str) -> str:
    return _RUN_SCRIPT_PREAMBLE.format(repo=pr.repo) + f"{test_cmd}\n"


def _test_run_script(pr: PullRequest, test_cmd: str) -> str:
    return (
        _RUN_SCRIPT_PREAMBLE.format(repo=pr.repo)
        + "git apply --whitespace=nowarn /home/test.patch\n"
        + f"{test_cmd}\n"
    )


def _fix_run_script(pr: PullRequest, test_cmd: str) -> str:
    # test.patch first, then fix.patch — the fix patch is authored against a tree
    # that already carries the test changes.
    return (
        _RUN_SCRIPT_PREAMBLE.format(repo=pr.repo)
        + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        + f"{test_cmd}\n"
    )


def _instance_files(pr: PullRequest, test_cmd: str) -> list[File]:
    """The standard 6 files. Every one of them is COPY'd by `_default_dockerfile`."""
    return [
        File(".", "fix.patch", f"{pr.fix_patch}"),
        File(".", "test.patch", f"{pr.test_patch}"),
        File(".", "prepare.sh", _prepare_script(pr)),
        File(".", "run.sh", _run_script(pr, test_cmd)),
        File(".", "test-run.sh", _test_run_script(pr, test_cmd)),
        File(".", "fix-run.sh", _fix_run_script(pr, test_cmd)),
    ]


class _ExpectWebDriverIOImageBase(Image):
    """Clone-only base layer.

    Deliberately minimal: no proxy/cert/label/ARG plumbing, because
    ``DockerfileEnhancer.enhance()`` (image.py:265-291) injects all of that and
    rewrites the hardcoded ``git clone`` into the parameterised
    ``REPO_URL``/``BASE_COMMIT`` form plus the history-hardening block.
    """

    #: Overridden per era so the two base images never dedupe onto each other
    #: (Image.__eq__/__hash__ key off image_full_name()).
    TAG = "base"
    BASE_IMAGE = "node:20-bookworm"

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
        return self.BASE_IMAGE

    def image_tag(self) -> str:
        return self.TAG

    def workdir(self) -> str:
        return self.TAG

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


class ExpectWebDriverIOJestImageBase(_ExpectWebDriverIOImageBase):
    TAG = "base-jest"
    BASE_IMAGE = "node:20-bookworm"


class ExpectWebDriverIOVitestImageBase(_ExpectWebDriverIOImageBase):
    TAG = "base-vitest"
    BASE_IMAGE = "node:20-bookworm"


class _ExpectWebDriverIOImageDefault(Image):
    BASE_CLS = _ExpectWebDriverIOImageBase
    TEST_CMD = _JEST_TEST_CMD

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
        return self.BASE_CLS(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return _instance_files(self.pr, self.TEST_CMD)

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


class ExpectWebDriverIOJestImageDefault(_ExpectWebDriverIOImageDefault):
    BASE_CLS = ExpectWebDriverIOJestImageBase
    TEST_CMD = _JEST_TEST_CMD


class ExpectWebDriverIOVitestImageDefault(_ExpectWebDriverIOImageDefault):
    BASE_CLS = ExpectWebDriverIOVitestImageBase
    TEST_CMD = _VITEST_TEST_CMD


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Jest suite header: "PASS test/utils.test.ts" or "FAIL test/x.test.ts (22.021 s)".
_JEST_SUITE_RE = re.compile(
    r"^(?P<status>PASS|FAIL)\s+(?P<path>\S+\.(?:test|spec)\.[cm]?[jt]sx?)"
    r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
)
_PASS_MARKS = "\u2713\u2714\u221a"  # v  (jest/vitest pass; \u221a on Windows)
_FAIL_MARKS = "\u2715\u2716\u2717\u00d7"  # x  (jest fail; \u00d7 on vitest/Windows)
_SKIP_MARKS = "\u25cb\u25ef\u26ac\u270e\u21bb"  # circle / pencil (skipped, todo)
_MARK_RE = re.compile(
    r"^(?P<indent>[ ]*)(?P<mark>["
    + _PASS_MARKS
    + _FAIL_MARKS
    + _SKIP_MARKS
    + r"])[ ]+(?P<name>.*\S)\s*$"
)
_DESCRIBE_RE = re.compile(r"^(?P<indent>[ ]{2,})(?P<title>\S.*?)\s*$")
# Trailing per-test duration. Varies run to run, so it MUST be stripped or the
# same test gets three different names across the three stages.
_JEST_DURATION_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\)\s*$")
_SKIP_PREFIX_RE = re.compile(r"^(?:skipped|todo)\s+", re.IGNORECASE)

# Vitest verbose already emits fully-qualified "file > describe > test" names.
_VITEST_DURATION_RE = re.compile(r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?\s*$")
_VITEST_SKIP_RE = re.compile(r"^\s*[\u2193-]\s+(?P<name>.+?)\s*\[skipped\]\s*$")
_VITEST_FAIL_FILE_RE = re.compile(
    r"^\s*FAIL\s+(?P<path>\S+\.(?:test|spec)\.[cm]?[jt]sx?)"
)


def _strip_ansi(log: str) -> str:
    return _ANSI_RE.sub("", log).replace("\r\n", "\n").replace("\r", "\n")


def _parse_jest_log(
    cleaned: str,
) -> tuple[set[str], set[str], set[str]]:
    """Rebuild ``file > describe > ... > leaf`` names from jest --verbose output.

    Jest emits one contiguous, blank-line-delimited block per test file::

        PASS test/util/formatMessage.test.ts (22.021 s)
          formatMessage
            enhanceError
              default
                v starting message (12 ms)

    Indentation is exactly 2 spaces per nesting level, so a line's indent gives
    its depth directly. A blank line closes the block, which is what keeps the
    failure-detail sections ("* Test suite failed to run", TS diagnostics,
    "Summary of all failing tests") from being mistaken for describe titles.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    current_file: str | None = None
    describes: list[str] = []

    for raw_line in cleaned.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            current_file = None
            describes = []
            continue

        suite = _JEST_SUITE_RE.match(line)
        if suite:
            current_file = suite.group("path")
            describes = []
            # File-level result. Kept alongside the per-test entries because a
            # suite that fails to COMPILE (the common shape of a JS/TS
            # fail-to-pass) reports zero individual tests — the file entry is
            # then the only FAIL->PASS signal in the whole log.
            if suite.group("status") == "PASS":
                passed.add(current_file)
            else:
                failed.add(current_file)
            continue

        if current_file is None:
            continue
        if line.lstrip().startswith("\u25cf"):  # bullet of a failure detail block
            continue

        mark = _MARK_RE.match(line)
        if mark:
            depth = max(len(mark.group("indent")) // 2 - 1, 0)
            name = _JEST_DURATION_RE.sub("", mark.group("name")).strip()
            marker = mark.group("mark")
            if marker in _SKIP_MARKS:
                name = _SKIP_PREFIX_RE.sub("", name).strip()
            if not name:
                continue
            full = " > ".join([current_file] + describes[:depth] + [name])
            if marker in _PASS_MARKS:
                passed.add(full)
            elif marker in _FAIL_MARKS:
                failed.add(full)
            else:
                skipped.add(full)
            continue

        describe = _DESCRIBE_RE.match(line)
        if describe:
            depth = max(len(describe.group("indent")) // 2 - 1, 0)
            describes = describes[:depth]
            describes.append(describe.group("title"))

    return passed, failed, skipped


def _parse_vitest_log(
    cleaned: str,
) -> tuple[set[str], set[str], set[str]]:
    """UNVERIFIED — see VERIFICATION STATUS in the module docstring.

    Vitest's verbose reporter already prints fully-qualified
    ``file > describe > test`` names, so no indentation bookkeeping is needed;
    only the trailing duration has to go.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for raw_line in cleaned.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        skip = _VITEST_SKIP_RE.match(line)
        if skip:
            name = skip.group("name").strip()
            if name:
                skipped.add(name)
            continue

        fail_file = _VITEST_FAIL_FILE_RE.match(line)
        if fail_file:
            failed.add(fail_file.group("path"))
            continue

        mark = _MARK_RE.match(line)
        if mark:
            name = _VITEST_DURATION_RE.sub("", mark.group("name")).strip()
            if not name:
                continue
            marker = mark.group("mark")
            if marker in _PASS_MARKS:
                passed.add(name)
            elif marker in _FAIL_MARKS:
                failed.add(name)
            else:
                skipped.add(_SKIP_PREFIX_RE.sub("", name).strip())

    return passed, failed, skipped


@Instance.register("webdriverio", "expect-webdriverio")
class ExpectWebDriverIO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _is_jest_era(self) -> bool:
        return self.pr.number <= JEST_ERA_MAX_PR

    def dependency(self) -> Image | None:
        if self._is_jest_era():
            return ExpectWebDriverIOJestImageDefault(self.pr, self._config)
        return ExpectWebDriverIOVitestImageDefault(self.pr, self._config)

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
        cleaned = _strip_ansi(test_log)

        if self._is_jest_era():
            passed_tests, failed_tests, skipped_tests = _parse_jest_log(cleaned)
        else:
            passed_tests, failed_tests, skipped_tests = _parse_vitest_log(cleaned)

        # TestResult.__post_init__ (test_result.py:56-101) rejects overlapping
        # sets. A failure is the strongest signal, then a skip.
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
