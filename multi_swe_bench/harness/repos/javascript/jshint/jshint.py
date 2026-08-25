import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Mirrors the package list baked into Image.dockerfile() (image.py) so the
# shared base image installs exactly the canonical toolchain.
_DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]


# ---------------------------------------------------------------------------
# jshint: jshint/jshint
# ---------------------------------------------------------------------------
# Static analysis tool for JavaScript.  Every suite in the dataset era runs on
# **nodeunit 0.9** via the default (console) reporter -- there is no TAP/JSON
# reporter available, so parse_log() consumes the reporter's own line format.
# See parse_log() for the exact grammar.
#
# Four graded suites, matching package.json's `test-*` scripts:
#
#   unit        `nodeunit tests/unit`        -> modules core, envs, module-api,
#                                               objrestspread, options, parser
#   cli         `nodeunit tests/cli.js`      -> module cli.js
#   regression  `nodeunit tests/regression`  -> modules npm, thirdparty
#   browser     `node tests/browser`         -> the browser build of the same
#                                               unit suite, driven headlessly
#
# `tests/test262` is deliberately NOT graded: it needs the test262 git submodule
# (`npm run fetch-test262`), which the hardening block strips from the image.
#
# Image chain (the standard two-layer split -- see DOCKERFILE_QC_PROMPT.md):
#
#   node:18-bookworm -> base-pr-<N>  toolchain + chromium, git clone, checkout
#                                    ${BASE_COMMIT}, history scrub.  The proxy/
#                                    CA/LABEL header and the clone->checkout->
#                                    harden tail are injected by
#                                    DockerfileEnhancer, so this file must NOT
#                                    emit them itself (doing so duplicates the
#                                    ENV block).
#                    -> pr-<N>       inherits base-pr-<N>, COPYs the two patches
#                                    + five scripts, runs prepare.sh once.
#                                    It does not clone, apt-install, or scrub.
#
# Two environment facts drive the whole config:
#
#   1. `--ignore-scripts` is MANDATORY on every npm install.  The pre-fix
#      package.json pins `phantomjs-prebuilt`, whose postinstall downloads a
#      binary from a CDN that no longer exists; without the flag `npm install`
#      fails outright and no image can be built.
#
#   2. The browser suite needs a real Chromium.  It is installed from apt (and
#      pinned via PUPPETEER_EXECUTABLE_PATH) rather than letting puppeteer
#      fetch its own: puppeteer 1.20.0 only publishes an x86_64 build, which
#      would break the arm64 half of a multi-arch build.
#
# Why prepare.sh warms the npm cache with the POST-FIX dependency set:
# for this dataset the fix IS a dependency swap (phantom/phantomjs-prebuilt ->
# puppeteer), so the graded runs must reinstall from package.json *after*
# patching -- otherwise fix.patch has no observable effect.  Warming the cache
# at build time (where the network is available) lets those reinstalls run with
# `--prefer-offline`, so all three graded runs work under `--network none`.
#
# Verified on PR 3438 (`git apply` + full three-run sweep, offline):
#   run  620 pass / test  620 pass / fix 1193 pass
#   -> 620 p2p (node suites, regression safety) + 573 n2p (browser suite).
# ---------------------------------------------------------------------------


def _node_base_image(pr_number: int) -> str:
    """Node base image for a given PR.

    The delivered dataset holds a single row (PR 3438, jshint 2.10.3,
    Dec 2019), which was verified end-to-end on ``node:18-bookworm``.  Bookworm
    is required rather than the era-contemporary ``node:12`` (buster) because
    buster's apt repositories are archived and carry no maintained ``chromium``
    package for arm64.

    Older jshint PRs would need an older Node; add an era branch here.  The base
    image is tagged per-PR (`base-pr-<N>`), so eras cannot collide.
    """
    return "node:18-bookworm"


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class JSHintImageBase(Image):
    """Per-PR base image - Node toolchain + headless Chromium + the repo cloned
    and pinned to ``BASE_COMMIT``."""

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
        return _node_base_image(self.pr.number)

    def extra_packages(self) -> list[str]:
        # `chromium` backs the browser suite; `fonts-liberation` stops Chromium
        # from warning about a fontconfig-less environment on a slim image.
        return ["chromium", "fonts-liberation"]

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # NOTE ON WHAT THIS FILE DELIBERATELY OMITS.
        # DockerfileEnhancer.enhance() (image.py) post-processes this string
        # because dependency() returns a str.  It supplies, and this template
        # must therefore NOT repeat:
        #   * the `# syntax=docker/dockerfile:1.6` directive
        #   * ARG TARGETARCH / REPO_URL / BASE_COMMIT + the proxy ARGs
        #   * the ENV block (DEBIAN_FRONTEND, LANG, TZ, proxy passthrough, TLS)
        #   * the OCI LABEL block and the CA-cert symlink farm
        # It also rewrites the bare `RUN git clone ... /home/<repo>` line below
        # into the canonical tail: clone "${REPO_URL}" -> WORKDIR /home/<repo>
        # -> git reset --hard -> git checkout ${BASE_COMMIT} -> history-scrub +
        # integrity asserts -> CMD ["/bin/bash"].  Nothing may follow that line.
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}

