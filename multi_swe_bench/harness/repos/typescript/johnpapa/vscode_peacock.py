import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# =============================================================================
# johnpapa/vscode-peacock -- TypeScript VS Code extension
#                            mocha (tdd ui, spec reporter) inside a real VS Code
#                            instance driven by `vscode-test` + xvfb.
#
# DATASET (output2_old/johnpapa__vscode-peacock_raw_dataset.jsonl, 1 entry)
#   PR 312 "Preserve order of existing settings when writing configuration"
#   base master @ 20308b116189094ea3704e73570b9c66f7ef6df6, merged 2020-03-15
#   `number_interval` and `tag` are both null in the JSONL, so Instance.create()
#   builds the key f"{org}/{repo}" == "johnpapa/vscode-peacock". That is exactly
#   what the single registration at the bottom of this file answers to. One entry
#   means no era split is possible or needed, and no number_interval alias is
#   invented here (inventing one would make the key unresolvable).
#
# WHAT THE SIGNAL IS
#   test_patch touches two files and adds exactly one `test()`:
#       src/test/suite/config-changes.test.ts   -> + test('will preserve setting order')
#       testworkspace/.vscode/settings.json     -> a wider, deliberately
#                                                  out-of-order colorCustomizations
#                                                  block for that test to read.
#   fix_patch touches package.json, src/apply-color.ts,
#   src/configuration/read-configuration.ts, src/models/enums.ts and adds
#   src/object-library.ts (sortSettingsIndexer + a stable merge). No overlap with
#   test_patch's file set, so Report.check() rule 5 (fix patch tampering with a
#   gold test file) cannot trip.
#
#   Measured end to end in Docker on this exact image (node:16-bullseye, VS Code
#   1.134.0 downloaded by vscode-test, xvfb-run):
#       run   (no patch)          147 passing, 0 failing, 1 pending
#       test  (test.patch)        147 passing, 1 failing, 1 pending
#       fix   (both patches)      148 passing, 0 failing, 1 pending
#   The single failing name at the test stage is byte-identical to the name that
#   passes at the fix stage:
#       "changes to configuration > when starting with a color in the workspace
#        config > will preserve setting order"
#   -> one clean FAIL->PASS, no PASS->FAIL anywhere, so rules 1-4 all hold.
#
# NODE 16, NOT NODE 20/22.
#   package.json declares only `engines.vscode` -- there is no engines.node,
#   .nvmrc or .node-version to read -- so the constraint comes from the build
#   chain instead: webpack 4.41.5 (locked) hashes with md4 through Node's crypto,
#   which OpenSSL 3 removed. Any Node >= 17 therefore dies with
#   ERR_OSSL_EVP_UNSUPPORTED before a single test runs. Node 16 is the newest
#   release that still ships OpenSSL 1.1.1, and it also ships npm 8, which reads
#   this repo's lockfileVersion-1 package-lock.json without rewriting it.
#   `node:16-bullseye` is pinned rather than bare `node:16` so the Debian suite
#   the apt list below resolves against cannot drift.
#
# ARM64 IS A REPO-LEVEL LIMIT, NOT A DOCKERFILE ONE.
#   Nothing in this config downloads an architecture-specific binary; the apt
#   package list below resolves on amd64 and arm64 alike. But `vscode-test`
#   (1.3.0 in the lockfile) hardcodes its download platform:
#       switch (process.platform) { ... default: downloadPlatform = 'linux-x64' }
#   so on an arm64 host it fetches an x64 VS Code that cannot exec. There is no
#   config-side fix short of patching a locked dependency; the pipeline's own
#   `--platform linux/amd64` is the remedy. Recorded here so the next reader does
#   not go looking for a missing TARGETARCH branch.
# =============================================================================

# X11 / Chromium shared libraries that Electron (i.e. the VS Code binary that
# vscode-test downloads) needs to boot headlessly, plus xvfb itself.
#
# `xauth` is NOT optional and is listed explicitly: `xvfb-run` shells out to it
# to build the X authority file, and under --no-install-recommends it does not
# come in with xvfb. Without it every stage dies instantly with
# "xvfb-run: error: xauth command not found" and collects 0 tests.
#
# `git`, `ca-certificates`, `curl` and `wget` are already in the default package
# set that Image.dockerfile() installs, so they are not repeated here.
_X11_PACKAGES = [
    "xvfb",
    "xauth",
    "libnss3",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libgtk-3-0",
    "libgbm1",
    "libasound2",
    "libx11-xcb1",
    "libxcb-dri3-0",
    "libdrm2",
    "libxkbfile1",
    "libsecret-1-0",
    "libxshmfence1",
    "libcups2",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libpango-1.0-0",
    "libcairo2",
]


