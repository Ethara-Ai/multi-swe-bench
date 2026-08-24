"""danger/danger-js harness config -- TypeScript, Yarn 1, Jest + ts-jest.

Repo
----
``danger-js`` is the Node implementation of Danger. It is a plain single-package
TypeScript repo: sources under ``source/``, tests co-located in ``_tests/``
directories as ``*.test.ts``, driven by Jest with the ``ts-jest`` preset
(``package.json`` -> ``"jest": {"preset": "ts-jest", ...}``, ``"test": "jest"``).

Toolchain pin
-------------
``node:14-bullseye``. The repo's own CI (``.github/workflows/CI.yml`` at the base
commit) pins ``node-version: '14'``, and the dependency set is of that era --
Jest 24, ts-jest 24, TypeScript 3.9. ``-bullseye`` rather than the default
``-buster`` tag: Buster is EOL and its apt repos 404, which breaks the ``git``
install the base layer needs.

Where the tests run from
------------------------
No build step. ``ts-jest`` compiles the TypeScript in-process, so the graded
stages run Jest directly against ``source/`` -- ``yarn build`` (which also runs
``madge --circular``) is CI hygiene, not a prerequisite for the suite.

Install placement
-----------------
The base install happens at image-build time in ``prepare.sh`` -- frozen against
the committed lockfile, since at the base commit that lockfile is authoritative
and yarn 1 would otherwise rewrite the tracked ``yarn.lock`` and leave the tree
dirty. The RUN and TEST stages reuse that install untouched: the gold test patch
edits only ``*.test.ts`` files plus one test helper, so neither stage's
dependency set differs from the base commit's.

The FIX stage is the exception and must install again. This PR *is* a dependency
upgrade -- ``@octokit/rest`` ^16.43.1 -> ^18.12.0, ``typescript`` ^3.9.7 ->
^4.5.5, plus ``@octokit/openapi-types`` -- so ``package.json`` and ``yarn.lock``
are part of the fix patch. Running the suite against the base ``node_modules``
would compile the patched sources (which call ``repos.getContent``, the v18 name)
against the v16 typings and fail for reasons unrelated to the change under test.
``yarn install`` therefore runs after the patches are applied, which means that
stage needs network. Plain ``yarn install`` rather than ``--frozen-lockfile``:
an agent's fix patch will usually touch ``package.json`` without regenerating the
lockfile, and a frozen install would abort on the mismatch rather than resolve it.

Test identity
-------------
``<suite path> > <test name>``, e.g.::

    source/platforms/github/_tests/_github_utils.test.ts > getContent > should call the API

Jest's ``--verbose`` reporter prints a ``PASS``/``FAIL`` header per suite followed
by the indented tick/cross/circle lines for that suite, so the header is tracked
as state and prefixed onto each test name. The prefix is not cosmetic: danger-js
has genuinely duplicated test names across platform suites (the BitBucket Cloud
and Server API suites mirror each other), and a bare-name identity would let a
pass in one suite mask a failure in the other.

A suite that dies before running -- a ts-jest compile error, a module that throws
at import -- prints its ``FAIL`` header and no test lines at all. Those suites are
recorded under their path alone so the failure is visible; without that fallback
a broken patch could report an empty, all-green result.
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

# `--ci` keeps Jest from writing new snapshots (an unwritten snapshot must fail,
# not silently become the expectation). `--maxWorkers=2` bounds memory: the
# default of `cpus - 1` ts-jest workers each hold their own TypeScript program,
# which is what OOMs this suite on a fat build host.
JEST_CMD = "yarn jest --ci --verbose --maxWorkers=2 2>&1"


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every path the gold test patch touches.

    Defence in depth for the harness's own
    ``test_result.fix_patch_tampers_with_tests``. At evaluation time the fix
    patch is the *agent's*, and a patch that edits the very tests grading it must
    not take effect. Both sides of each ``diff --git`` header are collected as
    well as ``get_modified_files``, because the latter drops entries whose
    ``---`` side is ``/dev/null`` and so cannot see test files a patch creates.

    The gold fix patch touches none of these paths -- it is confined to
    ``package.json``, ``yarn.lock``, ``CHANGELOG.md`` and non-test sources -- so
    the exclusions are a no-op for dataset generation.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


class DangerJsImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 14 plus a clone of the repo.

    Tagged per PR rather than with a bare ``:base``, because this layer is *not*
    PR-agnostic even though its Dockerfile text is. ``build_image`` passes
    ``BASE_COMMIT = pr.base.sha`` as a build arg to any image whose
    ``dependency()`` is a string (build_dataset.py), and the enhancer's hardening
    block then checks that commit out and deletes every other ref, remote and
    unreachable object. A shared ``:base`` tag would be rewritten by -- or worse,
    reused by, via the ``image_exists`` skip -- every other PR of this repo, and
    the next PR's ``git checkout <sha>`` would fail on a pruned object.
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
    git \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DangerJsImageDefault(Image):
    """Per-PR ``:pr-<N>`` image -- checkout of the base commit, deps installed.

    Pipeline::

        git checkout <base_sha>
        yarn install
        # graded stage applies its patches, then:
        yarn jest --ci --verbose --maxWorkers=2
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
        return DangerJsImageBase(self.pr, self.config)

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
# `-e` is load-bearing: without it the check_git_changes.sh calls below are
# advisory -- a dirty tree complains, the script runs on, and the image builds
# green. An assertion that cannot fail the build is not an assertion. The one
# command allowed to fail carries its own `|| true`, and is checked by artefact
# instead.
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# --frozen-lockfile: yarn 1 rewrites the TRACKED yarn.lock whenever it has
# drifted from package.json, which would trip the pristine-tree check below and
# fail the build for a reason that has nothing to do with this config. At the
# base commit the committed lockfile is authoritative, so freezing it is also
# the more faithful install. (fix-run.sh deliberately does NOT freeze -- see the
# comment there.)
#
# --network-timeout: the registry fetch for this dependency set (Jest 24,
# Babel 7, the full @octokit tree) regularly exceeds yarn's 30s default on a
# cold cache, and the default failure mode is a hard abort mid-install.
#
# `|| true`, then assert on the artefact. A bare non-zero exit here is usually a
# transient registry hiccup or an arm64 optional-dependency postinstall that the
# suite never touches; failing the whole image build on it is wrong. What must
# not survive is a *half*-installed tree, so the test runner's own binary is
# checked for instead -- that names the failure precisely.
yarn install --frozen-lockfile --network-timeout 600000 || true
if [ ! -x node_modules/.bin/jest ]; then
    echo "yarn install did not produce a usable jest binary" >&2
    exit 1