WORKDIR /home/

{apt_command}

# Use the distro Chromium instead of puppeteer's bundled download: puppeteer
# 1.20.0 (pinned by this repo's fix.patch) ships an x86_64-only revision, which
# fails the arm64 leg of a multi-arch build.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

{self.clear_env}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class JSHintImageDefault(Image):
    """Per-PR instance image.  Checks out the base commit, installs deps, warms
    the npm cache for the post-fix dependency set, and injects patches + run
    scripts."""

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
        return JSHintImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # -- shared script fragments -------------------------------------------

    # `--ignore-scripts` is load-bearing (see the module docstring); the other
    # flags keep the working tree clean so check_git_changes.sh stays meaningful
    # and so a stale lockfile cannot contradict a patched package.json.
    _NPM_FLAGS = "--ignore-scripts --no-audit --no-fund --no-package-lock"

    def _apply_block(self, patches: str) -> str:
        """git-apply the given patches, failing loudly rather than silently
        grading an unpatched tree."""
        if not patches:
            return ""
        return """if ! git apply --whitespace=nowarn {patches}; then
    if ! git apply --whitespace=nowarn --3way {patches}; then
        echo "Error: failed to apply {patches}" >&2
        exit 1
    fi
fi
""".format(patches=patches)

    def _make_run_script(self, patches: str) -> str:
        """Generate a graded run script.

        `set -eo pipefail` with NO `|| true` on any test command, per the repo
        config QC standard: a runner that fails to *start* must surface, not be
        swallowed into an empty 0/0/0 TestResult.

        Ordering consequence to be aware of: the four suites run under `set -e`,
        so a failing suite stops the ones after it. The browser suite is
        deliberately LAST because it is the one that fails in the run and test
        stages (its driver cannot start before the fix) -- so the three node
        suites always complete first and their results are always recorded.
        If a future jshint PR makes an earlier suite fail, the later suites
        would not run; re-order so the expected-failing suite stays last.

        The `### SUITE:` markers are consumed by parse_log() to build the test
        keys -- nodeunit prints only the bare module name, and bare test names
        collide across modules (e.g. `plusplus` and `strings` exist in both
        `core` and `options`).
        """
        return """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{apply_block}
# Reinstall from the (possibly patched) package.json.  For this dataset the fix
# IS a dependency change, so this line is what makes fix.patch observable.
# The cache was warmed at build time, so this resolves without a network.
npm install {npm_flags} --prefer-offline 2>&1

echo "### SUITE: unit"
npx nodeunit tests/unit 2>&1

echo "### SUITE: cli"
npx nodeunit tests/cli.js 2>&1

echo "### SUITE: regression"
npx nodeunit tests/regression 2>&1

echo "### SUITE: browser"
timeout 600 node tests/browser 2>&1
""".format(
            repo=self.pr.repo,
            apply_block=self._apply_block(patches),
            npm_flags=self._NPM_FLAGS,
        )

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

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Base-state dependencies.  This also warms the npm cache for every pre-fix
# package.
npm install {npm_flags} || true

# Warm the cache for the POST-fix dependency set as well, then put the tree
# back exactly as it was.  Build time is the only point where the network is
# guaranteed, and the graded runs reinstall after patching -- without this the
# fix run would need to reach the registry.
cp -a node_modules /home/node_modules.base
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install {npm_flags} || true
# test.patch/fix.patch only touch tracked files, so this is an exact revert.
git checkout -- .
rm -rf node_modules
mv /home/node_modules.base node_modules

