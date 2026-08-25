import re
from typing import Optional

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
# LiberatedPixelCup/Universal-LPC-Spritesheet-Character-Generator
# ---------------------------------------------------------------------------
# A browser app (vanilla ESM + mithril) that composes LPC spritesheets on a
# canvas.  There is no Node test runner here at all: package.json carries no
# `scripts` block, and the suite is a real page -- `tests_run.html` -- driven by
# testem, which starts a static server, launches headless Chrome and Firefox
# against it, and collects the mocha results over its socket.  Upstream CI
# (.github/workflows/ci.yml) is therefore `npx testem ci` under Xvfb + dbus,
# and that is what is reproduced below.
#
# Image chain:  node:20-bookworm -> Base-PR (toolchain + both browsers, then a
#                                           shallow fetch of ${BASE_COMMIT} and
#                                           the hardening block)
#                                -> Per-PR (npm install, vendored test assets,
#                                           patches applied per run)
# ---------------------------------------------------------------------------
#
# Base image
# ~~~~~~~~~~
# node:20-bookworm: upstream CI pins `node-version: "20"`, and Debian bookworm
# is the newest suite that ships BOTH `chromium` and `firefox-esr` in main.
# Taking both browsers from apt keeps the build free of Google's third-party
# repository (which is amd64-only and would break arm64 builds outright).
#
# Browser discovery
# ~~~~~~~~~~~~~~~~~
# testem resolves the launcher names in `testem.js` by probing $PATH:
# "Chrome" wants google-chrome / google-chrome-stable / chrome and "Firefox"
# wants firefox (testem lib/utils/known-browsers.js).  Debian installs the
# executables as `chromium` and `firefox-esr`, so the base image adds two
# symlinks.  The alternative -- rewriting `launch_in_ci` to ["Chromium"] --
# was rejected twice over: it drops the Firefox half of the matrix, and the
# repo hangs its whole CI flag list (--headless, --no-sandbox, --disable-gpu,
# the fixed 1680x1024 window) off the `Chrome` key of `browser_args`, so a
# renamed launcher would silently run with none of them.
#
# Both browsers are kept because upstream runs both and they are not
# interchangeable here: the suite drives canvas, image decoding and hash/history
# APIs, which is exactly the surface where two engines disagree.  testem tags
# every result with its browser, so the two runs stay separable in the parsed
# output rather than collapsing onto each other (see parse_log).
#
# Vendored test assets
# ~~~~~~~~~~~~~~~~~~~~
# `tests_run.html` pulls mocha, mithril, classnames and bulma from cdnjs/unpkg
# and maps the bare specifiers `chai` and `sinon` through an importmap to
# jsdelivr.  Left alone, that makes every EVALUATION run -- not just the image
# build -- depend on outbound internet, and one CDN blip reads as the entire
# suite failing rather than as the infrastructure error it is.  prepare.sh
# fetches the pinned URLs into an untracked `vendor/` at build time; the run
# scripts then rewrite the page to point at them (rewrite, not fetch, so the
# test-time container needs no network).
#
# The rewrite deliberately lives in the run scripts rather than in prepare.sh:
# `tests_run.html` is a TRACKED file, so a build-time edit to it would be undone
# by prepare.sh's own `git reset --hard` / `git checkout ${BASE_COMMIT}`, and
# would in any case be overwritten by a PR that touches the page.  Untracked
# `vendor/` survives both; the sed is replayed per run, AFTER the patches, so it
# applies to whatever version of the page that run is testing.
# jsdelivr's `+esm` bundles inline their own dependencies, so the vendored
# copies load identically to the CDN ones.
# ---------------------------------------------------------------------------

_EXTRA_PACKAGES = [
    # Test browsers, matching `launch_in_ci: ["Chrome", "Firefox"]`.
    "chromium",
    "firefox-esr",
    # Upstream CI's runner: an X server for the browsers and a session bus,
    # without which Chrome logs `Failed to connect to the bus` and Firefox
    # refuses to start at all.
    "xvfb",
    "dbus",
    "dbus-x11",
    # Chrome renders text into the canvas the tests read back; with no fonts
    # installed those assertions fail for reasons that have nothing to do
    # with the PR.
    "fonts-liberation",
    "fonts-dejavu-core",
]

