"""Repo config for eugeneware/ffmpeg-static (Node / tape, PR #33).

What is actually graded
-----------------------
PR #33 ("Download platform/architecture specific binaries", closing issue #20
"split package to reduce install size") replaces a repo that *vendors* every
platform's ffmpeg under ``bin/<platform>/<arch>/`` with one that downloads the
single binary for the host at **npm install time**.

The three moving parts of the fix patch:

* ``index.js`` -- the exported path changes from
  ``<pkg>/bin/<platform>/<arch>/ffmpeg`` to ``<pkg>/ffmpeg``.
* ``install.js`` (new) -- fetches
  ``https://github.com/qawolf/ffmpeg-static/releases/download/<release>/<platform>-<arch>``
  and ``fs.chmodSync(ffmpegPath, 0o755)``.
* ``package.json`` -- adds ``"install": "node install.js"`` plus the two runtime
  deps that script needs (``progress``, ``simple-get``).

The gold test patch adds one assertion to ``test/index.js``::

    t.doesNotThrow(()=> {
      fs.accessSync(ffmpegPath, fs.constants.X_OK)
    }, "ffmpeg not executable");

This grades cleanly *because* ``bin/*`` is ``.gitignore``d (line 9 of
``.gitignore`` at the base commit) and the tree only carries ``.gitkeep``
placeholders. A checkout therefore has **no ffmpeg binary anywhere**, and the
pre-existing ``fs.statSync(ffmpegPath)`` on line 9 of the test throws ENOENT.
Only the fix patch's install step puts an executable binary at the advertised
path.

Measured 2026-08-24, node:14-bullseye, all three stages run with the identical
command:

===========  ==================  ==================================================
stage        patches applied     ``test/index.js`` outcome
===========  ==================  ==================================================
``run``      none                aborts -- ``ENOENT ... /bin/linux/x64/ffmpeg``
``test``     test.patch          aborts -- same ENOENT, before the new assertion
``fix``      test + fix.patch    ``ok 1``, ``ok 2``, ``ok 3 ffmpeg not executable``,
                                 ``1..3``, ``# ok``
===========  ==================  ==================================================

so the graded transition is ``FAIL -> PASS`` on
``test/index.js > should find ffmpeg``, which is what ``Report.check()`` rule 3
requires.

``Report.check()`` rule 5 (the cheating guard) is clean by construction: the test
patch touches only ``test/index.js`` while the fix patch touches ``index.js``,
``install.js``, ``package.json``, ``package-lock.json``, ``.gitignore``,
``.travis.yml``, ``build/index.sh``, the CI workflow and the ``bin/**/.gitkeep``
placeholders -- ``set(fix_patch_files) & set(test_patch_files)`` is empty, and
none of the fix files trips ``_looks_like_test_file`` (verified: no path segment
in ``{test, tests, __tests__, testing}``).

``npm install`` is load-bearing in every graded stage
-----------------------------------------------------
Unlike a normal JS repo, running the tests is not enough here: the entire fix is
an ``install`` lifecycle script. If the graded body only ran ``tape``, the fix
patch would change nothing observable and the instance would be dead on arrival.
Every stage therefore runs ``npm install`` *after* ``git apply`` and before the
suite. In the ``run`` and ``test`` stages the base ``package.json`` declares no
``install`` script and no new dependencies, so it is a sub-second no-op; in the
``fix`` stage it installs ``progress``/``simple-get`` and executes
``install.js``, which is the thing under test.

``--unsafe-perm`` is mandatory, not hygiene
-------------------------------------------
The harness runs as root. npm 6 drops privileges before running a lifecycle
script and, when it cannot, **skips the script and still exits 0**::

    npm WARN lifecycle ffmpeg-static@3.0.0~install: cannot run in wd
    ffmpeg-static@3.0.0 node install.js (wd=/tmp/r)
    added 45 packages from 25 contributors in 1.477s

Measured 2026-08-24: without ``--unsafe-perm`` the fix stage reported a
*successful* install, downloaded nothing, and the suite aborted with exactly the
same ENOENT as the baseline -- no ``FAIL -> PASS`` transition, and the instance
would have been rejected as unfixable rather than flagged as misconfigured.
That silent-success is the reason the flag is passed explicitly instead of
relying on the default.

Test identity and the aborted TAP stream
----------------------------------------
``package.json`` runs ``node_modules/.bin/tape test/*.js``. Two problems with
keying ``TestResult`` on tape's assertion lines directly:

* the assertion descriptions are **not unique** -- both ``t.ok`` calls in this
  file emit the default ``should be truthy``, so assertion-level names would
  collide (the HIGH-risk case in the audit's framework table);
* in the ``run`` and ``test`` stages the ENOENT is *not* caught by tape. It
  escapes the test callback and kills the process, so the stream stops after
  ``ok 1`` with **no** ``not ok`` line, no ``1..N`` plan and no ``# fail``
  summary. An assertion-level parser would see one passing assertion and call
  the stage green.

``parse_log`` therefore reports at the **tape test level** and treats a stream
that ends before its plan as a failure of whichever test was still open -- the
standard TAP-consumer reading of a truncated stream, and the honest one here: a
suite that died mid-test did not pass.

Names are ``<test file> > <test name>``, e.g.
``test/index.js > should find ffmpeg``. The file half is not cosmetic: it is the
JS/TS shape ``report.py``'s ``_test_name_matches_files`` recognises
(``test_name.startswith(f + " > ")``), which lets the classifier tie a credited
test back to ``test/index.js`` in the gold test patch. tape's TAP output does not
say which file it is running, so the run scripts loop over ``test/*.js`` and echo
a ``##### TAPFILE <path>`` marker before each one.

Neither name shape carries a duration, a count or an assertion index, so a name
is byte-identical across the three stages -- which matters because an unstable
name surfaces as the ``PASS -> NONE -> FAIL`` anomaly rule 4 rejects.

Toolchain
---------
``node:14-bullseye``. ``package.json`` declares ``engines.node >= 10``; Node 14
is the LTS contemporary with the PR and is the oldest line whose bundled npm
still behaves predictably with ``--unsafe-perm``. bullseye rather than alpine:
the downloaded ffmpeg is a glibc static build, and the ``node:*-bullseye``
variants derive from ``buildpack-deps:*-scm``, so ``git`` (2.30.2) and
``ca-certificates`` are already present -- verified in the image, hence no apt
layer at all.

Known risk -- the binary is fetched live
-----------------------------------------
``install.js`` downloads ~75 MB from ``github.com/qawolf/ffmpeg-static``
releases, pinned by ``package.json``'s ``ffmpeg-static.binary_release`` field
(``b3.0.0-2``). That is the PR's actual mechanism, so it cannot be stubbed out
without grading something other than the fix. Verified reachable 2026-08-24:
both ``linux-x64`` and ``linux-arm64`` return HTTP 200. There is no useful
pre-warm -- caching the two npm deps saves under a second, and the binary itself
is not an npm artifact. If every stage reports the suite aborting with ENOENT,
check the download first; that is the expected failure signature.

Architecture
------------
Genuinely arch-neutral. ``install.js`` builds its URL from ``os.arch()``, and the
``linux-arm64`` asset exists, so an arm64 image downloads an arm64 ffmpeg. The
test only checks ``X_OK`` and never executes it. Nothing in the Dockerfile is
arch-bound.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One npm invocation, defined once so the three graded scripts cannot drift.
# --unsafe-perm : see the module docstring -- without it npm silently skips the
#                 `install` lifecycle script as root and the fix does nothing.
# --no-audit/--no-fund : network chatter that cannot change the result.
# --loglevel=error : keeps the progress bar out of the captured log; the real
#                 output is kept in /tmp/npm-install.out and echoed only on
#                 failure.
_NPM_INSTALL = (
    "npm install --unsafe-perm --no-audit --no-fund --loglevel=error"
)

# Shared body of run.sh / test-run.sh / fix-run.sh. Identical in all three by
# construction: the only thing that differs between the graded stages is which
# patch was applied above this block. Anything that varied the command itself
# would make a FAIL -> PASS transition attributable to the command rather than
# to the fix.
_TEST_BODY = """\
# Runs in /home/{repo}; every caller cd's there first.
#
# Deliberately non-fatal, then re-armed. The `run` and `test` stages are
# *supposed* to end non-zero: with no ffmpeg binary on disk the pre-existing
# `fs.statSync` throws ENOENT, the exception escapes tape and kills node. That
# abort is the graded signal. Aborting the script on it under `set -e` would cut
# the stage off before the log reached stdout, leaving parse_log with nothing and
# tripping Report.check() rule 1 on an instance that is in fact working.
#
# This does not weaken the failure signal -- the start-up assertion at the bottom
# is what guarantees a stage cannot silently report 0/0/0.
set +e