bash /home/check_git_changes.sh

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    npm_flags=self._NPM_FLAGS,
                ),
            ),
            # run.sh - baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh - test.patch only
            File(".", "test-run.sh", self._make_run_script("/home/test.patch")),
            # fix-run.sh - test.patch + fix.patch (test patch first)
            File(
                ".",
                "fix-run.sh",
                self._make_run_script("/home/test.patch /home/fix.patch"),
            ),
        ]

    def dockerfile(self) -> str:
        # Intentionally tiny.  Everything heavy -- toolchain, clone, the
        # BASE_COMMIT pin, the proxy/CA trust and the history scrub -- is
        # already earned by base-pr-<N>.  This layer only stages the patches
        # and run-scripts and runs prepare.sh once, per the P-series contract.
        # It must NOT clone, apt-install, re-scrub, or re-declare the ARGs:
        # dependency() returns an Image, so DockerfileEnhancer leaves this
        # string untouched and whatever is written here is what gets built.
        dep = self.dependency()
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""\
FROM {dep.image_full_name()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh
"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("jshint", "jshint")
class JSHint(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JSHintImageDefault(self.pr, self._config)

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

    @staticmethod
    def _test_key(suite: str, module: str, name: str) -> str:
        """Build the repo-relative test identifier -- see parse_log's docstring
        for why the head has to be a real path.

        `suite` is our own `### SUITE:` marker; `module` is nodeunit's header
        (the module basename, e.g. `core`, or `cli.js` when a single file was
        passed). Layout of this repo, from package.json's `test-*` scripts:

            unit        nodeunit tests/unit        -> tests/unit/<module>.js
            cli         nodeunit tests/cli.js      -> tests/cli.js
            regression  nodeunit tests/regression  -> tests/regression/<module>.js
            browser     node tests/browser         -> tests/browser.js::<module>
        """
        if suite == "browser":
            # Keyed under the driver the test patch rewrites, with the module
            # kept as a second qualifier so browser and node runs of the same
            # assertion do not collide.
            return (
                f"tests/browser.js::{module}::{name}"
                if module
                else f"tests/browser.js::{name}"
            )
        if suite == "cli":
            return f"tests/cli.js::{name}"
        if suite in ("unit", "regression") and module:
            return f"tests/{suite}/{module}.js::{name}"

        # Unknown suite/module shape: fall back to the raw qualifiers rather
        # than inventing a path that no patch could ever match.
        prefix = "/".join(part for part in (suite, module) if part)
        return f"{prefix}::{name}" if prefix else name

    def parse_log(self, test_log: str) -> TestResult:
        """Parse nodeunit's default (console) reporter.

        The reporter's grammar is fixed in
        ``nodeunit/lib/reporters/default.js`` and its colours are hardcoded in
        ``nodeunit/bin/nodeunit.json`` (NOT TTY-dependent), so the escape codes
        below are stable in a piped container log::

            \\x1b[1m<module>\\x1b[22m          module header (whole line bold)
            ✔ <test name>                     pass
            \\x1b[31m✖ <test name>\\x1b[39m    fail, then an indented stack
            \\x1b[1m\\x1b[32mOK: \\x1b[39m\\x1b[22m<n> assertions (<t>ms)
            \\x1b[1m\\x1b[31mFAILURES: \\x1b[39m\\x1b[22m<n>/<m> assertions ...

        Test names are keyed by their REPO-RELATIVE SOURCE FILE, then the test
        name -- e.g. ``tests/unit/core.js::NumberNaN``.  The file comes from the
        ``### SUITE:`` marker the run scripts emit plus nodeunit's own module
        header; see ``_test_file_for``.  Two reasons:

        * Uniqueness.  Bare nodeunit names collide -- ``plusplus`` and
          ``strings`` each exist in two modules -- and the node-side unit suite
          and the browser build of that same suite report identical names.
        * The harness classifier matches a test to the patch that authored it
          with ``_test_name_matches_files`` (harness/report.py), which compares
          the part BEFORE ``::`` against the patch's file paths.  A key like
          ``browser/core.js`` is not a path, so the matcher can never hit,
          ``_test_patch_matcher_ok`` goes False, and ``_touched_by_test_patch``
          silently falls back to ``return True`` -- crediting every test as n2p
          regardless of evidence.  Real paths make that guard actually run.

        The browser suite is deliberately keyed under ``tests/browser.js`` (the
        driver the test patch rewrites) rather than under ``tests/unit/*.js``
        (where the assertions live), because ``tests/browser.js`` is what makes
        these tests exist as runnable entities at all: before the fix the suite
        could not start, since its PhantomJS driver has no working binary.  The
        module name is kept as a second qualifier
        (``tests/browser.js::core.js::NumberNaN``) so browser and node runs of
        the same assertion stay distinct.

        Verified to make all 1193 observed names unique.

        The summary lines cannot be mistaken for module headers: they carry
        text *after* the bold-off code, so the whole-line anchor fails.  Stack
        traces carry no escape codes at all.

        nodeunit has no notion of a skipped test, so ``skipped_tests`` is
        always empty; a test that never ran is simply absent from the log,
        which the harness scores as ``NONE``.
        """
        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        re_suite = re.compile(r"^###\s*SUITE:\s*(\S+)\s*$")
        # A module header is bold for the WHOLE line; "OK: "/"FAILURES: " are
        # bold only up to the label, so the trailing anchor rejects them.
        re_module = re.compile(r"^\x1b\[1m(.+?)\x1b\[22m\s*$")
        re_test = re.compile(r"^([✔✖])\s+(.+?)\s*$")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        suite = ""
        module = ""

        for raw_line in test_log.split("\n"):
            # The browser suite is relayed through `console.log("", msg.text())`,
            # which prefixes every line with a space -- strip before matching.
            line = raw_line.strip()
            if not line:
                continue

            m_suite = re_suite.match(line)
            if m_suite:
                suite = m_suite.group(1)
                # A new suite restarts nodeunit, so the previous module header
                # no longer applies.
                module = ""
                continue

            m_module = re_module.match(line)
            if m_module:
                module = m_module.group(1).strip()
                continue

            m_test = re_test.match(ansi.sub("", line).strip())
            if not m_test:
                continue

            marker, name = m_test.group(1), m_test.group(2).strip()
            if not name:
                continue

            key = self._test_key(suite, module, name)

            if marker == "✔":
                passed_tests.add(key)
            else:
                failed_tests.add(key)

        # A name seen failing anywhere is a failure.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
