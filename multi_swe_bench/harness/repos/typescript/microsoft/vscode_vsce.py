"""Repo config for microsoft/vscode-vsce (TypeScript / Mocha + ts-node).

Runner
------
``package.json`` sets ``"test": "mocha"`` and configures Mocha inline::

    "mocha": {
      "require": ["ts-node/register"],
      "spec": "src/test/**/*.ts"
    }

so the suite runs straight off the TypeScript sources through ``ts-node`` --
there is no ``tsc`` build step in front of the tests, and ``npm run build``
(which only exists to produce ``out/`` for publishing) is never needed here.

Test identity
-------------
Reported as ``<spec file>::<fullTitle>``::

    src/test/package.test.ts::toVsixManifest should detect short gitlab repositories

Two pieces, from two places. The scripts below call Mocha with
``--reporter json`` rather than the default spec reporter, because the JSON
reporter emits ``fullTitle`` -- the ``describe`` chain plus the leaf name -- as
one string, whereas the spec reporter prints only the leaf under an indented
header and leaves the nesting to be reconstructed from indentation. The file
half cannot come from the reporter at all: Mocha 7 reports no source file per
test, so ``_RUN_MOCHA`` runs one spec file per invocation behind a marker line
and ``parse_log`` stitches the path back on.

Toolchain pin
-------------
``node:14-bullseye``:

* ``ts-node@10`` requires Node >= 12, so the ``10.x`` in ``azure-pipelines.yml``
  is stale for this commit -- the pinned ``ts-node`` in ``package-lock.json``
  will not run on it.
* ``package-lock.json`` is ``lockfileVersion: 1``, i.e. npm 6 -- exactly what
  ships with Node 14, so ``npm ci`` reproduces the locked tree.
* The ``node:*-bullseye`` variants derive from ``buildpack-deps:*-scm`` and
  already carry git, so no apt layer is needed (which also avoids the
  archived-Debian-repo fixups in ``Image._get_apt_update_command``).

The gold fix patch adds ``hosted-git-info`` (and its ``@types``) to
``package.json``, but ``node_modules`` is baked at image-build time from the
*base* commit's lockfile, and the run stages have no network. Both packages are
therefore installed alongside the locked tree in ``prepare.sh``; without them
``ts-node`` fails to resolve ``import GitHost = require('hosted-git-info')`` and
the fix stage reports zero tests instead of a clean pass. They are installed
with ``--no-save --no-package-lock`` so neither ``package.json`` nor the
lockfile is dirtied -- ``check_git_changes.sh`` runs immediately afterwards and
would otherwise fail the build.

Installs run with ``--ignore-scripts``: the only lifecycle hook in the tree is
``husky``'s, which just wires up git hooks for local development and has no
bearing on the suite.
"""

import json
import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# `diff --git a/<old> b/<new>` -- group(2) is the post-image path, present even
# for created files (where the `--- a/` side is `/dev/null`).
_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# The marker `_RUN_MOCHA` prints ahead of each per-file Mocha invocation, which
# is what lets `parse_log` attribute a test to its spec file. Deliberately not
# anchored to the start of a line: Mocha's JSON reporter emits no trailing
# newline, so a marker can end up welded to the previous object's closing brace
# (`}##### MSWEB-SPEC-FILE: ...`). `_RUN_MOCHA` prints a leading newline to
# avoid that, but an unanchored pattern keeps a log captured by some other
# route parseable too.
_SPEC_FILE_MARKER = re.compile(r"#####\s+MSWEB-SPEC-FILE:\s*(\S+)")


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every file the gold test patch touches.

    Reward-hacking guard, defence in depth for
    ``test_result.fix_patch_tampers_with_tests``: that pre-run check reads
    ``get_modified_files``, which drops entries whose ``---`` side is
    ``/dev/null`` and is therefore blind to gold tests the test patch
    *creates*. Both halves are collected here.

    Expressing the guard as an exclusion rather than a post-hoc revert is what
    lets ``fix-run.sh`` keep the canonical stage order (gold tests first, fix
    patch second): the agent's edits to a gold test file are simply never laid
    down, so there is nothing to undo and no need to re-apply the test patch
    afterwards.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


