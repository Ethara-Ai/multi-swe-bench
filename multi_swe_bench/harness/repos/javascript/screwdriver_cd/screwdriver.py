"""Repo config for screwdriver-cd/screwdriver (JavaScript / Mocha + hapi).

Runner
------
The gold test patch for PR #1952 adds a ``describe('GET /templates/name/metrics')``
block with three ``it()`` cases to ``test/plugins/templates.test.js``, plus the
fixture ``test/plugins/data/templateVersionsMetrics.json``. The fix patch adds
the route module ``plugins/templates/listVersionsWithMetric.js`` and registers it
in ``plugins/templates/index.js``.

``package.json`` defines::

    "pretest": "eslint .",
    "test": "nyc --report-dir ./artifacts/coverage --reporter=lcov mocha
             --recursive --timeout 4000 --retries 1 --exit
             --allow-uncaught true --color true"

The graded stages call ``mocha`` directly rather than ``npm test``:

* ``pretest`` would run ``eslint .`` first and abort the whole stage on a lint
  error. eslint 4 and eslint-config-screwdriver are resolved from unpinned ``^``
  ranges, so lint output is a property of the registry, not of the PR under
  test. It must not decide pass/fail.
* ``nyc`` only adds coverage instrumentation. It costs time and interleaves its
  own output with the spec tree that ``parse_log`` consumes.
* ``--color true`` forces ANSI even off a TTY. Dropping it keeps the log clean;
  escapes are stripped in ``parse_log`` regardless.
* ``--allow-uncaught true`` is dropped so it falls back to Mocha's default
  (false). With it enabled an uncaught error kills the process and truncates the
  log mid-suite; with it off the error is attributed to the running test as a
  failure and the remaining files still execute.

``--recursive``, ``--retries 1`` and ``--exit`` are kept from upstream.
``--retries 1`` is safe for parsing: Mocha's reporter emits only a test's final
outcome, so a retried test produces one spec line, not two. ``--exit`` is
load-bearing -- the suite builds hapi servers and leaves handles open, so
without it the process never returns. The timeout is raised 4000ms -> 10000ms
because the three stages may run concurrently with other instances.

Environment
-----------
``screwdriver.yaml`` pins CI to ``image: node:12`` and runs a bare
``npm install``; this config mirrors both.

``NODE_ENV`` is deliberately left unset. Upstream CI does not set it, and
node-config would otherwise look for a ``config/test.yaml`` that does not exist
at this commit -- only ``config/default.yaml`` and
``config/custom-environment-variables.yaml`` are shipped.

No ``apt-get`` runs in the base image, which is a deliberate deviation from the
usual toolchain-install layer. ``node:12`` is Debian **stretch**, long past EOL:
its indexes are gone from ``deb.debian.org`` and ``apt-get update`` exits 100 on
``dists/stretch/main/binary-amd64/Packages`` (404, measured in-container
2026-08-21). ``Image._get_apt_update_command`` carries an archive.debian.org
rewrite for exactly this, but it only fires when the base image string matches
``Image.DEPRECATED_DEBIAN_IMAGES`` (``"debian:stretch"``, ...) -- ``"node:12"``
does not match, so the default apt layer would hard-fail the build. It is also
unnecessary: the image already ships git 2.11, ca-certificates, gcc 6.3 + make
4.1, curl 7.52 and python 2.7.13/3.5.3, and ``npm install`` compiles this tree's
native addons against exactly that toolchain.

Dependency pinning is load-bearing
----------------------------------
This commit ships **no lockfile** -- no ``package-lock.json``, no ``yarn.lock``,
no ``npm-shrinkwrap.json`` -- while ``package.json`` declares roughly sixty
``^``-ranged dependencies. A bare ``npm install`` therefore resolves against
*today's* registry, and the result no longer runs on Node 12.

Measured 2026-08-21, unpinned: ``npm install`` succeeded (1157 packages) but the
suite died before executing a single test::

    /home/screwdriver/node_modules/@so-ric/colorspace/dist/index.cjs.js:1976
            (limiters[m] ||= [])[channel] = modifier;
    SyntaxError: Unexpected token '='

``||=`` is ES2021 and Node 12 cannot parse it. The package arrives through
``screwdriver-logger`` -> ``winston`` -> ``@dabh/diagnostics`` ->
``@so-ric/colorspace``, so the crash happens at ``require`` time while Mocha
loads ``test/plugins/builds.test.js``. All three graded stages reported
``(0, 0, 0)`` and the instance was rejected as invalid.

``--before=2020-02-14`` -- the day after this PR merged (``merged_at``
``2020-02-13T00:35:14Z`` in the raw dataset) -- constrains every transitive
resolution to versions published by that date, reproducing the tree the tests
were written against. npm 6.14 (bundled with node:12) supports the flag.
Measured with the pin: 1062 packages, ``@so-ric/colorspace`` absent, winston
3.2.1, and the full suite reports **746 passing, 1 pending, 0 failing**.

This fixes the cause rather than the symptom. Pinning only the offending package
would leave every other unpinned range free to drift into the next Node 12
incompatibility; the date pin freezes all of them at once and keeps the instance
reproducible as the registry moves on.

Test identity
-------------
Mocha's ``spec`` reporter prints only the leaf name, indented beneath its suite
header, and leaf names are **not** unique in this repo. Two distinct tests inside
``describe('DELETE /templates/tags')`` are both named ``returns 403 when
pipelineId does not match``, and ``returns 404 when template does not exist``
recurs under six different route suites. Keying on the leaf alone would silently
merge those results and corrupt the f2p comparison.

``parse_log`` therefore rebuilds the suite path from output indentation and
reports ``Suite > Nested suite > test name``. Where a full path is still
genuinely duplicated -- the ``DELETE /templates/tags`` pair above -- occurrences
after the first are suffixed ``#2``, ``#3``, ... Mocha loads files in a
deterministic order and both duplicates sit in the same ``describe``, so the
numbering is stable across the three stages.

Trailing ``(64ms)`` durations are stripped: they vary run to run, and an
unstripped duration would make the same test a different name in each stage,
which surfaces as the ``PASS -> NONE -> FAIL`` anomaly ``Report.check()``
rejects.

The suite writes hapi/``good`` JSON log lines such as
``{"message":"...","level":"error",...}`` to stdout at column zero, interleaved
with the spec tree. The suite-header branch below has an indent floor of two
precisely so those lines are never mistaken for a ``describe`` and pushed onto
the stack.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Registry cut-off for `npm install`. See "Dependency pinning is load-bearing".
_NPM_BEFORE = "2020-02-14"

# Common body shared by run.sh / test-run.sh / fix-run.sh.
#
# Identical in all three by construction: the only thing that differs between
# the graded stages is which patch was applied before this block runs. Anything
# that varied the command itself would make a FAIL -> PASS transition
# attributable to the command rather than to the fix.
#
# No `|| true` here. If Mocha fails to start, the stage must fail loudly rather
# than hand parse_log an empty log and report a silent 0/0/0.
_TEST_BODY = """\
export NODE_OPTIONS="--max_old_space_size=4096"

