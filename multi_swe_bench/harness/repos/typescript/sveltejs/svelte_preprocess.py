r"""Repo config for sveltejs/svelte-preprocess (TypeScript / Jest via ts-jest).

Placed under ``repos/typescript/sveltejs/``. GitHub classifies the repo as
121,350 bytes TypeScript against 361 bytes JavaScript (99.7%), and the language
directory follows that classification, not the org: the sibling ``sveltejs``
package under ``repos/javascript/`` holds ``svelte`` and ``kit``, which are
different repos and may live elsewhere. ``validate_dataset.py`` derives the
emitted ``lang`` field from this directory via the module path, so the placement
is load-bearing for the dataset record even though ``Instance.create`` routes on
the bare ``org/repo`` key.

PR #257 ("Fix/globalify nth-child", closes #224) changes one regular expression
in ``src/modules/globalifySelector.ts``::

    -const combinatorPattern = /(?<!\\)(?:\\\\)*([ >+~,]\s*)(?![^[]+\])/g;
    +const combinatorPattern = /(?<!\\)(?:\\\\)*([ >+~,]\s*)(?![^[]+\]|\d)/g;

Without the ``|\d`` alternative the ``+`` in ``:nth-child(2n+1)`` is treated as
a CSS sibling combinator, so the selector is split mid-expression and globalified
as ``:global(tr:nth-child(2n)+:global(1))``. The gold test patch adds
``test/modules/globalifySelector.test.ts`` whose ``works with nth-child`` case
asserts the whole ``An+B`` micro-syntax stays inside one ``:global(...)``.

Everything unusual below follows from the repo being a September 2020 tree whose
``package.json`` pins nothing but caret ranges.

Dependency resolution is load-bearing
-------------------------------------
Upstream CI (``.github/workflows/ci.yml``) runs ``npm install && npm run
test:ci``. Reproducing that literally today does **not** work, and the failure is
silent rather than loud. ``npm install`` ignores the committed ``yarn.lock``, so
``svelte: "^3.23.0"`` resolves to the current 3.59.x, whose ``Processed`` type is
declared ``void | Processed`` where 3.24.0 declared ``Processed``. ts-jest
type-checks every file it transforms, so ``src/autoProcess.ts`` fails to compile
and Jest reports::

    Test Suites: 24 failed, 24 total
    Tests:       0 total

Measured 2026-08-26 on ``node:14-bullseye``. Zero tests in *every* stage means
``fix_patch_result.all_count == 0``, which ``Report.check()`` rule 1 rejects --
an instance that looks broken but is really just a floating-range resolution six
years past its lockfile.

``yarn install --frozen-lockfile`` is therefore used instead of ``npm install``.
``yarn.lock`` is committed at the base commit and pins the contemporary set:
svelte 3.24.0, typescript 3.9.7, jest 25.5.4, ts-jest 25.5.1, node-sass 4.14.1,
sass 1.26.10, less 3.12.2, stylus 0.54.8, pug 3.0.0. With it all 24 baseline
suites compile and 133/133 tests pass. This is a fidelity *gain*, not a
deviation: the lockfile is what the PR author actually had installed.

Node version
------------
Node **14**. The CI matrix is ``[8, 10, 12]`` and ``engines.node`` is
``>= 9.11.2``, but Debian bullseye images exist only from Node 14 up (the Node 12
line stopped at buster, now archived), and node-sass 4.14.1 -- the one native
dependency in the tree -- publishes a prebuilt ``linux-x64-83`` binding for Node
14, so on amd64 nothing is compiled from source.

On arm64 there is no such binding and libsass is built from source instead; see
``SveltePreprocessImageBase.dockerfile`` for why ``python2`` is in the apt line
and what breaks without it.

Test identity and the file rename
---------------------------------
The gold test patch does two things: it adds
``test/modules/globalifySelector.test.ts``, and it **renames**
``test/modules.test.ts`` to ``test/modules/modules.test.ts``, deleting the old
inline ``globalifySelector`` describe block on the way.

Test names are reported as ``<repo-relative file> > <describe...> > <it>``, the
shape ``report._test_name_matches_files`` already understands for JS/TS. The
rename is therefore visible in the report: of the 11 tests in
``test/modules.test.ts`` at baseline, the 3 ``globalifySelector`` ones are
genuinely deleted and the other 8 reappear under ``test/modules/modules.test.ts``
-- so 11 ids read ``(PASS, NONE, NONE)`` and 8 new ids read ``(NONE, PASS,
PASS)``. That is the literal truth about what the patch did, and it is safe
against every ``check()`` rule: rule 2 needs ``test == PASS and fix == FAIL``,
rule 4 needs ``fix == FAIL``, and neither shape has a FAIL anywhere. Dropping the
path to hide the rename was rejected -- it would trade an accurate report for
title collisions across the 25 suites.

The credited test needs no matcher at all. ``globalifySelector > works with
nth-child`` is ``(NONE, FAIL, PASS)`` -- new in the test stage, failing there,
passing after the fix -- which rule 6 classifies as F2P from status alone.

The cheating guard is clear as well: the fix patch touches ``CHANGELOG.md``,
``package.json`` and ``src/modules/globalifySelector.ts``, none of which the gold
test patch touches, and ``src/modules/globalifySelector.ts`` does not
path-prefix-match ``test/modules/globalifySelector.test.ts``.

Duplicate titles
----------------
``test/autoProcess/style.test.ts`` declares ``it(\`should parse external
${lang}\`)`` **twice**, with byte-identical bodies, inside a ``forEach`` over
four languages. That is a copy-paste in the repo, not a parser artefact: Jest
counts all eight, so its own totals (133 at baseline, 134 after the test patch)
exceed the distinct titles by exactly four. Collapsing each pair onto one id
would leave every parsed count four short of the ``Tests: N total`` line in the
same log -- a discrepancy a reader would have to re-derive every time -- so
repeats within a file get an occurrence suffix (``... [#2]``) and the totals line
up exactly.

The suffix is stable, not incidental: Jest executes ``it()`` blocks in
declaration order within a file, so the n-th occurrence of a title is always the
same physical test, and the pair is byte-identical anyway.

Why ``--json`` and not the spec reporter
----------------------------------------
Jest is invoked directly rather than through ``npm run test:ci`` so that
``--json --outputFile`` can be added. The human reporter writes to stderr,
interleaves suite headers with per-test lines, and carries ANSI colour; the JSON
report gives ``ancestorTitles``/``title``/``status`` per assertion and the
absolute file path per suite, with no parsing ambiguity. The flags that differ
from ``test:ci`` (``jest --silent --no-cache``) are:

* ``--ci`` -- fails on a *missing* snapshot instead of silently writing one.
  Four snapshots are committed; without this flag a stage that lost them would
  quietly re-record and pass.
* ``--runInBand`` -- one worker, so ordering is identical in all three stages.
  The whole suite is ~15s, so this costs nothing.
* ``--coverage=false`` -- ``package.json`` sets ``collectCoverage: true``.
  Coverage instrumentation only adds a summary table to the log.
* ``--json --outputFile`` -- see above.

``--no-cache`` is kept from ``test:ci``: measured at 14.9s versus 16.3s cached,
so there is no reason to carry a cache across stages.
"""