# The exact pinned assets referenced by tests_run.html at the dataset's base
# commit, as (url, local name) pairs.  Kept as data so the rewrite below and
# the download stay in lockstep.
_VENDORED_ASSETS = [
    ("https://cdnjs.cloudflare.com/ajax/libs/mocha/11.7.2/mocha.css", "mocha.css"),
    (
        "https://cdnjs.cloudflare.com/ajax/libs/mocha/11.7.2/mocha.min.js",
        "mocha.min.js",
    ),
    ("https://cdn.jsdelivr.net/npm/bulma@0.9.4/css/bulma.min.css", "bulma.min.css"),
    ("https://unpkg.com/mithril@2.2.2/mithril.min.js", "mithril.min.js"),
    ("https://unpkg.com/classnames@^2.5.1", "classnames.js"),
    ("https://cdn.jsdelivr.net/npm/chai@6.2.2+esm", "chai.js"),
    ("https://cdn.jsdelivr.net/npm/sinon@21.0.1/+esm", "sinon.js"),
]


def _apply(patches: str) -> str:
    """Apply patches, and ABORT the run if they will not go on.

    The usual `... || true` tail is actively dangerous for this repo.  The test
    patch adds a whole new spec file, and `tests/tests.js` is the only thing
    that pulls specs into the page -- so a silently-skipped patch does not
    produce an obviously broken run, it produces a run that looks exactly like
    a clean baseline (140/140 green) while claiming to be the patched one.  An
    empty log with a loud error is recoverable; a plausible wrong answer is not.
    """
    return f"""if ! git apply --whitespace=nowarn {patches} \\
   && ! git apply --whitespace=nowarn --3way {patches}; then
    echo "patch application failed: {patches}" >&2
    exit 1
fi"""


def _vendor_download() -> str:
    """Build-time: fetch every CDN asset into the untracked vendor/ directory."""
    lines = ["mkdir -p vendor"]
    for url, name in _VENDORED_ASSETS:
        lines.append(f'curl -fsSL --retry 3 -o "vendor/{name}" "{url}"')
    return "\n".join(lines)


def _vendor_rewrite() -> str:
    """Run-time: repoint tests_run.html at vendor/, then prove nothing remote
    is left.  The guard matters more than the sed: if a PR bumps one of these
    pinned versions the substitution silently no-ops, and without the check the
    run would degrade into a network-dependent one that fails opaquely when the
    evaluation sandbox has no egress."""
    # `#` as the sed delimiter keeps the URLs readable; in a BRE the only
    # metacharacters left in them are `.` (matches itself here) and `^` /
    # `+`, which are literal outside an anchor position.
    seds = " \\\n".join(
        f"    -e 's#{url}#/vendor/{name}#g'" for url, name in _VENDORED_ASSETS
    )
    return f"""sed -i \\
{seds} \\
    tests_run.html

if grep -qE 'https?://' tests_run.html; then
    echo "vendor-rewrite: unvendored remote asset still referenced:" >&2
    grep -nE 'https?://' tests_run.html >&2
    exit 1
fi"""


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class LPCImageBase(Image):
    """Shared base image - toolchain + browsers, one build for every PR."""

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return _EXTRA_PACKAGES

    def dockerfile(self) -> str:
        # Toolchain + repo checkout, in that order -- the layout every other
        # config in the tree uses, so the PR image below is only COPY +
        # prepare.sh.
        #
        # Tagged per PR because the hardening block prunes the repo to a single
        # commit, which pins the image to one BASE_COMMIT. That costs nothing:
        # every layer above the clone (apt, symlinks) is byte-identical across
        # PRs of this repo, so Docker's layer cache reuses them and only the
        # clone itself is rebuilt -- which is per-PR either way.
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}

# REPO_URL and BASE_COMMIT are not declared here for the same reason
# DEBIAN_FRONTEND and LANG are not (see below): DockerfileEnhancer emits both,
# and build_dataset.py passes them as build args, for any image whose
# dependency() is a string -- which this one's is.

