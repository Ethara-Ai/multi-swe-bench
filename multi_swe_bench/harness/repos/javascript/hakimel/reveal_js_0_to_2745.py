"""reveal.js harness -- Grunt era (PRs #0-#2745, v3.x).

node:14-bullseye with `grunt test` via PhantomJS/QUnit.
Routing keys: reveal_js_0_to_2745 plus the delivered grunt-era bundle intervals
registered at the bottom of this file.

Hardening split (see image.py)
-----------------------------
Level 1, RevealJsGruntImageBase, is a SHARED image -- one `base-grunt` tag for
every PR in the era -- so it must keep full git history. It opts OUT of
DockerfileEnhancer by emitting `# syntax=docker/dockerfile:1.6` as its first
line (image.py:281 returns the content verbatim the moment it sees that), and
hand-writes the infra the enhancer would otherwise inject.

That opt-out is load-bearing. Without it, _inject_final_sanitize() sees the
`git clone` and appends the full Image._HARDENING_BLOCK, which detaches at
${BASE_COMMIT} and deletes every ref. Since the tag is shared, the image would
be pinned to whichever PR happened to build it first (build_dataset.py:614-620
passes REPO_URL / BASE_COMMIT only when dependency() is a str, i.e. only to
this level) and every other PR's `git checkout` would then fail against a repo
with no refs. So: light hardening here, canonical hardening per-PR.

Level 2, RevealJsGruntImage, is PER-PR, so pinning is exactly right there. Its
dependency() returns an Image, which makes DockerfileEnhancer.enhance() return
the content verbatim -- nothing is injected for us, so Image._HARDENING_BLOCK
is concatenated in by hand after prepare.sh checks out ${BASE_COMMIT}. It also
declares `ARG BASE_COMMIT="<sha>"` with a DEFAULT, because per-PR images get no
build args at all (same build_dataset.py branch) and an argless ARG would
expand empty, leaving the hardening block detaching at nothing.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# GitHub's exact casing, and the path the clone + WORKDIR create. Matches
# image._PATH_COMPONENT_RE (dots are permitted), so it is safe to interpolate
# into RUN/WORKDIR paths.
REPO_DIR = "reveal.js"

# Mirrors the default set in Image.dockerfile(); the hand-written base
# Dockerfile below has to reproduce it since it bypasses that method.
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

_PUPPETEER_ENV = (
    "ENV PUPPETEER_SKIP_DOWNLOAD=true\n"
    "ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium\n"
    "ENV CHROME_BIN=/usr/bin/chromium"
)

# Configure git globally BEFORE cloning. reveal.js has a long history, and the
# arm64 half of a multi-arch build runs under QEMU where a briefly stalled
# transfer easily trips libcurl's low-speed timeout and dies with
# `gnutls_handshake() failed`. Same mitigation the nanopb registry carries.
_GIT_RESILIENCY = (
    "RUN git config --global http.version HTTP/1.1 \\\n"
    "    && git config --global http.postBuffer 1048576000 \\\n"
    "    && git config --global http.lowSpeedLimit 0 \\\n"
    "    && git config --global http.lowSpeedTime 999999 \\\n"
    "    && git config --global core.compression 0 \\\n"
    "    && git config --global submodule.fetchJobs 1"
)

# Retry x3 so one flaky handshake does not discard an hour of emulated build.
# The bare `git clone "${REPO_URL}" /home/<repo>` form is preserved inside the
# loop so the reference-format marker still matches.
_CLONE_WITH_RETRY = (
    "RUN for i in 1 2 3; do \\\n"
    f'        git clone "${{REPO_URL}}" /home/{REPO_DIR} && break; \\\n'
    '        echo "clone attempt $i failed; retrying"; \\\n'
    f"        rm -rf /home/{REPO_DIR}; \\\n"
    "        sleep 10; \\\n"
    "    done; \\\n"
    f"    test -d /home/{REPO_DIR}/.git"
)

# LIGHT hardening only -- drop the origin remote so the image carries no
# upstream to re-fetch from, and stop submodule recursion. The canonical
# Image._HARDENING_BLOCK deliberately does NOT run here; see module docstring.
_LIGHT_HARDENING = (
    "RUN git remote remove origin 2>/dev/null || true; \\\n"
    "    git config --local fetch.recurseSubmodules false; \\\n"
    '    git config --local remote.pushDefault ""; \\\n'
    "    git config --local gc.auto 0"
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


class RevealJsGruntImageBase(Image):
    """Shared Grunt-era base: node:14-bullseye + a full-history clone."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return "base-grunt"

    def workdir(self) -> str:
        return "base-grunt"

    def extra_packages(self) -> list[str]:
        # chromium serves the newer grunt-contrib-qunit (v3+) headless runner;
        # the older PhantomJS path ignores it. Installed unconditionally so one
        # base tag covers the whole v3.x span. fonts-liberation because without
        # a font package headless chromium renders blank glyphs, which breaks
        # reveal.js layout assertions.
        return ["chromium", "fonts-liberation"]

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        # Hand-written infra block: the `# syntax` directive opts this
        # Dockerfile out of DockerfileEnhancer entirely (see module docstring),
        # so nothing below is injected for us and it must stay in sync with the
        # enhancer's reference format.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            "ARG TARGETARCH\n" f'ARG REPO_URL="{repo_url}"\n' "ARG BASE_COMMIT",
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    LC_ALL=C.UTF-8 \\\n"
            "    TZ=UTC",
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(apt_command)
        sections.append(_PUPPETEER_ENV)
        sections.append(_GIT_RESILIENCY)
        sections.append(_CLONE_WITH_RETRY)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(_LIGHT_HARDENING)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class RevealJsGruntImage(Image):
    """Per-PR Grunt-era image: pins ${BASE_COMMIT}, then hardens."""

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
        return RevealJsGruntImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        return _PUPPETEER_ENV

    def files(self) -> list[File]:
        # Build-time only. Checks out ${BASE_COMMIT} (NOT a baked-in literal
        # sha) so the value the hardening block later asserts against is the
        # same one used here.
        prepare_sh = """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git checkout ${{BASE_COMMIT}}
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

npm install --legacy-peer-deps --ignore-scripts 2>&1 \\
    || npm install --ignore-scripts 2>&1 || true

# Older grunt-contrib-qunit (v1.x) drives PhantomJS. The npm postinstall that
# downloads the binary is routinely blocked, so fetch it from the Medium
# mirror and tolerate failure -- the v3.x chromium path covers the rest.
PHANTOMJS_CDNURL=https://github.com/Medium/phantomjs/releases/download/v2.1.1 \\
    npm install --no-save phantomjs-prebuilt@2.1.16 --legacy-peer-deps 2>&1 || true

# node-sass ships a native binding compiled for a node ABI this image does not
# have, and `npm install --ignore-scripts` skips the node-gyp rebuild, so the
# real module cannot load. Stub the PACKAGE ENTRY (lib/index.js is node-sass's
# "main"), not just the native binding.
#
# Stubbing lib/binding.js alone was the bug behind "Loading gruntfile.js
# tasks...ERROR >> TypeError: require(...) is not a function": node-sass's
# lib/index.js does `require('./binding')(require('./extensions'))` -- it CALLS
# the binding -- so an object exported there is not callable. The v3.9+
# gruntfiles open with `const sass = require('node-sass')`, so the whole
# gruntfile failed to load, grunt registered NO tasks, and every stage died on
# 'Warning: Task "qunit" not found' with zero tests captured. The v3.6-era
# Gruntfile.js has no top-level require(), which is exactly why pr-1885 was
# unaffected while pr-2336/pr-2337 both failed.
if [ -d node_modules/node-sass ]; then
    mkdir -p node_modules/node-sass/lib
    cat > node_modules/node-sass/lib/index.js <<'NODE_SASS_STUB'
module.exports = {{
  info: 'node-sass stub',
  render: function (o, cb) {{ cb(null, {{ css: Buffer.from('') }}); }},
  renderSync: function () {{ return {{ css: Buffer.from('') }}; }},
  types: {{}},
  libsassVersion: '0.0.0'
}};
NODE_SASS_STUB
    # Keep ./binding callable too, in case anything requires it directly.
    cat > node_modules/node-sass/lib/binding.js <<'NODE_SASS_BINDING'
module.exports = function () {{
  return {{
    render: function (o, cb) {{ cb(null, {{ css: Buffer.from('') }}); }},
    renderSync: function () {{ return {{ css: Buffer.from('') }}; }},
    libsassVersion: function () {{ return '0.0.0'; }}
  }};
}};
NODE_SASS_BINDING
fi || true

""".format(repo_dir=REPO_DIR)

        # Narrow `test` to qunit only. Left OUT of prepare.sh on purpose: the
        # gold test patch can itself edit Gruntfile.js, so this has to re-run
        # AFTER each stage applies its patches or the two would fight.
        #
        # It only ever removes non-test subtasks (jshint/sass ordering), never
        # adds or rewrites an assertion, so it cannot manufacture a pass.
        body = """
export OPENSSL_CONF=/dev/null

# Hard upper bound on the browser, as a backstop. The per-file `timeout` option
# below should stop any single page wedging the run, but if the runner itself
# hangs before it reads that option there is nothing else to catch it: pr-2336
# parked headless Chromium on about:blank for 46 minutes and stalled the whole
# chunk until the container was killed by hand. -k sends SIGKILL if the runner
# ignores the initial SIGTERM. Healthy stages finish in seconds.
SUITE_TIMEOUT="timeout -k 30 600"

# WHICH build system does the tree have *after* this stage's patches?
# It is not a property of the era. PR #2336 is the 3.9.2..4.0.0 bundle and its
# fix patch DELETES gruntfile.js and ADDS gulpfile.js -- v4.0 is exactly where
# reveal.js swapped Grunt for Gulp. Choosing the runner from the era alone made
# that instance's fix stage die with "Fatal error: Unable to find Gruntfile"
# and capture nothing, while its baseline ran 94 tests happily. Filename casing
# also moved mid-era (v3.6 Gruntfile.js, v3.9+ gruntfile.js).
GRUNTFILE=$(ls Gruntfile.js gruntfile.js 2>/dev/null | head -1)
GULPFILE=$(ls gulpfile.js gulpfile.babel.js 2>/dev/null | head -1)

# npm install ONLY when the runner binary is genuinely missing (i.e. the patch
# swapped the build system out from under node_modules). Running it
# unconditionally is destructive -- npm prunes anything it deems extraneous,
# which previously removed a package gruntfile.js requires and left every stage
# with 'Warning: Task "qunit" not found'.
ensure_runner() {
    if [ ! -x "./node_modules/.bin/$1" ]; then
        npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
    fi
}

if [ -n "$GRUNTFILE" ]; then
    # Narrow `test` to qunit only -- removes non-test subtasks (jshint/sass),
    # never adds or rewrites an assertion, so it cannot manufacture a pass.
    sed -i "s|grunt.registerTask( *'test',[^)]*)|grunt.registerTask('test', ['qunit'])|g" \\
        "$GRUNTFILE" || true

    # grunt-contrib-qunit v3+ drives headless Chrome through puppeteer, which
    # refuses to start as root without --no-sandbox and needs more /dev/shm
    # than a container gets by default. v1.x drives PhantomJS and has neither
    # option, so only patch when v3+ is actually installed -- otherwise this
    # would perturb the era that already passes (pr-1885).
    #
    # `timeout: 30000` is the important half. Without a per-file timeout ONE
    # wedged page zeroes the entire stage: pr-2336's test stage hung on
    # test/test-auto-animate.html (4x ERR_FILE_NOT_FOUND) and lost all ~90
    # otherwise-good tests with it. With it, that file fails -- which is the
    # correct outcome, it IS the F2P test -- and the rest still report.
    QUNIT_MAJOR=$(node -e "try{console.log(require('./node_modules/grunt-contrib-qunit/package.json').version.split('.')[0])}catch(e){console.log(0)}" 2>/dev/null || echo 0)
    if [ "$QUNIT_MAJOR" -ge 3 ] 2>/dev/null; then
        if ! grep -q "no-sandbox" "$GRUNTFILE"; then
            sed -i "s|qunit: *{|qunit: { options: { timeout: 30000, puppeteer: { args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu'] } },|" \\
                "$GRUNTFILE" || true
        fi
    fi

    ensure_runner grunt
    $SUITE_TIMEOUT ./node_modules/.bin/grunt --force qunit 2>&1
    STATUS=$?
    # 124 = timed out. Deliberately do NOT fall through to another runner on a
    # timeout -- it would burn the same budget again on the same wedged page.
    if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 124 ]; then
        $SUITE_TIMEOUT ./node_modules/.bin/grunt --force test 2>&1 || true
    fi
elif [ -n "$GULPFILE" ]; then
    # The 3.9->4.0 migration landed: this stage's tree is a Gulp repo even
    # though the instance's baseline was Grunt. Same environment-only edits the
    # Gulp-era module makes -- browser flags, and drop the `eslint` gate so a
    # lint error cannot suppress the whole qunit run.
    if ! grep -q "no-sandbox" "$GULPFILE"; then
        sed -i "s|puppeteerArgs: *\\[|puppeteerArgs: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu',|" \\
            "$GULPFILE" || true
    fi
    sed -i "s|gulp.series( *'eslint', *'qunit' *)|gulp.series('qunit')|g" "$GULPFILE" || true

    ensure_runner gulp
    $SUITE_TIMEOUT npx gulp qunit 2>&1
    STATUS=$?
    if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 124 ]; then
        $SUITE_TIMEOUT npx gulp test 2>&1 || true
    fi
else
    $SUITE_TIMEOUT npm test 2>&1 || true
fi
true
"""

        run_sh = """#!/bin/bash
# Stage 1 (baseline): no patches applied.
set -uo pipefail
cd /home/{repo_dir}
{body}""".format(repo_dir=REPO_DIR, body=body)

        test_run_sh = """#!/bin/bash
# Stage 2: gold test patch only.
set -uo pipefail
cd /home/{repo_dir}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
{body}""".format(repo_dir=REPO_DIR, body=body)

        # Patch order and flags match test-run.sh exactly; the sole difference
        # between the two stages is fix.patch.
        fix_run_sh = """#!/bin/bash
# Stage 3: gold test patch + fix patch.
set -uo pipefail
cd /home/{repo_dir}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn /home/fix.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
{body}""".format(repo_dir=REPO_DIR, body=body)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        header = f"""FROM {base_name}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{self.extra_setup()}

WORKDIR /home/{REPO_DIR}

{copy_commands}RUN bash /home/prepare.sh

"""

        # Concatenated raw rather than interpolated through the f-string above
        # so the block's own ${BASE_COMMIT} and %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("hakimel", "reveal_js_0_to_2745")
class RevealJsGruntInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealJsGruntImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    # -- log parsing --------------------------------------------------------
    _ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    # grunt-contrib-qunit v3+ (headless Chrome):
    #   ✔ test/test.html [158/158] in 450ms
    #   ✖ test/test.html [155/158] in 450ms
    _V3_PASS = re.compile(r"^[✔✓]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")
    _V3_FAIL = re.compile(r"^[!✘✗×✕✖]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")

    # grunt-contrib-qunit v1.x (PhantomJS):
    #   Testing test/test.html ........OK
    #   Testing test/test.html F
    _PHANTOM_OK = re.compile(r"Testing\s+(\S+)\s+\.*\s*(?:OK|ok)\b")
    _PHANTOM_FAIL = re.compile(
        r"Testing\s+(\S+)\s+\.*\s*(?:F\b|FAILED|failed|FAIL|fail)"
    )

    # Per-assertion detail some qunit reporters emit, giving real test names.
    _TEST_NAME = re.compile(r"^Test:\s+(.+?)\s*$")
    _TEST_STATUS = re.compile(r"^Status:\s+(passed|failed|skipped)\s*$")

    def parse_log(self, test_log: str) -> TestResult:
        """Parse grunt-contrib-qunit output into STABLE test identities.

        Identity is the test-file path (or the reporter's test name), never a
        count. An earlier version of this parser synthesised names from the
        summary line -- `>> 158 assertions passed` became a "test" called
        `suite:passed-158`. That made identity a function of the pass COUNT, so
        any fix that changed the total (157 -> 158) retired one synthetic name
        and introduced another, which report.py reads as a test transitioning
        NONE -> PASS. Check 3 ("fix something", report.py:216) would then be
        satisfied by a pure count change with no test actually going from
        failing to passing. Counts are still parsed, but only to decide
        pass/fail for a real file.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pending: Optional[str] = None
        for raw in test_log.splitlines():
            line = self._ANSI.sub("", raw).strip()
            if not line:
                continue

            m = self._TEST_NAME.match(line)
            if m:
                pending = m.group(1).strip()
                continue
            m = self._TEST_STATUS.match(line)
            if m and pending is not None:
                {"passed": passed_tests, "failed": failed_tests}.get(
                    m.group(1), skipped_tests
                ).add(pending)
                pending = None
                continue

            m = self._V3_PASS.match(line)
            if m:
                # Trust the ratio over the glyph: a check mark with [155/158]
                # is a partial failure.
                target = passed_tests if m.group(2) == m.group(3) else failed_tests
                target.add(f"file:{m.group(1)}")
                continue

            m = self._V3_FAIL.match(line)
            if m:
                failed_tests.add(f"file:{m.group(1)}")
                continue

            m = self._PHANTOM_FAIL.search(line)
            if m:
                failed_tests.add(f"file:{m.group(1)}")
                continue

            m = self._PHANTOM_OK.search(line)
            if m:
                passed_tests.add(f"file:{m.group(1)}")
                continue

        # A file reported both ways (file-level OK plus a per-test failure)
        # is a failure.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing ========================================
# Every record in the lht dump is a release-line BUNDLE, and `Instance.create`
# (instance.py:41-48) builds the registry key as f"{org}/{number_interval}"
# whenever that field is non-empty, with NO fallback to the repo key. So every
# dash-joined bundle value that can be DELIVERED must also be a registered key,
# or re-ingesting the resolved jsonl dies with "Instance 'hakimel/<interval>'
# is not registered".
#
# The value is the EXPLICIT dash-joined list, not a "start-end" range:
# "1885-2171" would wrongly imply every PR number in between is in the bundle.
#
# Grunt-era bundles only (lowest PR <= 2745, i.e. base.sha predates the v4.0
# switch to Gulp). Data-derived from the delivered
# dataset3/hakimel__reveal.js_lht_final.jsonl -- regenerate if that set changes.
GRUNT_MAX_PR = 2745

_GRUNT_BUNDLES = [
    [1885, 1958, 2042, 2045, 2077, 2078, 2080, 2097, 2114, 2121, 2128, 2131,
     2133, 2141, 2158, 2171],
    [2336, 2581, 2651, 2666],
    [2337, 2364, 2378, 2392, 2400, 2410, 2416, 2433, 2437, 2442, 2451, 2453,
     2454, 2474, 2483, 2499, 2513, 2567],
]

for _bundle in _GRUNT_BUNDLES:
    Instance.register(
        "hakimel", "-".join(str(n) for n in sorted(set(_bundle)))
    )(RevealJsGruntInstance)