import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Fenced so parse_log never has to guess which part of a container log is the
# report. The suite's own human-readable output is emitted before the opening
# marker and can never contain it.
_JSON_BEGIN = "-----MSB-JEST-JSON-BEGIN-----"
_JSON_END = "-----MSB-JEST-JSON-END-----"

# Absolute prefix Jest reports for every suite (`testResults[].name`), stripped
# to give repo-relative ids that match the paths in the patches.
_REPO_ROOT = "/home/svelte-preprocess/"

# Common body of run.sh / test-run.sh / fix-run.sh.
#
# Byte-identical in all three by construction: the only thing that may differ
# between the graded stages is which patch was applied before this block runs.
# Anything that varied the command itself would make a FAIL -> PASS transition
# attributable to the command rather than to the fix.
_TEST_BODY = """\
# Jest exits non-zero whenever a test fails, which is the *expected* outcome of
# the test stage -- `works with nth-child` fails there by design. Aborting on it
# would leave that stage with no results at all, so the exit code is captured
# and the run continues; the harness grades from the log text, not from it.
rm -f /tmp/jest-results.json /tmp/jest-stdio.log

set +e
./node_modules/.bin/jest \\
    --ci \\
    --no-cache \\
    --runInBand \\
    --silent \\
    --coverage=false \\
    --json \\
    --outputFile=/tmp/jest-results.json \\
    > /tmp/jest-stdio.log 2>&1
JEST_RC=$?
set -e

# Human-readable output first, for anyone reading the stage log by hand.
cat /tmp/jest-stdio.log

# Runner-start guarantee. Jest writes the report file even when suites fail to
# compile (`testExecError`), so an absent or empty file means the runner never
# came up -- fail the stage loudly instead of reporting a silent 0/0/0 that
# would surface much later as a Report rule-1 rejection.
if [ ! -s /tmp/jest-results.json ]; then
    echo "Error: jest wrote no result file (exit ${{JEST_RC}})" >&2
    exit 1
fi

echo "{begin}"
cat /tmp/jest-results.json
echo
echo "{end}"
""".format(begin=_JSON_BEGIN, end=_JSON_END)


class SveltePreprocessImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 14 plus the git/TLS essentials.

    Tagged per PR rather than with a shared ``:base``: one shared tag would be
    rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into the standard clone +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args.
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
        # See the module docstring: bullseye starts at Node 14, and 14 is the
        # highest ABI for which node-sass 4.14.1 ships a prebuilt linux-x64
        # binding. The -bullseye variants derive from buildpack-deps:*-scm, so
        # git and a C toolchain are already present.
        return "node:14-bullseye"

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

        # `git` and `ca-certificates` are what the harness itself needs for the
        # hardening block and the HTTPS fetch.
        #
        # `python2` is for arm64 and nothing else. Every preprocessor the suite
        # exercises -- sass, less, stylus, pug, coffeescript, postcss, babel --
        # is pure JavaScript, except node-sass, which needs a compiled libsass
        # binding. Measured 2026-08-26: node-sass 4.14.1 publishes prebuilt
        # bindings for darwin-x64, linux-ia32, linux-x64, linux_musl-x64 and
        # win32 only -- there is **no linux-arm64 asset at any ABI**. On arm64 it
        # therefore falls back to building libsass from source through node-gyp
        # 3.8.0, which requires Python *2* specifically and rejects Python 3, and
        # bullseye does not install one by default.
        #
        # The blast radius is much wider than the scss suites, because yarn 1.x
        # aborts the whole install when a build script fails: a probe without
        # python2 finished with 6 failures across five suites -- scss, but also
        # `transformers/babel`, `processors/babel` and `processors/postcss`,
        # whose packages were simply never set up after yarn gave up at
        # node-sass. `python2` costs ~4 MB on amd64, where the prebuilt binding
        # is downloaded and node-gyp never runs.
        #
        # g++/make come from the buildpack-deps base the node images derive from.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git python2 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SveltePreprocessImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the locked dependency set."""

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
        return SveltePreprocessImageBase(self.pr, self._config)

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
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# yarn, not npm -- see the module docstring. `--frozen-lockfile` pins the
# resolution to the committed lockfile.
#
# `|| true` is not optional: node-sass 4.14.1 is a native dependency resolved
# from a prebuilt binding, and a binding that is missing for the build
# architecture must not abort the image build. The graded runs surface any real
# breakage as test results. node_modules is gitignored, so this cannot dirty the
# tree for the asserts above or for the `git apply` in the run scripts.
yarn install --frozen-lockfile --non-interactive || true

# Prove at image-build time that the tree the graded stages will run against
# actually compiles and reports tests. A silent 0/0/0 discovered three stages
# later is far more expensive to diagnose than a failed build.
#
# The run itself is non-fatal for the same reason as the install -- a suite that
# fails here is data, not a broken image. The assertion is on the *report file*:
# jest writes it even when suites fail to compile, so an absent one means the
# runner never came up, and that does fail the build.
./node_modules/.bin/jest \\
    --ci --no-cache --runInBand --silent --coverage=false \\
    --json --outputFile=/tmp/jest-warmup.json > /tmp/jest-warmup.log 2>&1 || true