WORKDIR /home/

# DEBIAN_FRONTEND and LANG are deliberately absent: DockerfileEnhancer._ENV_BLOCK
# (image.py) already sets both, along with TZ, the proxy vars and the CA-cert
# paths, into every Dockerfile it emits. LC_ALL is not in that block, so it is
# set here.
ENV LC_ALL=C.UTF-8

{apt_command}

# testem probes $PATH for the launcher names used in testem.js; Debian ships
# these executables under different names.  See the header comment.
RUN ln -sf /usr/bin/chromium /usr/local/bin/google-chrome \\
    && ln -sf /usr/bin/firefox-esr /usr/local/bin/firefox

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class LPCImageDefault(Image):
    """Per-PR instance image: base commit + npm install + vendored assets."""

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
        return LPCImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _make_run_script(self, patches: str) -> str:
        """Reproduce upstream CI's run step (.github/workflows/ci.yml).

        Deviations from upstream, and why:
          * The X server is waited on by socket rather than upstream's blind
            `sleep 3` -- a fixed sleep is a coin flip on a loaded build host,
            and losing it costs the whole run with `browser_start_timeout: 30`.
            The wait is spelled `if ...; then break; fi` rather than
            `[ -e ... ] && break` only for legibility under `set -e`.  Both
            are in fact safe -- a failing test that is a non-final member of
            an `&&` list is exempt from `set -e` -- but that is a subtlety no
            reader of this script should have to know to be sure the loop
            actually loops.
          * `--reporter tap` is testem ci's own default, passed explicitly so
            parse_log is not at the mercy of that default changing.
          * NO `|| true` on the test command.  testem exits non-zero whenever
            any test fails, which is the normal state of the test.patch stage,
            but that costs nothing here: the harness runs the script with
            `check="ignore"` (session_util.py) and then downloads the log from
            `/home/run_msb.log` regardless of exit status.  Suppressing the
            code would only hide the case that matters -- a testem that never
            starts -- which would otherwise reach parse_log as an empty log,
            become a 0/0/0 TestResult, and be rejected by Report.check() rule 1
            with nothing in the log to say why.
          * `timeout` because a browser that hangs on startup takes the whole
            evaluation with it; 1800s is ~40x the observed full-suite runtime.
        """
        return """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{patches}
bash /home/vendor-rewrite.sh

export CI=true
export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1920x1080x24 &
for _ in $(seq 1 30); do
    if [ -e /tmp/.X11-unix/X99 ]; then
        break
    fi
    sleep 1
done

export XDG_RUNTIME_DIR=/tmp/xdg-runtime
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

timeout -k 60 1800 dbus-run-session -- npx testem ci --reporter tap 2>&1
""".format(
            repo=self.pr.repo,
            patches=patches,
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
                "vendor-rewrite.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{rewrite}
""".format(
                    repo=self.pr.repo,
                    rewrite=_vendor_rewrite(),
                ),
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

npm install || true

{vendor}
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    vendor=_vendor_download(),
                ),
            ),
            # run.sh - baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh - test.patch only
            File(".", "test-run.sh", self._make_run_script(_apply("/home/test.patch"))),
            # fix-run.sh - test.patch + fix.patch
            File(
                ".",
                "fix-run.sh",
                self._make_run_script(_apply("/home/test.patch /home/fix.patch")),
            ),
        ]

    def dockerfile(self) -> str:
        # COPY + prepare.sh, nothing else -- the clone, the checkout and the
        # hardening block all live in the base image now, which is where every
        # other config in the tree puts them. prepare.sh therefore runs against
        # an already-pruned tree, so `npm install` is the only thing this layer
        # contributes.
        dep = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""\
FROM {dep.image_full_name()}

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

# testem's TAP names are "<Browser> <version> - [<duration>] - <mocha title>":
#   ok 7 Chrome 151.0 - [1 ms] - state/hash.js getHashParams should parse ...
# Both of the leading segments are stripped on the way into the result set.
#
# The duration HAS to go: it is wall-clock, so keeping it would give the same
# test a different name on every single run and no two runs would ever
# intersect.  The browser version has to go for the slower version of the same
# problem -- it would pin every recorded name to whichever chromium/firefox
# Debian shipped on the day the image was built, so a rebuild would look like
# the entire suite had been replaced.  The browser NAME stays, because each
# test genuinely runs twice and the two engines can disagree.
_TAP_LINE = re.compile(r"^(not ok|ok)\s+\d+\s*-?\s*(.*)$", re.MULTILINE)
_BROWSER_VERSION = re.compile(
    r"^(Chrome|Firefox|Chromium|Safari|Headless \w+)\s+[\d.]+\s+-\s+"
    r"(?:\[[^\]]*\]\s+-\s+)?"
)
# A trailing TAP directive marks a test the runner did not actually assert on.
_DIRECTIVE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

# A module that fails to load takes the whole page with it -- see the note on
# the instance below -- and testem reports that as a synthetic "Global error"
# entry whose text quotes the failing URL:
#   not ok 1 Chrome 151.0 - [undefined ms] - Global error: Uncaught SyntaxError:
#     ... at http://localhost:7357/6346036541134/tests/state/hash_spec.js, line 4
# That middle path segment is testem's per-run cache-buster, so the name would
# otherwise be unique to a single execution and could never match across runs.
# Reduce any test-server URL to the repo-relative path it points at.
_TESTEM_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+(?:/\d+)?/")


@Instance.register("LiberatedPixelCup", "Universal-LPC-Spritesheet-Character-Generator")
class UniversalLPCSpritesheetCharacterGenerator(Instance):
    """PR 297, measured in-container against this config:

        run.sh       140 pass    0 fail   (70 specs x 2 browsers, ~28s)
        test-run.sh    0 pass    2 fail
        fix-run.sh   170 pass    0 fail   (+15 new state/hash specs x 2)

    Note the shape of the middle row, which is a property of the SUITE and not
    of this config.  Every spec reaches the page through a single ESM entry
    point (`tests/tests.js` imports all of them), so when the test patch lands
    without the fix, the new spec's `import { getHash, ... }` cannot be
    resolved against the unfixed `sources/state/hash.js` and the module graph
    fails to instantiate -- taking all 70 specs down with it, not just the new
    one.  testem reports the wreck as two synthetic "Global error" entries, one
    per browser.

    So the test-patch-only run yields no passing tests at all.  That does NOT
    collapse the classification, because Report.check() rule 6 is baseline-
    first: `run == PASS` is temporal proof a test existed before test.patch,
    so `run=PASS, test=NONE, fix=PASS` is read as classic CBC -- hidden by the
    test patch, restored -- and lands in p2p, not f2p.  Built from the three
    logs above, the report is valid:

        p2p 140  (all via reclassified_from_target)
        n2p  30  (the new state/hash specs, matched to tests/state/hash_spec.js)
        f2p   0    s2p 0    cheating guard 0

    The cheating guard is worth a second look on this repo and does come back
    clean: fix.patch touches `sources/state/hash.js` while the credited specs
    are named "<browser> - state/hash.js <title>" after their describe block,
    so the name and the patched path share a suffix without the guard firing.

    Splitting the specs into one page load each would recover a per-spec
    signal from the middle stage, but only by running the suite in a way the
    project never does; the browser-level failure above is the real behaviour
    of applying that test patch on its own.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LPCImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # testem streams the browsers' own console output into the same log,
        # colour codes and all.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for status, raw_name in _TAP_LINE.findall(clean_log):
            name = raw_name.strip()
            if not name:
                continue

            directive = _DIRECTIVE.search(name)
            name = _DIRECTIVE.sub("", name).strip()
            name = _BROWSER_VERSION.sub(lambda m: f"{m.group(1)} - ", name).strip()
            name = _TESTEM_URL.sub("/", name).strip()
            if not name:
                continue

            if directive:
                skipped_tests.add(name)
            elif status == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

        # A name that failed under one browser and passed under the other keeps
        # both entries, because the browser is part of the name.  Within one
        # name, failure wins: testem re-reports a test it retried.
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