# Mocha is invoked through the local binary; `npx` is only a fallback because
# npm 6's npx would try to fetch mocha from the registry, and the run stage has
# no network.
#
# Each spec file is run in its own Mocha process behind a marker line. Mocha 7's
# JSON reporter reports only `title` / `fullTitle` -- it does not carry the
# source file of a test (`file` was added to that reporter later) -- so running
# the whole glob at once makes it impossible to say which file a test came from.
# Driving one file per invocation and printing the path first lets `parse_log`
# stitch the two halves into a `<file>::<test>` identity. The `find` mirrors the
# `spec: src/test/**/*.ts` glob configured in package.json.
#
# `|| true` on the Mocha call: without it `set -e` would abort the loop at the
# first spec file with a failing test, and the TEST stage -- where a gold test
# is *expected* to fail -- would silently lose every later file. A runner that
# fails to launch is still visible: ts-node's compile diagnostics go to stderr,
# 2>&1 keeps them in the same log, and the absence of any JSON leaves a 0/0/0
# TestResult that `generate_report` rejects rather than scoring.
#
# `--no-package --no-config` is what makes the per-file split real. A positional
# spec argument is *appended* to the `spec` glob configured in package.json
# rather than replacing it, so without these flags every invocation re-runs the
# entire suite and each test is attributed to whichever file happened to be
# named that round. Suppressing both config sources means `require:
# ts-node/register` has to be passed explicitly -- it lived in the same
# package.json block.
#
# The marker is printed with a leading newline because Mocha's JSON reporter
# does not terminate its output with one; without it the next marker lands on
# the same line as the previous object's closing brace.
#
# `--timeout 60000` replaces Mocha's 2s default, which is not a margin this
# suite can rely on. The seven `version` tests shell out to git and npm: ~230-390ms
# on native amd64, but 3951-4467ms measured under QEMU arm64 emulation, i.e. all
# seven fail the default timeout on an emulated or otherwise slow host. That
# yields a stage with failures the code did not cause -- at the RUN stage it
# means a non-clean baseline and a rejected instance, which reads as a broken
# config rather than a slow CPU. The raised limit costs nothing where the suite
# is fast, since these tests never approach it natively.
_RUN_MOCHA = """MOCHA=./node_modules/.bin/mocha
if [ ! -x "$MOCHA" ]; then MOCHA="npx mocha"; fi
find src/test -name '*.test.ts' | sort | while IFS= read -r spec; do
    printf '\\n##### MSWEB-SPEC-FILE: %s\\n' "$spec"
    timeout -k 60 1800 $MOCHA --no-package --no-config \\
        --require ts-node/register --reporter json --timeout 60000 "$spec" 2>&1 || true
done"""

# Lockfiles are excluded so a patch that churns them cannot invalidate the
# node_modules tree baked into the image at build time.
_APPLY_EXCLUDES = "--exclude package-lock.json --exclude yarn.lock"


class VscodeVsceImageBase(Image):
    """Shared ``:base`` image -- clones the repo on top of Node 14.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` line below into the standard
    clone + ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` sequence
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

    def dependency(self) -> Union[str, "Image"]:
        return "node:14-bullseye"

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


class VscodeVsceImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the locked dependency tree."""

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
        return VscodeVsceImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gold_excludes = _gold_test_exclude_flags(self.pr.test_patch)

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
set -uxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# npm ci honours lockfileVersion 1 under the npm 6 that ships with Node 14.
# The fallback keeps the image buildable if a tarball has gone missing from
# the registry since 2021.
npm ci --ignore-scripts --no-audit --no-fund \\
    || npm install --ignore-scripts --no-audit --no-fund \\
    || true

# The gold fix patch imports `hosted-git-info`, which the base commit's
# lockfile does not carry. --no-save --no-package-lock keeps the git tree
# clean so the check below still passes; the versions match the ranges the
# fix patch adds to package.json.
npm install --no-save --no-package-lock --ignore-scripts --no-audit --no-fund \\
    hosted-git-info@^4.0.2 @types/hosted-git-info@^3.0.2 \\
    || true