fi

# Defensive: a no-op under --frozen-lockfile, but it keeps the check below
# honest if the freeze is ever relaxed.
git checkout -- yarn.lock

# node_modules, .jest/ and test-results.json are all gitignored, so a clean
# install must leave the tree pristine.
# Deliberately last: no `exit 0` follows, so the script's status IS this check's.
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

# Baseline: no patches. Dependencies are exactly the ones prepare.sh installed.
{jest}
""".format(repo=self.pr.repo, jest=JEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# No reinstall: the gold test patch touches only *.test.ts and one test helper,
# so the dependency set is still the base commit's.
{jest}
""".format(repo=self.pr.repo, jest=JEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# Every gold test path is excluded, so a fix patch that edits the tests grading
# it cannot take effect. The gold fix patch touches none of them.
if ! git apply --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# This PR is a dependency upgrade: package.json and yarn.lock are part of the
# fix patch, and the patched sources call the @octokit/rest v18 API. Installing
# again is what makes those typings present. Plain `yarn install`, not
# --frozen-lockfile: an agent patch that edits package.json without regenerating
# the lockfile should resolve, not abort.
yarn install --network-timeout 600000

{jest}
""".format(
                    repo=self.pr.repo,
                    gold_excludes=gold_excludes,
                    jest=JEST_CMD,
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


def parse_jest_verbose(test_log: str) -> TestResult:
    """Parse ``jest --verbose`` output into pass/fail/skip sets.

    The reporter prints, per suite::

        PASS source/platforms/github/_tests/_github_utils.test.ts
          getContent
            v should call the API (4 ms)
            x should reject unknown paths (2 ms)
            o skipped never mind

    (with U+2713 / U+2715 / U+25CB in place of ``v``/``x``/``o``).

    The ``PASS``/``FAIL`` header is the only place the suite path appears, so it
    is carried as state and prefixed onto each test name. The enclosing
    ``describe`` blocks are carried too, reconstructed from indentation, giving
    ``<suite path> > <describe> > ... > <test name>``.

    The describe chain is not cosmetic -- it is what makes the identity unique.
    Jest indents two spaces per nesting level, and the leaf name alone repeats
    constantly inside a single file: ``_pull_request_parser.test.ts`` has four
    separate ``handles PRs`` tests under ``GitLab > .com``, ``GitLab > CE/EE``
    and friends. Keying on suite+leaf collapsed 665 result lines into 608
    identities on a real run (57 lost). Collapsed entries are not merely
    imprecise: ``passed -= failed`` means one failing twin drags its passing
    namesakes into the failed set, so a single collision can invent an f2p or
    destroy a p2p.

    Suites that produce no test lines at all (ts-jest compile error, a module
    that throws on import) are recorded under their path alone; otherwise a
    patch that breaks compilation would report an empty, all-green result. That
    fallback is load-bearing for this PR: at the TEST stage three suites fail
    ``ts-jest`` type-checking outright (the renamed ``repos.getContent`` does not
    exist in the @octokit/rest v16 typings), so the failure has no test lines to
    attach to.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    suite_re = re.compile(r"^(PASS|FAIL)\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$")
    pass_re = re.compile(r"^[✓✔]\s+(.+)$")
    fail_re = re.compile(r"^[✕✗✘×]\s+(.+)$")
    skip_re = re.compile(r"^[○◌⊝]\s+(.+)$")
    # "(123 ms)" / "(1.5 s)" -- timing is per-run noise and must not enter the
    # identity, or no test would ever match itself across two stages.
    timing_re = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")

    state = {"suite": "", "status": "", "had_tests": False, "in_console": False}
    empty_failed_suites: set[str] = set()
    # (indent, label) for each open describe, outermost first.
    describes: list[tuple[int, str]] = []

    def close_suite() -> None:
        if state["suite"] and state["status"] == "FAIL" and not state["had_tests"]:
            empty_failed_suites.add(state["suite"])

    def identity(name: str, indent: int) -> str:
        """``<suite> > <describe chain> > <test>``, chain scoped by indentation.

        Describes indented at or deeper than the test line cannot enclose it, so
        they are dropped first -- that is what closes a sibling block when the
        next one starts without any explicit end marker.
        """
        while describes and describes[-1][0] >= indent:
            describes.pop()
        parts = [label for _, label in describes]
        parts.append(timing_re.sub("", name.strip()))
        suite = state["suite"]
        return f"{suite} > {' > '.join(parts)}" if suite else " > ".join(parts)

    for raw in test_log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).rstrip()
        stripped = line.strip()
        if not stripped:
            # A blank line ends a `console.*` block; jest prints those between
            # suites, not inside one.
            state["in_console"] = False
            continue
        indent = len(line) - len(line.lstrip())

        m = suite_re.match(stripped)
        if m:
            close_suite()
            state["status"] = m.group(1)
            state["suite"] = m.group(2).strip()
            state["had_tests"] = False
            state["in_console"] = False
            describes.clear()
            continue

        m = pass_re.match(stripped)
        if m:
            state["had_tests"] = True
            state["in_console"] = False
            passed_tests.add(identity(m.group(1), indent))
            continue

        m = fail_re.match(stripped)
        if m:
            state["had_tests"] = True
            state["in_console"] = False
            failed_tests.add(identity(m.group(1), indent))
            continue

        m = skip_re.match(stripped)
        if m:
            state["had_tests"] = True
            state["in_console"] = False
            skipped_tests.add(identity(m.group(1), indent))
            continue

        # Anything else that is indented under a suite header is a describe
        # label -- except captured console output and failure-detail prose,
        # which are filtered here. Mis-pushing one of those would corrupt every
        # identity below it, so the filter is deliberately conservative.
        if stripped.startswith("console."):
            state["in_console"] = True
            continue
        if state["in_console"] or not state["suite"] or indent < 2:
            continue
        if stripped.startswith(("●", "at ", "|", ">", "+", "-")):
            continue
        while describes and describes[-1][0] >= indent:
            describes.pop()
        describes.append((indent, stripped))

    close_suite()
    failed_tests |= empty_failed_suites

    # A name reported both ways across retries resolves to failed: a test that
    # failed once is not passing.
    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("danger", "danger-js")
class DangerJs(Instance):
    """Instance handler for danger/danger-js.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DangerJsImageDefault(self.pr, self._config)

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
        return parse_jest_verbose(test_log)
