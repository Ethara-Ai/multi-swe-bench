"""Repo config for microsoft/vscode-languageserver-node (TypeScript / Mocha in Electron).

Runner
------
The gold test patch for PR #1158 adds one ``test()`` to
``client-node-tests/src/integration.test.ts``. That file is not an ordinary node
suite -- ``client-node-tests`` is a **VS Code extension**
(``engines.vscode: ^1.67.0``) and its ``npm test`` is::

    node ../build/bin/symlink-tests.js && node lib/runTests.js

``runTests.js`` calls ``runTests()`` from ``vscode-test``, which downloads a VS
Code build, launches it under Electron, loads the extension, and runs Mocha
*inside* the editor process (``client-node-tests/src/index.ts``:
``new Mocha({ ui: 'tdd', color: true })``). There is no way to exercise the
gold test outside Electron: the suite imports ``vscode`` and drives real
language-client providers.

Everything unusual below follows from that one fact.

Environment
-----------
``build/azure-pipelines/linux/build.yml`` is the upstream recipe and is
reproduced here rather than invented:

* apt: ``libxkbfile-dev pkg-config libsecret-1-dev libxss1 dbus xvfb libgtk-3-0``.
  The Electron runtime libraries (``libnss3``, ``libgbm1``, ``libasound2``,
  the atk/cups/drm/xkbcommon/x11 set) are added on top -- CI gets them from the
  ``ubuntu-latest`` agent image, a ``node:*-bullseye`` container does not.
* Node **16.14.0**, the exact ``versionSpec`` in the pipeline. Not a guess:
  ``package-lock.json`` is ``lockfileVersion: 2`` and ``@types/node`` is pinned
  to ``16.11.43``.
* ``Xvfb :10 -ac -screen 0 1024x768x24`` and ``DISPLAY=:10``, matching
  ``build/azure-pipelines/linux/xvfb.init`` and the pipeline's
  ``DISPLAY=:10 npm run test``.

Non-root execution
------------------
Electron refuses to start as uid 0 unless launched with ``--no-sandbox``, and
``runTests.js`` hard-codes its ``launchArgs`` -- the flag cannot be injected
from outside without editing the repo under test, which would corrupt the
instance. The suite therefore runs as the ``node`` user that ships in the
official Node images (CI runs as a non-root agent user for the same reason).
``prepare.sh`` chowns the tree and the run scripts re-chown after ``git apply``,
since the harness applies patches as root. ``safe.directory`` is registered for
both users so git does not reject the now-mixed ownership.

Recompilation is load-bearing
-----------------------------
The fix patch edits ``client/src/common/client.ts``; the tests execute
``client/lib/**/*.js``. Applying the patch alone changes nothing observable, so
every run script runs ``npm run compile`` (``tsc -b``) *after* ``git apply`` and
before the suite. Without it the fix stage reports exactly the same results as
the test stage and the instance is dead on arrival. ``prepare.sh`` also
compiles, so the in-stage build is an incremental no-op when nothing changed.

Test identity
-------------
Mocha runs programmatically with no ``reporter`` option, so it uses ``spec``,
which prints only the leaf name indented beneath its suite header. Leaf names
alone are **not** unique in this repo -- ``Notebook document: open`` and
``Notebook document: change`` each appear in both ``suite('Full notebook
tests')`` and ``suite('Simple notebook tests')``. Keying on the leaf would
silently merge those four results into two and corrupt the f2p comparison, so
``parse_log`` rebuilds the suite path from output indentation and reports
``Suite > Nested suite > test name``.

The reporter is not negotiable: ``index.ts`` constructs ``new Mocha({...})``
directly, so there is no CLI ``--reporter json`` to switch to the way
``vscode_vsce.py`` does.

``color: true`` is set explicitly in ``index.ts``, so the log is always
ANSI-coloured even without a TTY -- stripping escapes first is mandatory, not
defensive. Trailing ``(123ms)`` durations are stripped as well: they vary run to
run, and an unstripped duration would make the same test a different name in
each of the three stages, which surfaces as the ``PASS -> NONE -> FAIL``
anomaly ``Report.check()`` rejects.

Known risk -- ``version: 'insiders'``
-------------------------------------
``runTests.js`` requests ``version: 'insiders'``, i.e. whatever VS Code Insiders
is on the day the image runs, against a tree pinned to ``@types/vscode 1.67.0``
(March 2023). The extension also declares ``enabledApiProposals:
["notebookContentProvider"]``, a proposal that has since been withdrawn
upstream. Neither can be pinned from here without editing the repo under test.
``prepare.sh`` warms ``client-node-tests/.vscode-test`` at image-build time so
all three stages reuse one downloaded build rather than racing three different
Insiders releases, but the download itself remains a live dependency on
Microsoft's update server. If the suite reports zero tests in every stage, check
the Electron launch output first -- that is the expected failure signature.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Common preamble shared by run.sh / test-run.sh / fix-run.sh.
#
# Identical in all three by construction: the only thing that may differ between
# the graded stages is which patch was applied before this block runs. Anything
# that varies the command itself would make a FAIL -> PASS transition
# attributable to the command rather than to the fix.
_TEST_BODY = """\
# The harness applies patches as root; the suite runs as `node` (Electron will
# not start as uid 0). Re-assert ownership after every git write.
chown -R node:node /home/{repo}