# Last resort: the suite itself needs only the mocha/ts-node toolchain.
if [ ! -x node_modules/.bin/mocha ]; then
    npm install --no-save --no-package-lock --ignore-scripts \\
        mocha@7 ts-node@10 typescript@4.3 || true
fi

# node_modules is gitignored, so the tree must still be pristine here: a dirty
# tree at this point means an install wrote into a tracked path, and every
# later `git apply` would then be laid on top of unexplained edits.
bash /home/check_git_changes.sh

exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

{run_mocha}
""".format(pr=self.pr, run_mocha=_RUN_MOCHA),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn {excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_mocha}
""".format(pr=self.pr, excludes=_APPLY_EXCLUDES, run_mocha=_RUN_MOCHA),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --whitespace=nowarn {excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so it is applied with every
# gold test file excluded -- a fix patch that edits the tests grading it cannot
# take effect. The gold fix patch touches none of those paths, so the
# exclusions are a no-op for dataset generation.
if ! git apply --whitespace=nowarn {excludes} {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_mocha}
""".format(
                    pr=self.pr,
                    excludes=_APPLY_EXCLUDES,
                    gold_excludes=gold_excludes,
                    run_mocha=_RUN_MOCHA,
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


def _json_objects(text: str) -> list[str]:
    """Every balanced top-level ``{...}`` region of ``text``.

    Handing the whole log to ``json.loads`` is not an option: anything the suite
    writes to stdout lands in the same stream, as do the marker lines, so the
    log is JSON *embedded in* noise rather than JSON. Scanning for balanced
    braces tolerates that noise on either side.
    """
    blocks: list[str] = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start : i + 1])
                start = None
    return blocks


def parse_mocha_json(test_log: str) -> TestResult:
    """Parse the per-spec-file ``mocha --reporter json`` output of ``_RUN_MOCHA``.

    Identity is ``<spec file>::<fullTitle>``, e.g.::

        src/test/package.test.ts::toVsixManifest should detect short gitlab repositories

    ``fullTitle`` supplies the ``describe`` chain and the leaf name; the file
    comes from the ``##### MSWEB-SPEC-FILE:`` marker ``_RUN_MOCHA`` prints ahead
    of each invocation, since Mocha 7's JSON reporter does not report it. The
    log is split on those markers and each JSON object is attributed to the
    marker preceding it.

    Tests found before any marker keep a bare ``fullTitle``. That only happens
    for a log produced without the marker loop, and an unprefixed name is worth
    more than a name attributed to the wrong file.

    A log with no parseable object at all -- a ts-node compile error, say --
    yields an empty 0/0/0 result, which ``generate_report`` rejects rather than
    silently scoring as "nothing regressed".
    """
    clean = ANSI_ESCAPE.sub("", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    # (spec file or None, text) for each marker-delimited region, in order.
    segments: list[tuple[Optional[str], str]] = []
    cursor = 0
    current: Optional[str] = None
    for match in _SPEC_FILE_MARKER.finditer(clean):
        segments.append((current, clean[cursor : match.start()]))
        current = match.group(1).strip().lstrip("./")
        cursor = match.end()
    segments.append((current, clean[cursor:]))

    for spec_file, segment in segments:
        for block in _json_objects(segment):
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue

            for bucket, sink in (
                ("passes", passed),
                ("failures", failed),
                ("pending", skipped),
            ):
                for test in data.get(bucket, []) or []:
                    if not isinstance(test, dict):
                        continue
                    title = test.get("fullTitle") or test.get("title") or ""
                    if not title:
                        continue
                    sink.add(f"{spec_file}::{title}" if spec_file else title)

    # Mocha lists a retried test in both buckets; a failure is the stronger
    # signal, so it wins.
    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("microsoft", "vscode-vsce")
class VscodeVsce(Instance):
    """Instance handler for microsoft/vscode-vsce.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create``
    resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VscodeVsceImageDefault(self.pr, self._config)

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
        return parse_mocha_json(test_log)