# Load-bearing, not boilerplate: the whole fix patch is an `install` lifecycle
# script, so the fix stage only differs from the baseline once npm has run it.
# In the run/test stages the base package.json has no install script and no new
# dependencies, so this is a sub-second no-op.
{npm_install} > /tmp/npm-install.out 2>&1
NPM_RC=$?

# tape's TAP output never says which file it is running, and this repo's own
# `npm test` globs `test/*.js`. Emit the path ourselves so parse_log can key each
# test as `<file> > <test name>` -- the shape report.py's file matcher expects.
: > /tmp/tap.out
for _f in test/*.js; do
    echo "##### TAPFILE $_f" >> /tmp/tap.out
    node_modules/.bin/tape "$_f" >> /tmp/tap.out 2>&1
done
set -e

# parse_log reads stdout, so the captured suite output has to land there.
cat /tmp/tap.out

if [ "$NPM_RC" -ne 0 ]; then
    echo "NOTE: npm install exited $NPM_RC; tail of its output follows"
    tail -20 /tmp/npm-install.out
fi

# Start-up guarantee. A stage where node never launched -- a missing tape binary,
# a wiped node_modules, a glob that matched nothing -- writes no TAP header at
# all. Failing here surfaces that as a broken stage instead of an empty
# TestResult that looks like a legitimate 0/0/0.
grep -q "^TAP version" /tmp/tap.out
"""


class FfmpegStaticImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 14 and nothing else.

    Tagged per PR rather than with a shared ``:base``: one shared tag would be
    rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into the standard clone +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args. Nothing that
    matters is emitted after the clone line for exactly that reason -- the
    enhancer appends ``CMD`` there, and any later instruction would be stranded
    below it.
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
        # engines.node is ">=10"; 14 is the LTS contemporary with this PR.
        # bullseye, not alpine: the downloaded ffmpeg is a glibc static build,
        # and the -bullseye variants derive from buildpack-deps:*-scm, so git
        # and ca-certificates already ship -- verified git 2.30.2 in the image,
        # which is why this Dockerfile installs no apt packages at all.
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

        # Deliberately minimal. node:14-bullseye already ships git (2.30.2) and
        # ca-certificates -- verified in the image -- so there is no apt layer to
        # add, and DEBIAN_FRONTEND / LANG / TZ are supplied by
        # DockerfileEnhancer._ENV_BLOCK. Re-declaring them here would only create
        # two places to keep in sync.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class FfmpegStaticImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and warms node_modules."""

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
        return FfmpegStaticImageBase(self.pr, self._config)

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

# `|| true` per house rule: a transient registry failure here must not abort the
# image build, and the graded runs surface any real breakage as test results.
#
# This warms node_modules with the base devDependencies (tape, any-shell-escape)
# only. The fix patch's two runtime deps are deliberately NOT pre-installed:
# `--no-save` still lets npm 6 rewrite package-lock.json, which the fix patch
# also patches, and a dirtied lock file would make the fix stage's `git apply`
# fail outright. Measured 2026-08-24: installing them costs the fix stage under
# a second, while the 75 MB ffmpeg download it triggers cannot be cached at all,
# so there is nothing worth pre-warming here.
#
# Verified 2026-08-24 that this leaves `git status --porcelain` empty and the
# fix patch still applying cleanly afterwards.
{npm_install} || true

""".format(pr=self.pr, npm_install=_NPM_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
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
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
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
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
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

# Emitted by the run scripts, once per file, because tape's TAP output does not
# identify the file it is executing.
_TAPFILE = re.compile(r"^#####\s+TAPFILE\s+(\S+)\s*$")
# `TAP version 13` -- one per file, and the start-up marker the scripts assert on.
_TAP_VERSION = re.compile(r"^TAP version\s+\d+\s*$")
# `1..3` -- the plan. Its presence is what distinguishes a suite that finished
# from one that died mid-test.
_PLAN = re.compile(r"^\d+\.\.\d+\s*$")
# `ok 1 should be truthy` / `not ok 2 ffmpeg not executable`
_ASSERT = re.compile(r"^(not )?ok\b\s*(\d+)?\s*(?:-\s+)?(.*)$")
# `# should find ffmpeg` (a tape test header) and also `# tests 3` / `# pass  3`
# / `# ok` / `# fail  1` (tape's trailing summary). Both shapes are comments, so
# the summary keywords have to be filtered out or the summary would open a
# phantom test.
_COMMENT = re.compile(r"^#\s*(.*?)\s*$")
_SUMMARY_BODY = re.compile(r"^(?:tests|pass|fail|ok|not ok|skip|todo)\b", re.I)
# tape marks a directive on the assertion line itself: `ok 3 foo # SKIP reason`.
_DIRECTIVE = re.compile(r"#\s*(SKIP|TODO)\b", re.I)


def parse_tape_tap_log(log: str) -> TestResult:
    """Report tape's TAP output at the *test* level, keyed ``<file> > <name>``.

    Assertion-level names are unusable in this repo -- both ``t.ok`` calls emit
    the default ``should be truthy`` -- and, more importantly, the baseline
    stages die from an uncaught ENOENT that escapes tape, so the stream stops
    after ``ok 1`` with no ``not ok``, no plan and no summary. A stream that ends
    before its plan is therefore treated as a failure of whichever test was still
    open, which is both the standard TAP-consumer reading and the honest one.
    See the module docstring.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # tape does not colourise by default, but npm and CI wrappers can, and every
    # pattern below is anchored -- so stripping is unconditional rather than
    # conditional on what happened to be in the log.
    clean = ANSI_ESCAPE.sub("", log)

    state = {
        "file": None,       # current `##### TAPFILE` path
        "test": None,       # name of the tape test currently receiving asserts
        "failed": False,    # a `not ok` was seen inside it
        "asserts": 0,
        "skips": 0,
        "plan": False,      # this file's stream reached its `1..N`
        "any_test": False,  # this file declared at least one test
    }

    def close_test(aborted: bool) -> None:
        name = state["test"]
        if name is None:
            return
        if aborted or state["failed"]:
            failed_tests.add(name)
        elif state["asserts"] and state["asserts"] == state["skips"]:
            skipped_tests.add(name)
        else:
            # No assertions and a clean stream is a vacuous pass, which is what
            # tape itself reports for a test that only calls t.end().
            passed_tests.add(name)
        state["test"] = None
        state["failed"] = False
        state["asserts"] = 0
        state["skips"] = 0

    def end_file() -> None:
        # Reaching the plan means the suite finished; anything still open when it
        # did not is a test the process died inside of.
        close_test(aborted=not state["plan"])
        if state["file"] is not None and not state["any_test"]:
            # node blew up before tape declared anything -- a module-load error,
            # a missing runner. Record it rather than silently contributing zero.
            failed_tests.add(f"{state['file']} > <no tests ran>")
        state["file"] = None
        state["plan"] = False
        state["any_test"] = False

    for raw in clean.splitlines():
        line = raw.rstrip()

        m = _TAPFILE.match(line)
        if m:
            end_file()
            state["file"] = m.group(1)
            continue

        if _TAP_VERSION.match(line):
            continue

        if _PLAN.match(line):
            state["plan"] = True
            close_test(aborted=False)
            continue

        m = _COMMENT.match(line)
        if m:
            body = m.group(1)
            if not body or _SUMMARY_BODY.match(body):
                continue
            # A new tape test header closes the previous one, which by definition
            # completed -- tape only moves on once t.end() has fired.
            close_test(aborted=False)
            prefix = f"{state['file']} > " if state["file"] else ""
            state["test"] = f"{prefix}{body}"
            state["any_test"] = True
            continue

        m = _ASSERT.match(line)
        if m and state["test"] is not None:
            desc = m.group(3) or ""
            state["asserts"] += 1
            if _DIRECTIVE.search(desc):
                state["skips"] += 1
            elif m.group(1):
                state["failed"] = True
            continue

        # Everything else -- the uncaught stack trace, npm noise -- is ignored on
        # purpose. Crash output is unindented in places, so patterns above are
        # anchored and never match it.

    end_file()

    # TestResult.__post_init__ rejects overlapping sets. A name that reached more
    # than one bucket across files is reported by its worst outcome.
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


@Instance.register("eugeneware", "ffmpeg-static")
class FfmpegStatic(Instance):
    """Instance handler for eugeneware/ffmpeg-static.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on. The repo name keeps its hyphen because the JSONL does.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return FfmpegStaticImageDefault(self.pr, self._config)

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
        return parse_tape_tap_log(log)