# `npm run symlink` before the build, exactly as build/azure-pipelines/linux/build.yml
# does (`npm install && npm run symlink` then `npm run compile`). This is not
# optional bookkeeping: client-node-tests resolves `vscode-languageclient` through
# its own node_modules, and unless that entry is a symlink to ../../client the
# suite type-checks against the *published* vscode-languageclient@8.1.0 instead of
# the patched local sources. Measured 2026-08-20: without this line the fix stage
# still failed with `Property 'sendRequest' does not exist on type 'Middleware'`
# even though the fix patch had applied cleanly and client/lib/common/client.d.ts
# had been rebuilt with GeneralMiddleware in it -- the compiler was simply reading
# a different package. The warm-up `npm test` in prepare.sh runs symlink-tests.js,
# which replaces those dev symlinks with real directories, so the link has to be
# re-established in every stage rather than relied on from image-build time.
runuser -u node -- env CI=true bash -lc 'cd /home/{repo} && npm run symlink'

# tsc -b, after the patches. The suite loads client/lib/**, not client/src/**,
# so an un-recompiled fix patch is invisible to every assertion.
#
# Deliberately non-fatal. In the test stage this build *legitimately* fails:
# the gold test calls `middleware.sendRequest`, which the fix patch introduces,
# so `tsc` reports TS2339 and exits 1. Aborting there left the stage with no
# results at all. Measured 2026-08-20: on failure `tsc -b` leaves the
# previously built output in place -- client-node-tests/lib/integration.test.js
# survives at its pre-patch content, 81712 bytes, with zero occurrences of
# "General middleware" -- so the suite still executes the 171 pre-existing
# tests while the new one stays absent. That absence is precisely the NONE the
# n2p classification needs, and the stage now reports 171 instead of 0/0/0.
#
# This does not weaken the failure signal. If the *fix* stage ever failed to
# compile, the new test would likewise never appear, no !PASS -> PASS
# transition would exist, and Report.check() rule 3 would reject the instance.
# The runner-start guarantee is enforced separately by the marker grep below.
set +e
runuser -u node -- env CI=true bash -lc 'cd /home/{repo} && npm run compile'
COMPILE_RC=$?
set -e
if [ "$COMPILE_RC" -ne 0 ]; then
    echo "NOTE: tsc -b exited ${{COMPILE_RC}}; running the suite against the previously built lib/"
fi

# Same display the upstream pipeline uses (build/azure-pipelines/linux/xvfb.init).
Xvfb :10 -ac -screen 0 1024x768x24 > /tmp/Xvfb.out 2>&1 &
for _ in $(seq 1 30); do
    if [ -e /tmp/.X11-unix/X10 ]; then break; fi
    sleep 1
done

# The suite is run in the background and reaped on its own completion marker
# rather than waited on, because `node lib/runTests.js` does not exit after the
# tests finish. Measured 2026-08-20: Mocha reported `171 passing` and
# `Extension host test runner exit code: 0`, and the process was still alive
# five minutes later with `chrome_crashpad` and `gsettings` reparented to PID 1
# holding the stdio pipes open -- ordinary headless-Electron behaviour in a
# container. `docker_util.run` reads the stage with
# `container.logs(stream=True, follow=True)` and no timeout on this path
# (`agent_timeout` guards a different one), so waiting on it hangs the stage
# forever rather than failing.
rm -f /tmp/suite.out
runuser -u node -- env CI=true DISPLAY=:10 ELECTRON_DISABLE_SECURITY_WARNINGS=1 \\
    bash -lc 'cd /home/{repo}/client-node-tests && npm test' > /tmp/suite.out 2>&1 &
SUITE_PID=$!

for _ in $(seq 1 900); do
    if grep -q "Extension host test runner exit code:" /tmp/suite.out 2>/dev/null; then break; fi
    if ! kill -0 "$SUITE_PID" 2>/dev/null; then break; fi
    sleep 1
done

# Let Mocha flush its remaining lines, then take the whole tree down.
sleep 5
pkill -9 -P "$SUITE_PID" 2>/dev/null || true
kill -9 "$SUITE_PID" 2>/dev/null || true
pkill -9 -f runTests.js 2>/dev/null || true
pkill -9 Xvfb 2>/dev/null || true

# parse_log reads stdout, so the captured suite output has to land there.
cat /tmp/suite.out