tail -n 6 /tmp/jest-warmup.log
test -s /tmp/jest-warmup.json

# The warm-up run must leave the tree exactly as it found it; `--ci` guarantees
# no snapshot is rewritten, and this asserts it.
bash /home/check_git_changes.sh

""".format(pr=self.pr),
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
    echo "Error: git apply failed" >&2
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
    echo "Error: git apply failed" >&2
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


# Jest 25 assertion statuses. "passed"/"failed" are self-explanatory; the rest
# all mean "declared but not executed" and map onto SKIP:
#   pending  -- it.skip / xit / a describe.skip ancestor
#   todo     -- it.todo
#   disabled -- filtered out by it.only elsewhere in the file
_SKIPPED_STATUSES = frozenset({"pending", "todo", "disabled", "skipped"})

_JSON_SPAN = re.compile(
    re.escape(_JSON_BEGIN) + r"\s*(.*?)\s*" + re.escape(_JSON_END),
    re.DOTALL,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_jest_json_log(log: str) -> TestResult:
    """Read the fenced ``jest --json`` report out of a stage log.

    Names are ``<repo-relative file> > <describe...> > <it>``. The file prefix
    is not decoration: the gold test patch renames ``test/modules.test.ts`` to
    ``test/modules/modules.test.ts``, and only a path-qualified id can tell the
    two apart. See the module docstring for why that rename is safe.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    matches = _JSON_SPAN.findall(log)
    if not matches:
        # No fence means the runner never reached its final echo. Reporting an
        # empty result is the honest outcome; Report.check() rule 1 turns it
        # into an explicit "no test results were captured" rejection.
        return TestResult(0, 0, 0, set(), set(), set())

    # Last fence wins: a retried stage appends rather than overwrites.
    #
    # ANSI is stripped first. Measured 2026-08-26: all three real stage logs
    # contain zero ESC bytes, because jest writes the report to a file rather
    # than to a colourising TTY -- so this is insurance, not a live fix. It is
    # safe insurance: JSON encodes a literal escape as the six characters
    # `\u001b`, never as a raw ESC byte, so no test name can be corrupted by it.
    try:
        report = json.loads(_ANSI_ESCAPE.sub("", matches[-1]))
    except json.JSONDecodeError:
        return TestResult(0, 0, 0, set(), set(), set())

    for suite in report.get("testResults") or []:
        path = suite.get("name") or ""
        # Jest reports absolute paths; the patches speak repo-relative ones.
        if _REPO_ROOT in path:
            path = path.split(_REPO_ROOT, 1)[1]

        # Per file, so a title repeated in two different files is never
        # suffixed -- the path already tells those apart.
        seen: dict[str, int] = {}

        for case in suite.get("assertionResults") or []:
            parts = [path]
            parts.extend(case.get("ancestorTitles") or [])
            parts.append(case.get("title") or "")
            name = " > ".join(p for p in parts if p)

            # style.test.ts declares the same `it()` twice; see the module
            # docstring. Suffixing the repeat keeps the parsed count equal to
            # Jest's own `Tests: N total`.
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [#{seen[name]}]"

            status = case.get("status")
            if status == "passed":
                passed_tests.add(name)
            elif status == "failed":
                failed_tests.add(name)
            elif status in _SKIPPED_STATUSES:
                skipped_tests.add(name)
            # Any future status is deliberately dropped rather than guessed at:
            # a mis-bucketed test corrupts the f2p comparison, an absent one
            # only narrows it.

    # Belt and braces: the occurrence suffix already makes ids unique within a
    # file, but TestResult.__post_init__ raises on any overlap and a raise here
    # would take down the whole stage. Failure is the honest verdict if a name
    # ever does report two ways.
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


@Instance.register("sveltejs", "svelte-preprocess")
class SveltePreprocess(Instance):
    """Instance handler for sveltejs/svelte-preprocess.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SveltePreprocessImageDefault(self.pr, self._config)

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
        return parse_jest_json_log(log)