./node_modules/.bin/mocha --recursive --timeout 10000 --retries 1 --exit \\
    --reporter spec
"""


class ScrewdriverImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 12 with the repo cloned.

    Tagged per PR rather than with a shared ``:base``: one shared tag would be
    rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into the standard clone +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args. Nothing is
    emitted after the clone line for exactly that reason -- the enhancer appends
    ``CMD`` there, and any later instruction would be stranded below it.
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
        # `image: node:12` is screwdriver.yaml's own CI runtime, and the only
        # one upstream builds against. package.json's `engines.node: ">=8.9.0"`
        # is a floor, not a choice.
        #
        # The major matters beyond the engines field: this commit predates any
        # lockfile, so `npm install` resolves ~60 unpinned `^` ranges live.
        # node:12 ships npm 6, whose loose peer-dependency handling accepts the
        # resulting tree; npm 7+ (node:16 and later) applies strict peer
        # resolution and rejects it. npm 6.14 is also what provides `--before`.
        return "node:12"

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

        # No apt layer -- node:12 is EOL Debian stretch and `apt-get update`
        # 404s. The toolchain it would install is already in the image. See the
        # "Environment" section of the module docstring for the measurement.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ScrewdriverImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the era-correct tree."""

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
        return ScrewdriverImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export npm_config_audit=false
