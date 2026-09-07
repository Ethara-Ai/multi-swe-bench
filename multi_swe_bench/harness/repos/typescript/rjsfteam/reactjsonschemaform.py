"""Multi-SWE-bench config for rjsf-team/react-jsonschema-form.

Routing key
-----------
The raw dataset carries neither ``number_interval`` nor ``tag``, so
``Instance.create()`` resolves the key as ``org/repo`` ->
``rjsf-team/react-jsonschema-form``.  One key means one config, and therefore
ONE base Dockerfile (repo config created+modified == base Dockerfile).

Single shared base
------------------
All five PRs (1993, 3378, 3435, 3492, 3763) build from a single ``:base`` image
on ``node:18``.  This was verified empirically in a container before being
committed: the 2020-era tree (PR 1993, CI matrix Node 8-13) and the 2023-era
tree (PRs 3378+, CI matrix Node 14-18) both install and test on Node 18.

    !! SHARED-BASE BUILD ORDER !!
    ``build_dataset.py`` sets BASE_COMMIT from the first PR to build and skips
    any image whose tag already exists; the hardening block then deletes all
    history unreachable from that commit.  The five base commits are strictly
    linear (verified with ``git merge-base --is-ancestor``) in PR-number order,
    so the NEWEST commit is PR 3763 (2023-07-10).

        1. build PR 3763 alone:  --mode image --specifics "rjsf-team/react-jsonschema-form:pr-3763"
        2. then build the rest; the base already exists and is skipped

    NEVER pass ``--force_build true`` on this config: it re-pins the base to
    whichever PR runs first and every newer PR then fails with
    ``fatal: reference is not a tree``.

Hardening sentinel
------------------
``DockerfileEnhancer._inject_final_sanitize()`` force-appends the hardening
block to any base containing ``git clone``/``git fetch``/``git remote add``
unless its marker string already appears before ``CMD`` with no clone after it.
The lead's spec requires the base to stop at clone + WORKDIR + CMD and for the
hardening to live in the PR layer, so the base carries the marker inside a
comment.  This is a behavioural workaround and is fragile: if upstream changes
the marker string, injection silently resumes.  Re-check the rendered base
after any harness upgrade.

Era differences (same base image, different scripts)
----------------------------------------------------
* master era (PR <= 1993): webpack 4 needs ``--openssl-legacy-provider`` on
  Node 17+ (OpenSSL 3 dropped MD4); mocha needs ``--exit`` or the process hangs
  after the suite completes; mocha's TAP reporter is used because its
  ``fullTitle()`` yields qualified names.
* default era (PR > 1993): jest via ``dts``; ``--verbose`` is required or six of
  the eleven packages emit only file-level PASS lines and 450 tests are lost.

``@rjsf/playground`` is excluded from the build in both eras: it has no test
script, contributes nothing to f2p, and its build exhausts the Node heap.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Last PR of the 2020 "master" branch era.  PR 1993 targets master; every later
# PR in this dataset targets main.
MASTER_ERA_MAX_PR = 1993

# Marker that suppresses DockerfileEnhancer._inject_final_sanitize().
HARDENING_SENTINEL = (
    'test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
)

# --------------------------------------------------------------------------
# Graded commands.  Defined ONCE per era and interpolated into run.sh,
# test-run.sh and fix-run.sh so the graded invocation is byte-identical across
# the three stages by construction.
# --------------------------------------------------------------------------
BUILD_COMMAND = "npx lerna run --stream build --ignore @rjsf/playground"

# lerna 3.18.4 (master era) DROPS the "--" separator on `lerna run`: it emits
# `npm run test --reporter tap --exit`, npm swallows the flags as its own config
# and "tap" is appended to mocha as a positional glob - so neither the reporter
# nor --exit take effect (measured in-container).  `lerna exec` forwards the
# nested "--" correctly.  lerna 6 (default era) has no such problem.
# playground is ignored because it has no test script and `lerna exec` - unlike
# `lerna run` - would still try to run one there and fail the stage.
TEST_COMMAND_MASTER = (
    "npx lerna exec --stream --ignore @rjsf/playground -- "
    "npm run test -- --reporter tap --exit"
)
TEST_COMMAND_DEFAULT = "npx lerna run --concurrency 2 --stream test -- --verbose"


class reactjsonschemaformImageBase(Image):
    """Single shared base: clone only.  No checkout, no hardening, no installs.

    Dependency installation deliberately lives in prepare.sh so that each PR
    resolves its extras at its own base commit instead of all five inheriting
    one shared resolution.
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
        return "node:18"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        packages = [
            "ca-certificates",
            "curl",
            "git",
            "build-essential",
            "python3",
        ]
        packages_str = " \\\n    ".join(packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)

        # Must be the LAST RUN in this file.  "${REPO_URL}" (not a literal URL)
        # so _standardize_repo_fetch() leaves the line alone.
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append(f"WORKDIR /home/{self.pr.repo}")

        # Hardening sentinel - see module docstring.  Real hardening is applied
        # by the PR layer after prepare.sh.
        sections.append(
            "# Hardening is performed by the PR image after prepare.sh.\n"
            "# Marker below suppresses DockerfileEnhancer._inject_final_sanitize():\n"
            f"# {HARDENING_SENTINEL}"
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class reactjsonschemaformImagePR(Image):
    """Per-PR image: 7 COPY lines, one prepare.sh, then hardening."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def is_master_era(self) -> bool:
        return self.pr.number <= MASTER_ERA_MAX_PR

    @property
    def node_options(self) -> str:
        # webpack 4 (master era) needs the legacy OpenSSL provider on Node 17+.
        return (
            "export NODE_OPTIONS=--openssl-legacy-provider\n"
            if self.is_master_era
            else ""
        )

    @property
    def test_command(self) -> str:
        return TEST_COMMAND_MASTER if self.is_master_era else TEST_COMMAND_DEFAULT

    def dependency(self) -> Image | None:
        return reactjsonschemaformImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _stage_script(self, apply_patches: str) -> str:
        """run.sh / test-run.sh / fix-run.sh share one body.

        ``apply_patches`` is the only difference; the build and the graded test
        invocation are interpolated from the same constants in every stage.
        """
        return f"""#!/bin/bash
set -eo pipefail

export CI=true
{self.node_options}
cd /home/{self.pr.repo}
{apply_patches}
{BUILD_COMMAND}
{self.test_command}
"""

    def files(self) -> list[File]:
        install_chain = (
            "npm install || true\n"
            if self.is_master_era
            else "npm ci || npm install || true\n"
        )
        # Test-runner binary the graded TEST_COMMAND will actually invoke.
        runner = "_mocha" if self.is_master_era else "dts"

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
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# QC requires every install line to end in "|| true", which makes install
# failures silent.  The hard verification below is what turns a hollow image
# into a loud build failure - it must never carry "|| true".
{install_chain}
# lerna bootstrap / npm install rewrite the per-package package-lock.json files
# (11 tracked files in the 2023 tree, 8 in the 2020 tree).  Restore them so the
# tree is pristine: this is required both for the final check_git_changes assert
# and so `git apply` of test.patch/fix.patch lands on a clean tree.
# node_modules is gitignored and is unaffected by this restore (verified).
git checkout -- .

# ---- HARD VERIFICATION (no "|| true" anywhere in this block) ----
# Checks chosen against real installs of BOTH eras.  Note react/react-dom are
# NOT hoisted to the root in this lerna monorepo, and typescript is absent from
# the root in the 2020 tree - neither is a valid probe here.
test -d /home/{self.pr.repo}/node_modules
test -d /home/{self.pr.repo}/packages/core/node_modules
npx --no-install lerna --version
test -x /home/{self.pr.repo}/packages/core/node_modules/.bin/{runner}
# -----------------------------------------------------------------

bash /home/check_git_changes.sh
""",
            ),
            File(".", "run.sh", self._stage_script("")),
            File(
                ".",
                "test-run.sh",
                self._stage_script(
                    "git apply --whitespace=nowarn /home/test.patch"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                # test.patch BEFORE fix.patch, single invocation (QC 3B.3).
                self._stage_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copies = "\n".join(
            f"COPY {name} /home/"
            for name in (
                "fix.patch",
                "test.patch",
                "check_git_changes.sh",
                "prepare.sh",
                "run.sh",
                "test-run.sh",
                "fix-run.sh",
            )
        )

        # PR layers receive no build-args (build_dataset.py passes them only
        # when dependency() is a str), so BASE_COMMIT needs a literal default
        # for Image._HARDENING_BLOCK to resolve.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copies}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
"""


@Instance.register("rjsf-team", "react-jsonschema-form")
class reactjsonschemaform(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return reactjsonschemaformImagePR(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Parse both eras' output into qualified, collision-resistant names.

        master era - mocha TAP (``--reporter tap``).  ``fullTitle()`` already
        yields the full describe path, so ``ok N <name>`` is parsed directly.
        Verified in-container: 1285 pass / 1 fail, reconciling exactly with
        mocha's own ``# pass`` / ``# fail`` summary.  42 of the 1285 lines share
        a full title with another line (parametrised loops emitting identical
        titles in the same file); adding the file path does not separate them
        (measured with mocha's json reporter), so they collapse rather than
        being given fabricated indices.

        default era - jest via dts with ``--verbose``.  Names are qualified with
        package + test file + describe path, reconstructed from indentation.
        Parser state is kept PER PACKAGE because ``--concurrency 2`` interleaves
        two packages' lines.  Verified in-container: 3765 per-test lines,
        matching jest's own summary total.

        Counts use sets and then ``passed -= failed``, ``skipped -= failed``,
        ``passed -= skipped``, so a duplicated name with any failing instance is
        classified failed rather than inflating the pass count.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # ---- mocha TAP (master era) ----
        tap_ok = re.compile(r"^ok\s+\d+\s+(?:-\s+)?(.*?)\s*$")
        tap_not_ok = re.compile(r"^not ok\s+\d+\s+(?:-\s+)?(.*?)\s*$")
        tap_skip = re.compile(r"^ok\s+\d+\s+(?:-\s+)?(.*?)\s*#\s*SKIP.*$", re.I)

        # ---- jest verbose (default era) ----
        pkg_prefix = re.compile(r"^(@[\w./-]+):\s?(.*)$")
        jest_file = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+)")
        jest_test = re.compile(
            r"^(\s*)([✓✔✕✗✘○⚠])\s+"
            r"(.*?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )
        jest_head = re.compile(r"^(\s*)(\S.*?)\s*$")
        noise = (
            "npm",
            "lerna",
            "Tests:",
            "Test Suites:",
            "Snapshots:",
            "Time:",
            "Ran all",
            "> ",
            "console.",
            "PASS",
            "FAIL",
            "at ",
        )

        # Per-package parser state (concurrency 2 interleaves packages).
        stacks: dict[str, dict[int, str]] = {}
        cur_file: dict[str, str] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            if not line.strip():
                continue

            body = line
            pkg = ""
            m_pkg = pkg_prefix.match(line)
            if m_pkg:
                pkg, body = m_pkg.group(1), m_pkg.group(2)

            stripped = body.strip()

            # --- TAP lines (master era) -------------------------------------
            m = tap_skip.match(stripped)
            if m:
                skipped_tests.add(f"{pkg}|{m.group(1)}" if pkg else m.group(1))
                continue
            m = tap_not_ok.match(stripped)
            if m:
                failed_tests.add(f"{pkg}|{m.group(1)}" if pkg else m.group(1))
                continue
            m = tap_ok.match(stripped)
            if m:
                passed_tests.add(f"{pkg}|{m.group(1)}" if pkg else m.group(1))
                continue

            # --- jest lines (default era) -----------------------------------
            m = jest_file.match(body)
            if m:
                cur_file[pkg] = m.group(1)
                stacks[pkg] = {}
                continue

            m = jest_test.match(body)
            if m:
                # Only accept a check-mark line once a "PASS <file>"/"FAIL <file>"
                # header has been seen for this package.  The build step emits its
                # own tick marks ("OK Building modules 45.8 secs", "OK Creating entry
                # file") which would otherwise be counted as tests and inflate n2p.
                if pkg not in cur_file:
                    continue
                indent, mark, name = len(m.group(1)), m.group(2), m.group(3)
                stack = stacks.setdefault(pkg, {})
                path = [stack[k] for k in sorted(stack) if k < indent]
                qualified = "|".join(
                    [pkg, cur_file[pkg], " > ".join(path + [name])]
                )
                if mark in ("✓", "✔"):
                    passed_tests.add(qualified)
                elif mark in ("○", "⚠"):
                    skipped_tests.add(qualified)
                else:
                    failed_tests.add(qualified)
                continue

            m = jest_head.match(body)
            if m and m.group(2) and not m.group(2).startswith(noise):
                indent = len(m.group(1))
                stack = stacks.setdefault(pkg, {})
                stacks[pkg] = {k: v for k, v in stack.items() if k < indent}
                stacks[pkg][indent] = m.group(2)

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