# The marker doubles as the start-up guarantee: a runner that never launched
# writes no marker, so this fails the stage instead of reporting a silent 0/0/0.
# A non-zero code here is the honest outcome for a stage with failing tests --
# the harness grades from the log text, not from this exit status.
grep -q "Extension host test runner exit code:" /tmp/suite.out
"""


class VscodeLanguageserverNodeImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 16.14.0 plus the Electron runtime.

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
        # 16.14.0 is the pipeline's exact `NodeTool@0` versionSpec. bullseye
        # rather than alpine: Electron links against glibc, and the -bullseye
        # variants derive from buildpack-deps:*-scm so git is already present.
        return "node:16.14.0-bullseye"

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

        # First block: the upstream pipeline's own apt line, verbatim.
        # Second block: what the ubuntu-latest agent already had and a
        # node:*-bullseye container does not -- the Electron/Chromium runtime.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    libxkbfile-dev pkg-config libsecret-1-dev libxss1 dbus xvfb libgtk-3-0 \\
    libnss3 libasound2 libgbm1 libatk-bridge2.0-0 libatk1.0-0 libcups2 \\
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \\
    libpango-1.0-0 libcairo2 libatspi2.0-0 ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class VscodeLanguageserverNodeImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT, installs the workspace, warms VS Code."""

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
        return VscodeLanguageserverNodeImageBase(self.pr, self._config)

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

# Ownership is split from here on: the harness drives git as root, the suite
# runs as `node`. Register both so git does not reject the mixed ownership.
chown -R node:node /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
runuser -u node -- git config --global --add safe.directory /home/{pr.repo}

# `|| true` throughout: a native-module compile failure on arm64 must not abort
# the image build. The graded runs surface any real breakage as test results.
#
# `npm install` triggers the root postinstall (build/bin/all.js install + the
# testbed install + symlink:testbed), which is what populates every workspace
# package -- there is no separate bootstrap step in this repo.
runuser -u node -- env CI=true bash -lc 'cd /home/{pr.repo} && npm install' || true
runuser -u node -- env CI=true bash -lc 'cd /home/{pr.repo} && npm run symlink' || true
runuser -u node -- env CI=true bash -lc 'cd /home/{pr.repo} && npm run compile' || true

# Warm client-node-tests/.vscode-test so all three graded stages reuse one
# downloaded editor build instead of each fetching its own Insiders release.
# .vscode-test is gitignored, so this cannot dirty the tree for the asserts
# above or for the `git apply` in the run scripts.
Xvfb :10 -ac -screen 0 1024x768x24 > /tmp/Xvfb.out 2>&1 &
for _ in $(seq 1 30); do
    if [ -e /tmp/.X11-unix/X10 ]; then break; fi
    sleep 1
done
# Bounded: on a cross-built arm64 image this runs Chromium under QEMU
# emulation, where a hang is as likely as a slow pass. The build must not be
# held open by it -- a cold .vscode-test only costs the first graded run a
# download, whereas an unbounded warm-up costs the whole build.
timeout 2700 runuser -u node -- env CI=true DISPLAY=:10 \\
    bash -lc 'cd /home/{pr.repo}/client-node-tests && npm test' || true

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
                + _TEST_BODY.format(repo=self.pr.repo),
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
                + _TEST_BODY.format(repo=self.pr.repo),
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
                + _TEST_BODY.format(repo=self.pr.repo),
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

# Mocha's spec reporter, which `client-node-tests/src/index.ts` selects by
# omission. Every marker is anchored on its own indentation, because indentation
# is the only thing carrying the suite nesting.
#
#       Client integration
#         ✓ InitializeResult
#         ✓ Goto Definition (52ms)
#         File Operations
#           ✓ Will Create Files
#         1) General middleware
#         - Skipped one
_PASS_LINE = re.compile(r"^(\s*)[✓✔]\s+(.+?)\s*$")
_FAIL_LINE = re.compile(r"^(\s*)\d+\)\s+(.+?)\s*$")
_SKIP_LINE = re.compile(r"^(\s*)-\s+(.+?)\s*$")
# `2 passing (4s)` / `1 failing` / `3 pending` -- the tree ends here and the
# failure epilogue begins. The epilogue repeats suite and test names in a
# different shape and would corrupt the indentation stack if parsed.
_SUMMARY_LINE = re.compile(r"^\s*\d+\s+(?:passing|failing|pending)\b")
# Trailing duration on slow tests. Varies per run, so it must not reach a name.
_DURATION = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")


def parse_mocha_spec_log(log: str) -> TestResult:
    """Rebuild ``Suite > Nested > test`` names from Mocha spec indentation.

    The leaf name alone is ambiguous in this repo -- see the module docstring --
    so a suite stack is maintained keyed on indentation width and each result is
    reported with its full path.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # ANSI first: index.ts sets `color: true`, so escapes are always present and
    # every pattern below would fail to anchor against them.
    clean = ANSI_ESCAPE.sub("", log)

    # (indent, suite name) for every suite currently open, outermost first.
    stack: list[tuple[int, str]] = []

    def path_for(indent: int, leaf: str) -> str:
        parts = [name for width, name in stack if width < indent]
        parts.append(leaf)
        return " > ".join(parts)

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
        # matters: VS Code and Electron write unindented diagnostics to the same
        # stream, and treating those as suites would poison every name below.
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


@Instance.register("microsoft", "vscode-languageserver-node")
class VscodeLanguageserverNode(Instance):
    """Instance handler for microsoft/vscode-languageserver-node.

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
        return VscodeLanguageserverNodeImageDefault(self.pr, self._config)

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
