"""reveal.js harness -- Gulp era (PRs #2746-#99999, v4.x-v6.x).

node:20-bookworm + system chromium + `gulp test` via node-qunit-puppeteer.
Routing keys: reveal_js_2746_to_99999 plus the delivered gulp-era bundle
intervals registered at the bottom of this file.

Hardening split (see image.py)
-----------------------------
Level 1, RevealJsGulpImageBase, is a SHARED image -- one `base-gulp` tag for
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

Level 2, RevealJsGulpImage, is PER-PR, so pinning is exactly right there. Its
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

# Chromium refuses to start as root without --no-sandbox, and the default
# /dev/shm in a container is too small for it. Exported for every stage so the
# three runs share one browser configuration.
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


class RevealJsGulpImageBase(Image):
    """Shared Gulp-era base: node:20-bookworm + a full-history clone."""

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-gulp"

    def workdir(self) -> str:
        return "base-gulp"

    def extra_packages(self) -> list[str]:
        # fonts-liberation: without a font package headless chromium renders
        # blank glyphs, which breaks reveal.js layout assertions.
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


class RevealJsGulpImage(Image):
    """Per-PR Gulp-era image: pins ${BASE_COMMIT}, then hardens."""

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
        return RevealJsGulpImageBase(self.pr, self._config)

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

# gulp-sass/node-sass carry native bindings built for a node ABI this image
# does not have. Stub them so the `css` subtask cannot abort before qunit.
if [ -d node_modules/gulp-sass ]; then
    echo "module.exports = function() {{ return require('stream').PassThrough(); }}" \\
        > node_modules/gulp-sass/index.js
fi || true
if [ -d node_modules/node-sass ]; then
    mkdir -p node_modules/node-sass/lib
    echo "module.exports = {{ info: 'mock', render: function(o,cb){{cb(null)}}, types: {{}} }}" \\
        > node_modules/node-sass/lib/binding.js
fi || true

""".format(repo_dir=REPO_DIR)

        # Left OUT of prepare.sh on purpose: the gold test patch can itself
        # edit gulpfile.js, so this has to re-run AFTER each stage applies its
        # patches or the two would fight.
        #
        # Both edits are environment-only -- add the flags chromium needs to
        # run as root in a container, and drop the `eslint` step that gates
        # `test` so a lint error cannot suppress the entire qunit run. Neither
        # adds nor rewrites an assertion, so neither can manufacture a pass.
        patch_gulpfile = """
if [ -f gulpfile.js ]; then
    if ! grep -q "no-sandbox" gulpfile.js; then
        sed -i "s|puppeteerArgs: *\\[|puppeteerArgs: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu',|" \\
            gulpfile.js || true
    fi
    sed -i "s|gulp.series( *'eslint', *'qunit' *)|gulp.series('qunit')|g" gulpfile.js || true
fi
"""

        # Identical across all three stages so the ONLY difference between them
        # is which patches were applied.
        #
        # Same hard upper bound as the Grunt era. node-qunit-puppeteer waits on
        # a QUnit "done" signal with no timeout of its own, so a test page that
        # never fires it blocks the stage forever -- in the Grunt era that stalled
        # a whole chunk for 46 minutes until the container was killed by hand.
        # `timeout` makes it a bounded, reportable failure instead. A timeout
        # (124) deliberately does NOT fall through to the next runner, which
        # would just burn the same budget again on the same wedged page.
        invoke = """
# npm install ONLY when the runner binary is genuinely absent. Running it
# unconditionally per stage is destructive -- npm prunes anything it deems
# extraneous, which in the Grunt era removed a package the gruntfile
# required and left every stage with 'Task "qunit" not found'.
if [ ! -x ./node_modules/.bin/gulp ]; then
    npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
fi

SUITE_TIMEOUT="timeout -k 30 900"

$SUITE_TIMEOUT npx gulp qunit 2>&1
STATUS=$?
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 124 ]; then
    $SUITE_TIMEOUT npx gulp test 2>&1
    STATUS=$?
fi
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 124 ]; then
    $SUITE_TIMEOUT npm test 2>&1 || true
fi
true
"""

        run_sh = """#!/bin/bash
# Stage 1 (baseline): no patches applied.
set -uo pipefail
cd /home/{repo_dir}
{patch}
{invoke}""".format(repo_dir=REPO_DIR, patch=patch_gulpfile, invoke=invoke)

        test_run_sh = """#!/bin/bash
# Stage 2: gold test patch only.
set -uo pipefail
cd /home/{repo_dir}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn /home/test.patch 2>/dev/null \\
    || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
{patch}
{invoke}""".format(repo_dir=REPO_DIR, patch=patch_gulpfile, invoke=invoke)

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
{patch}
{invoke}""".format(repo_dir=REPO_DIR, patch=patch_gulpfile, invoke=invoke)

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


@Instance.register("hakimel", "reveal_js_2746_to_99999")
class RevealJsGulpInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealJsGulpImage(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    # -- log parsing --------------------------------------------------------
    _ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    # node-qunit-puppeteer per-file summary:
    #   ✔ test/test-core.html [293/293] in 1234ms
    #   ✘ test/test-core.html [292/293] in 1234ms
    _FILE_PASS = re.compile(r"^[✔✓]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")
    _FILE_FAIL = re.compile(r"^[!✘✗×✕✖]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]")

    # node-qunit-puppeteer printFailedTests / printResultSummary detail:
    #   Test: <name>
    #       Status: failed
    _TEST_NAME = re.compile(r"^Test:\s+(.+?)\s*$")
    _TEST_STATUS = re.compile(r"^Status:\s+(passed|failed|skipped)\s*$")

    def parse_log(self, test_log: str) -> TestResult:
        """Parse node-qunit-puppeteer output into STABLE test identities.

        Identity is the reporter's test name, or the test-file path -- never a
        count. Deriving a name from a total (`suite:passed-158`, as the Grunt
        parser once did) would make any change in the number of assertions look
        like one test disappearing and a different one appearing, which
        report.py credits as a NONE -> PASS transition and accepts as
        "fix something" (report.py:216). Counts are read only to decide
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

            m = self._FILE_PASS.match(line)
            if m:
                # Trust the ratio over the glyph: a check mark with [292/293]
                # is a partial failure.
                target = passed_tests if m.group(2) == m.group(3) else failed_tests
                target.add(f"file:{m.group(1)}")
                continue

            m = self._FILE_FAIL.match(line)
            if m:
                failed_tests.add(f"file:{m.group(1)}")
                continue

        # A file reported both ways (file-level pass plus a per-test failure)
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
# See reveal_js_0_to_2745.py for the full rationale: `Instance.create`
# (instance.py:41-48) routes on f"{org}/{number_interval}" with NO fallback to
# the repo key, so every dash-joined bundle value that can be DELIVERED must
# also be a registered key.
#
# Gulp-era bundles only (lowest PR >= 2746, i.e. base.sha is at or past the
# v4.0 switch from Grunt to Gulp). Data-derived from the delivered
# dataset3/hakimel__reveal.js_lht_final.jsonl -- regenerate if that set changes.
_GULP_BUNDLES = [
    [2746, 2752, 2767, 2771],
    [2982, 3005, 3006, 3026, 3027],
    [3019, 3020, 3135, 3156, 3157, 3165],
    [3257, 3268, 3291, 3305, 3310, 3324, 3356, 3358],
    [3409, 3441, 3442, 3443, 3444, 3445, 3446, 3450, 3453, 3454, 3456, 3457,
     3464],
    [3477, 3482, 3489],
    [3568, 3570],
    [3600, 3778, 3807, 3810, 3811],
    [3602, 3618, 3620, 3685, 3701, 3716, 3744],
]

for _bundle in _GULP_BUNDLES:
    Instance.register(
        "hakimel", "-".join(str(n) for n in sorted(set(_bundle)))
    )(RevealJsGulpInstance)