class ImageBase(Image):
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
        return "node:16-bullseye"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>` rather than a shared `base`: build_dataset.py
        # passes BASE_COMMIT as a build arg only for images whose dependency() is
        # a string, and the Dockerfile bakes that one commit in. A shared `base`
        # tag would therefore stay pinned to whichever PR built it first, and any
        # later PR whose base commit is unreachable from that sha would die in
        # prepare.sh. Costs one base image per PR; deliberate, and moot today
        # because the dataset holds exactly one PR.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Base image carries no patches or scripts; those live on ImageDefault.
        return []

    def extra_packages(self) -> list[str]:
        return list(_X11_PACKAGES)

    # dockerfile() is deliberately NOT overridden. The inherited
    # Image.dockerfile() already emits FROM / WORKDIR / DEBIAN_FRONTEND / LANG,
    # the apt install (default packages + extra_packages()), the parameterised
    # `git clone "${REPO_URL}"`, `git reset --hard`, `git checkout ${BASE_COMMIT}`
    # and the history-scrub hardening block, and DockerfileEnhancer.enhance()
    # layers the BuildKit syntax directive, the TARGETARCH/REPO_URL/BASE_COMMIT
    # ARGs, the proxy env, the CA-cert symlinks, the OCI labels and the MITM CA
    # mount on top of it. Re-emitting any of that here would duplicate it.


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
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

# The base image already checked this commit out; repeating it makes prepare.sh
# self-contained and safe to replay (build_dataset.py's envagent path re-runs the
# script body inside a live container before each stage).
#
# Each git step is followed by a clean-tree assertion. `git reset --hard` and
# `git checkout` both exit 0 on a tree that is subtly wrong -- a stray file, a
# half-applied patch, a reset that did not take -- and without a witness every
# graded stage would then run against a tree that is not the base commit while
# still reporting perfectly ordinary numbers. check_git_changes.sh fails the
# build loudly instead.
#
# Placement is deliberate: BOTH assertions sit ABOVE the `sed` further down,
# which intentionally edits a tracked file. Everything above that line must be
# pristine; everything below is dirty by design. That is exactly the boundary
# these two calls draw, and it is why they cannot be moved lower.
#
# They are safe to re-run: .gitignore covers out/, node_modules/, dist/ and
# .vscode-test/, so `git status --porcelain` stays empty even after a full
# build -- which matters because build_dataset.py's envagent path replays this
# script inside an already-built container.
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# `|| true` is REQUIRED on the install: an npm postinstall or an optional native
# module that fails to compile (arm64 is the usual offender) must not abort the
# image build, and everything these tests need is already unpacked by then.
npm ci || true

# Raise mocha's hook/test timeout before compiling.
#
# src/test/suite/index.ts constructs Mocha programmatically with a hardcoded
# `timeout: 7500`. That is not overridable from outside -- the programmatic API
# ignores .mocharc.*, and runTest.js forwards launchArgs to VS Code, not options
# to mocha -- so a sed on the source is the only lever.
#
# It is needed because build_dataset.py runs the three stages CONCURRENTLY
# (ThreadPoolExecutor(max_workers=3) in run_instance). Three Electron/VS Code
# instances on one host starve each other, and `suiteSetup` here drives real
# VS Code configuration writes. Measured on an 8-CPU host: run sequentially the
# suite is 147/0/1 and green; run three-up at 7500ms it collapses to 69 passing
# / 11 failing, every failure a "Timeout of 7500ms exceeded" in a before-all or
# after-all hook -- which takes whole suites down with it and would grade as a
# mass phantom regression. At 180000ms the same three-up run is green again.
#
# This changes tolerance, never semantics: the credited test fails on a
# deepEqual over key order, not on a clock, so nothing about the f2p signal is
# masked. The edit is baked into the image, so all three stages compile from an
# identical source tree and stay comparable.
#
# The grep is an assertion, not a formality: `sed -i` exits 0 when it matches
# nothing, so without it a renamed literal would silently leave the 7500ms
# timeout in place. Under `set -e` the grep turns that into a loud build failure.
sed -i 's/timeout: 7500,/timeout: 180000,/' src/test/suite/index.ts
grep -q 'timeout: 180000,' src/test/suite/index.ts

# Two build outputs are needed and they are NOT the same thing:
#   tsc     -> out/**    the mocha suite + runTest.js that vscode-test executes
#   webpack -> dist/extension.js   package.json "main", i.e. the extension VS Code
#                                  actually loads via --extensionDevelopmentPath
# This is what the repo's own `npm run test-compile` does. Neither is tolerated
# with `|| true`: both exit 0 on a clean tree (verified at this base commit, and
# with test.patch and with both patches applied), so a non-zero exit here is a
# real breakage and should fail the image build loudly rather than bake a stale
# bundle that would silently mis-grade every stage.
npx tsc -p ./
npx webpack --mode none

# Warm vscode-test's download cache at BUILD time. Without this each of the three
# run stages independently pulls ~150 MB of VS Code over the network, tripling the
# runtime and adding three chances for a flaky download to zero out a stage.
# The cache lands in $PWD/.vscode-test, which is why this runs from the repo root.
# `|| true` guards the network fetch only -- if it fails the stages simply
# download it themselves.
node -e "require('vscode-test').downloadAndUnzipVSCode().then(p => console.log('cached VS Code at ' + p))" || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

npx tsc -p ./
npx webpack --mode none

xvfb-run -a node ./out/test/runTest.js
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# No --exclude is needed: both patches in this dataset are pure text. Checked --
# neither fix_patch nor test_patch contains a "GIT binary patch" or "Binary files"
# section, and package-lock.json is untouched by either, so `git apply` has
# nothing it can choke on.
git apply --whitespace=nowarn /home/test.patch

npx tsc -p ./
npx webpack --mode none

xvfb-run -a node ./out/test/runTest.js
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

# test.patch FIRST, then fix.patch -- fix.patch is authored against the tree that
# already carries the new test, and the reverse order rejects.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

npx tsc -p ./
npx webpack --mode none

xvfb-run -a node ./out/test/runTest.js
""".format(pr=self.pr),
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


@Instance.register("johnpapa", "vscode-peacock")
class VscodePeacock(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        return parse_mocha_spec_log(test_log)


# -----------------------------------------------------------------------------
# parse_log
#
# The suite is mocha's TDD ui (`suite()` / `test()`) rendered by the `spec`
# reporter -- src/test/suite/index.ts sets
#     reporter: 'mocha-multi-reporters', reporterOptions:
#         { reporterEnabled: 'spec, xunit', xunitReporterOptions: { output: ... } }
# so xunit goes to test-results.xml on disk and only `spec` reaches stdout.
#
# NAME FORMAT: "Suite > SubSuite > test name".
# Qualifying by the full suite path is NOT cosmetic here. Leaf `test()` names in
# this repo collide badly -- "sets all color customizations for affected
# elements" appears four times inside affected-elements.test.ts alone, under
# "keep foreground color = false/true" and "keep badge color = false/true". A
# leaf-only name would collapse those four into one identity whose status flips
# arbitrarily between stages, which is precisely the shape that produces a
# phantom f2p. With the full path, the parser reproduces mocha's own counters
# exactly on all three measured logs (147/0/1, 147/1/1, 148/0/1), which is proof
# there are no collisions: mocha counts test *events*, this counts unique names,
# and the two agree.
#
# All 18 top-level `suite()` names in the repo are distinct, so the file the test
# came from does not need to be part of the identity -- which is fortunate,
# because the spec reporter never prints it.
# -----------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Passing test:  "      ✓ does not set colour customizations (1638ms)"
# The trailing duration is stripped by the optional group. It MUST be: the same
# test reports "(213ms)" in one stage and "(453ms)" in the next, and leaving it
# in the name would make every test look like two different tests across stages
# -- the classic NONE/FAIL split that trips Report.check() rule 4.
_PASS_RE = re.compile(r"^( +)[✓✔] +(.*?)(?: +\(\d+(?:\.\d+)?(?:ms|s|m)\))?$")

# Pending test: "    - status bar" (mocha renders `test.skip` with a dash).
_PENDING_RE = re.compile(r"^( +)- +(.*?)(?: +\(\d+(?:\.\d+)?(?:ms|s|m)\))?$")

# Failing test, INLINE in the tree: "      1) will preserve setting order".
# This is the only failure form read; the "N failing" detail block at the bottom
# repeats the name split across three differently-indented lines and is skipped
# entirely (see _SUMMARY_RE below).
_INLINE_FAIL_RE = re.compile(r"^( +)\d+\) +(.*?) *$")

# Any other indented, space-indented, non-empty line is a candidate `suite()`.
_SUITE_RE = re.compile(r"^( +)(\S.*?) *$")

# "  147 passing (2m)" / "  1 pending" / "  1 failing"
_SUMMARY_RE = re.compile(r"^ *\d+ +(passing|failing|pending)\b")
_FAILING_SUMMARY_RE = re.compile(r"^ *\d+ +failing\b")

# Shapes that are output, not structure. VS Code writes a lot to the same stdout
# ("[AgentHost] No token resolved for resource: https://api.github.com",
# "[main 2026-...Z] update#setState idle", "(node:12) [DEP0169] DeprecationWarning",
# "[85:0825/100551.290876:ERROR:dbus/bus.cc:405] Failed to connect to the bus").
# Every one of those observed in the real logs starts at column 0 and so is
# already rejected by the leading-space requirement -- these guards are the
# second line of defence for the day a VS Code release indents one of them. A
# false accept is expensive: a phantom suite silently prefixes every test name
# that follows it, in one stage only.
_NOISE_STACK_RE = re.compile(r"^at\s")
_NOISE_ERROR_RE = re.compile(r"^\w*(?:Error|Exception)\b\s*[:\[]")
_NOISE_SRCREF_RE = re.compile(r":\d+:\d+\)?$")


def _is_noise(text: str) -> bool:
    return bool(
        text.startswith("[")
        or text.startswith("(")
        or "://" in text
        or _NOISE_STACK_RE.match(text)
        or _NOISE_ERROR_RE.match(text)
        or _NOISE_SRCREF_RE.search(text)
    )


def parse_mocha_spec_log(test_log: str) -> TestResult:
    # ANSI first, unconditionally: mocha colours its ticks and VS Code wraps its
    # own log lines in \x1b[90m...\x1b[0m. Every regex below anchors on leading
    # spaces, and an un-stripped colour escape sits exactly there.
    clean_log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # (indent, name) pairs; mocha indents each nesting level by exactly 2 spaces.
    suite_stack: list[tuple[int, str]] = []

    def qualified(name: str) -> str:
        return " > ".join([s for _, s in suite_stack] + [name])

    def unwind(indent: int) -> None:
        while suite_stack and suite_stack[-1][0] >= indent:
            suite_stack.pop()

    # Set once the "N failing" summary line is reached. Everything after it is
    # the failure detail block: stack traces, assertion diffs ("+  \"activityBar
    # .activeBackground\"") and the failing name re-printed across three lines at
    # inconsistent indents. Reading it would invent names that exist only in the
    # failing stage -- a guaranteed cross-stage mismatch. The inline "N)" markers
    # earlier in the tree already recorded every failure.
    in_failure_block = False

    for raw_line in clean_log.splitlines():
        # Tabs never appear in mocha's own indentation (only in Node stack
        # frames), so normalising them keeps the 2-space grid honest.
        line = raw_line.replace("\t", "    ")
        if not line.strip():
            continue

        if _SUMMARY_RE.match(line):
            if _FAILING_SUMMARY_RE.match(line):
                in_failure_block = True
            continue

        if in_failure_block:
            continue

        match = _PASS_RE.match(line)
        if match:
            indent = len(match.group(1))
            unwind(indent)
            passed_tests.add(qualified(match.group(2).strip()))
            continue

        match = _INLINE_FAIL_RE.match(line)
        if match:
            indent = len(match.group(1))
            unwind(indent)
            failed_tests.add(qualified(match.group(2).strip()))
            continue

        match = _PENDING_RE.match(line)
        if match:
            indent = len(match.group(1))
            unwind(indent)
            skipped_tests.add(qualified(match.group(2).strip()))
            continue

        match = _SUITE_RE.match(line)
        if match:
            indent = len(match.group(1))
            name = match.group(2).strip()
            # Structural gate. A real mocha suite header sits on the 2-space
            # grid, starts at column 2 when the stack is empty, and can only ever
            # be a direct child (top + 2) or a sibling/ancestor (<= top) of the
            # suite currently open. Anything deeper than top + 2 is not something
            # the spec reporter can emit, so it is stray output.
            expected_max = suite_stack[-1][0] + 2 if suite_stack else 2
            if indent < 2 or indent % 2 != 0 or indent > expected_max:
                continue
            if _is_noise(name):
                continue
            unwind(indent)
            suite_stack.append((indent, name))
            continue

    # TestResult.__post_init__ raises if these sets intersect. Failure wins over
    # pass (a retried test can print both), and pass wins over pending.
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