export npm_config_fund=false
export NODE_OPTIONS="--max_old_space_size=4096"

# `--before` freezes every transitive `^` range at the day after this PR merged.
# Without it the winston chain resolves to @so-ric/colorspace, whose `||=`
# syntax Node 12 cannot parse, and the whole suite dies at require time.
#
# `|| true` so a native-module compile failure on arm64 cannot abort the image
# build. Real breakage still surfaces in the graded runs, where the test command
# carries no such guard.
npm install --no-audit --no-fund --before={npm_before} || true

""".format(pr=self.pr, npm_before=_NPM_BEFORE),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Mocha's spec reporter, selected by `--reporter spec`. Every marker is anchored
# on its own indentation, because indentation is the only thing carrying the
# suite nesting:
#
#     template plugin test
#       GET /templates
#         ✓ returns 200 and all templates
#         ✓ returns 200 and all templates with pagination (43ms)
#         - returns 200 and all namespaces using distinct query
#         1) returns 500 when datastore fails
_PASS_LINE = re.compile(r"^(\s*)[✓✔]\s+(.+?)\s*$")
_FAIL_LINE = re.compile(r"^(\s*)\d+\)\s+(.+?)\s*$")
_SKIP_LINE = re.compile(r"^(\s*)-\s+(.+?)\s*$")
# `746 passing (4m)` / `1 failing` / `1 pending` -- the tree ends here and the
# failure epilogue begins. That epilogue re-prints each failure as
# `1) <suite>` at the same indent the live `1) <test>` lines used, so parsing
# must stop here or every failing suite title becomes a phantom failing test.
_SUMMARY_LINE = re.compile(r"^\s*\d+\s+(?:passing|failing|pending)\b")
# Trailing duration on slow tests. Varies per run, so it must not reach a name.
_DURATION = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")


def parse_mocha_spec_log(log: str) -> TestResult:
    """Rebuild ``Suite > Nested > test`` names from Mocha spec indentation.

    The leaf name alone is ambiguous in this repo -- see the module docstring --
    so a suite stack is maintained keyed on indentation width and each result is
    reported with its full path. Paths that are still duplicated get an
    occurrence suffix so two distinct tests never collapse into one name.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # ANSI first: without this every pattern below fails to anchor whenever the
    # log carries colour.
    clean = ANSI_ESCAPE.sub("", log)

    # (indent, suite name) for every suite currently open, outermost first.
    stack: list[tuple[int, str]] = []
    # Full path -> times seen, for disambiguating genuinely duplicated names.
    seen_counts: dict[str, int] = {}

    def path_for(indent: int, leaf: str) -> str:
        parts = [name for width, name in stack if width < indent]
        parts.append(leaf)
        path = " > ".join(parts)
        count = seen_counts.get(path, 0) + 1
        seen_counts[path] = count
        return path if count == 1 else f"{path} #{count}"

    for raw in clean.splitlines():
        if _SUMMARY_LINE.match(raw):
            break

        line = raw.rstrip()
        if not line.strip():
            continue

        m = _PASS_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            passed_tests.add(path_for(indent, leaf))
            continue

        m = _FAIL_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            failed_tests.add(path_for(indent, leaf))
            continue

        m = _SKIP_LINE.match(line)
        if m:
            indent, leaf = len(m.group(1)), _DURATION.sub("", m.group(2))
            skipped_tests.add(path_for(indent, leaf))
            continue

        # Anything else at a positive indent is a suite header. The indent floor
        # matters: the suite writes hapi/`good` JSON log lines to stdout at
        # column zero, and treating those as suites would poison every name
        # below them.
        indent = len(line) - len(line.lstrip())
        if indent >= 2:
            name = line.strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, name))

    # TestResult.__post_init__ rejects overlapping sets. A test that fails after
    # a retry can be reported both ways; failure is the honest verdict.
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


@Instance.register("screwdriver-cd", "screwdriver")
class Screwdriver(Instance):
    """Instance handler for screwdriver-cd/screwdriver.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on. The org keeps its hyphen -- the key is built from the JSONL ``org``
    field verbatim.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ScrewdriverImageDefault(self.pr, self._config)

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
        return parse_mocha_spec_log(log)
