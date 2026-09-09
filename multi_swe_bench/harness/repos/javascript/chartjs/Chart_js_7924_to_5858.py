"""chartjs/Chart.js interval config for PRs 5858..7924.

Reached two ways, because Instance.create (harness/instance.py:41-48) keys on
`{org}/{number_interval}` and falls back to `{org}/{repo}` when that field is
empty:

  * a record carrying "number_interval": "Chart_js_7924_to_5858" routes straight
    here, which is the unambiguous path and the one to prefer;
  * a record with no number_interval - which is what the raw datasets in this
    tree actually contain, since only build_lht_dataset stamps the field -
    collapses onto the key `chartjs/Chart.js`. That key is claimed twice already
    inside this package (Chart_js_9183_to_8983, and Chart_js_5841_to_1653 which
    wins by being imported later), so such a record would silently build on the
    2.7-era toolchain. `ChartJsPlainKeyDispatcher` at the bottom of this file
    claims the key and routes PRs 5858..7924 here, delegating every other PR to
    whichever class held the key before this module was imported - so nothing
    outside this interval changes behaviour.

Image layout follows the shared-base / per-PR-checkout pattern:

  * base image - one full-history clone shared by every PR of an era. It
    deliberately does NOT check out or prune to a single commit, so the marker
    string that `DockerfileEnhancer._inject_final_sanitize` looks for is kept in
    a comment; that stops the enhancer from pinning the shared base to whichever
    PR happened to win the base-image dedup in `run_mode_image`.
  * PR image - checks out its own BASE_COMMIT (baked in as an ARG default,
    because build_dataset only passes REPO_URL/BASE_COMMIT build args to base
    images), runs prepare.sh, then applies the history hardening at the end so
    the shipped image still carries exactly one commit of history.

Two eras, both present in the interval and verified against the ten base
commits of the dataset:

  * 2.x era (PRs 5858..6343, Chart.js 2.7.3 - 2.8.0, base commits 0351a88a ..
    dd6e007a). No npm "scripts" block at all; tests run through `gulp unittest`,
    which starts karma programmatically with `configFile: karma.conf.js`, so no
    karma CLI flag can reach it. karma.conf.js hard-codes
    `browsers: ['chrome', 'firefox']` and `reporters: ['progress', 'kjhtml']` -
    both literals confirmed byte-identical at all seven base commits - so the
    browser and reporter are swapped by sed in prepare.sh. node:10-buster is the
    era-correct toolchain (the adjacent Chart_js_5841_to_1653 config uses it for
    PRs 4671..5841, i.e. right up to this interval's floor).
  * 3.0 era (PRs 7806..7924, Chart.js 3.0.0-beta[.3], base commits 10f393a5 ..
    c688c2f5). karma 5 + rollup 2; `npm test` is `npm run lint && cross-env
    NODE_ENV=test karma start --auto-watch --single-run --coverage --grep`.
    Karma is invoked directly instead, for three reasons: the lint step must not
    gate the graded artifact, NODE_ENV=test only switches on the istanbul babel
    plugin (coverage is not graded), and karma's CLI options are applied after
    the config file's own set(), so `--browsers chrome` wins. `--grep` must be
    passed: karma.conf.js builds its spec pattern as
    `'test/specs/**/*' + grep + '*.js'` and an absent flag makes that
    `*undefined*.js`, which matches no spec file at all. `--auto-watch` is kept
    for the same reason upstream keeps it - karma.autoWatch selects the
    unminified rollup build, so terser never runs. The toolchain is node:18-slim
    (bookworm), which is what the neighbouring Chart_js_9183_to_8983 config
    already uses for PRs above 7400; bullseye was the obvious era-correct choice
    but is unusable right now - its security suite expired on 2026-09-07 and
    archive.debian.org carries no replacement, so every apt path 404s.

Both eras need a real browser: Debian's chromium is the only build available for
amd64 and arm64 on these releases, wrapped in a --no-sandbox/--headless shim
because the build runs as root. fonts-liberation keeps text metrics stable,
which the image-diff fixtures depend on.

Known dataset limitation, not a defect of this config: the raw dataset's patches
were generated without `--binary`, so PNG fixtures arrive as unusable
"Binary files ... differ" stubs (7 files in PR 5858's test patch, 6 in PR
5880's; the other eight PRs have none). `_sanitize_patch` drops those sections -
git refuses the whole patch otherwise - which means the specs that compare
against a changed or newly added PNG fixture fail at both the test and the fix
stage. They can never be credited as fix-to-pass, so they are excluded rather
than mis-scored; every other spec in those two PRs applies and is graded
normally. Verified with `git apply --check` at each base commit: all ten test
patches and all ten test+fix combinations apply cleanly after sanitising.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

INTERVAL = "Chart_js_7924_to_5858"

# Inclusive PR bounds of this interval, used by the plain-key dispatcher at the
# bottom of this file. They are the interval's own name, not the ten PR numbers
# that happen to be in the current dataset file, so a regenerated dataset with
# other PRs from the same two eras still lands here.
_INTERVAL_MIN_PR = 5858
_INTERVAL_MAX_PR = 7924

# PRs at or above this number are the rollup/karma-5 (Chart.js 3.0) era. The
# dataset splits 6343 | 7806, so any threshold in that gap is equivalent; 7000
# is the round number closest to the actual 2.9/3.0 boundary upstream.
_ROLLUP_ERA_MIN_PR = 7000


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections git cannot apply.

    A section that says "Binary files ... differ" without a following
    "GIT binary patch" payload carries no content, and `git apply` rejects the
    ENTIRE patch when it meets one - taking every usable hunk down with it.
    Dropping just those sections keeps the rest of the patch appliable.
    """
    if not patch:
        return patch

    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    kept: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        body = "\n".join(section)
        unusable = "Binary files" in body and "GIT binary patch" not in body
        if not unusable:
            kept.extend(section)

    for line in patch.split("\n"):
        if header.match(line):
            flush()
            section = [line]
        elif section:
            section.append(line)
        else:
            kept.append(line)
    flush()

    return "\n".join(kept)


def _dropped_binary_paths(patch: str) -> list[str]:
    """Paths of the sections `_sanitize_patch` had to throw away."""
    if not patch:
        return []

    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    paths: list[str] = []
    path: Optional[str] = None
    section: list[str] = []

    def flush() -> None:
        if not section or path is None:
            return
        body = "\n".join(section)
        if "Binary files" in body and "GIT binary patch" not in body:
            paths.append(path)

    for line in patch.split("\n"):
        m = header.match(line)
        if m:
            flush()
            path, section = m.group(2), [line]
        elif section:
            section.append(line)
    flush()

    return paths


def _drop_stale_fixtures(pr: PullRequest) -> str:
    """Shell that removes every image fixture the dataset cannot deliver.

    A fixture is one `test/fixtures/<group>/<name>.{js,json}` file plus the
    `<name>.png` it is compared against; test/index.js turns each of those files
    into one spec. When the raw dataset drops a PNG (see `_sanitize_patch`) the
    spec is left comparing new rendering code against a stale expected image,
    and that is not a suppressed transition but an actively wrong one: on PR
    5858 `controller.radar/point-style` and four siblings pass at the run and
    test stages and then FAIL at the fix stage, which is exactly the
    pass-to-fail shape `Report.check` rejects as an invalid instance.

    Deleting the fixture in all three stages removes the spec everywhere at
    once, so it is simply not graded - the honest outcome for a test whose
    expected artefact was never delivered. Specs that do not depend on a PNG are
    untouched: PR 5858 still contributes its five roundedRect / element.point
    transitions.
    """
    dropped = _dropped_binary_paths(pr.test_patch) + _dropped_binary_paths(pr.fix_patch)
    if not dropped:
        return ""

    targets: list[str] = []
    for path in dropped:
        targets.append(path)
        if path.startswith("test/fixtures/") and path.endswith(".png"):
            stem = path[: -len(".png")]
            targets.extend((f"{stem}.js", f"{stem}.json"))

    listed = " \\\n       ".join(sorted(dict.fromkeys(targets)))
    return (
        "\n# The dataset delivered these files as unusable \"Binary files ... differ\"\n"
        "# stubs, so the fixtures that compare against them would be graded against a\n"
        "# stale expected image. Removed in every stage, so the specs disappear\n"
        "# consistently instead of reporting a spurious pass-to-fail.\n"
        f"rm -f {listed}\n"
    )


# The shell blocks below are kept out of the `.format()` templates because the
# embedded sed expressions are full of quoting that reads badly once escaped
# twice.

# buster is past EOL and only served from archive.debian.org; its Release files
# are expired, hence the Check-Valid-Until override.
_BROWSER_SETUP_BUSTER = """\
RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false \\
        -o Acquire::AllowInsecureRepositories=true update && \\
    apt-get -o APT::Get::AllowUnauthenticated=true install -y \\
        --no-install-recommends --allow-unauthenticated \\
        ca-certificates git chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/*"""

# bookworm is still served from the live mirrors, so the plain update is the
# normal path. The two fallbacks are ordered the way the failures actually
# happen: an expired Release file first (that is all that ails a suite in its
# last months, and Check-Valid-Until alone fixes it), an archived mirror second.
# Rewriting to archive.debian.org FIRST is what breaks: a suite that has expired
# but not yet been archived - bullseye-security was exactly this on 2026-09-09 -
# 404s on archive.debian.org and takes the whole build with it. bookworm keeps
# its sources in the deb822 /etc/apt/sources.list.d/debian.sources; the plain
# sources.list is edited too so the block survives an image base change.
_BROWSER_SETUP_BOOKWORM = """\
RUN apt-get update \\
    || apt-get -o Acquire::Check-Valid-Until=false update \\
    || (sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \\
        sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list 2>/dev/null; \\
        apt-get -o Acquire::Check-Valid-Until=false \\
            -o Acquire::AllowInsecureRepositories=true update); \\
    apt-get install -y --no-install-recommends --allow-unauthenticated \\
        ca-certificates git chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/*"""

# ca-certificates is explicit in both lists: node:18-slim ships none, and the
# infrastructure block the enhancer injects points SSL_CERT_FILE (and six
# symlinks) at /etc/ssl/certs/ca-certificates.crt, so without the package the
# very next step dies with "server certificate verification failed. CAfile:
# none" on `git clone`.

# karma-chrome-launcher resolves the 'chrome' custom launcher (base 'Chrome')
# through CHROME_BIN, so every flag the container needs lives in this wrapper.
_CHROMIUM_WRAPPER = """\
RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --headless --disable-gpu --disable-dev-shm-usage --disable-software-rasterizer --window-size=1280,1024 --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows --remote-debugging-port=9222 "$@"\\n' \\
        > /usr/local/bin/chromium-no-sandbox && \\
    chmod +x /usr/local/bin/chromium-no-sandbox"""

# 2.x era: gulp owns the karma invocation, so the config file itself has to be
# rewritten. Assertions are hard failures - a silently unmatched sed would leave
# firefox in the browser list (no gecko in the image, karma aborts) or leave the
# progress reporter in place (no per-spec lines, parse_log returns nothing).
_KARMA_SETUP_GULP = """\
sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['chrome']/" karma.conf.js
sed -i "s/reporters: \\['progress', 'kjhtml'\\]/reporters: ['spec']/" karma.conf.js
if ! grep -q "browsers: \\['chrome'\\]" karma.conf.js; then
    echo "ERROR: browser swap did not apply to karma.conf.js" >&2
    exit 1
fi
if grep -q "'firefox'" karma.conf.js; then
    echo "ERROR: firefox is still in the browser list of karma.conf.js" >&2
    exit 1
fi
if ! grep -q "reporters: \\['spec'\\]" karma.conf.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.js" >&2
    exit 1
fi"""

# 3.0 era: the browser list is `(args.browsers || 'chrome,firefox').split(',')`.
# The run scripts also pass --browsers on the command line (karma applies CLI
# options after the config file's set(), so they win), but the default is
# narrowed here as well so an unflagged `karma start` cannot reach for firefox.
_KARMA_SETUP_ROLLUP = """\
sed -i "s/args.browsers || 'chrome,firefox'/args.browsers || 'chrome'/" karma.conf.js
sed -i "s/reporters: \\['progress', 'kjhtml'\\]/reporters: ['spec']/" karma.conf.js
if grep -q "chrome,firefox" karma.conf.js; then
    echo "ERROR: browser default swap did not apply to karma.conf.js" >&2
    exit 1
fi
if ! grep -q "reporters: \\['spec'\\]" karma.conf.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.js" >&2
    exit 1
fi

# Turn jasmine's fail-fast OFF. karma.conf.js wires it to karma.autoWatch
# (`failFast: !!karma.autoWatch`), and the run command passes --auto-watch to select
# the unminified rollup build, so the FIRST failing spec aborts the entire run.
# Measured on pr-7806 before this line existed: the test stage reported
# "Executed 1 of 1073 (1 FAILED)" and test_patch_result captured a single test, so
# each stage was compared against a differently truncated suite and the 163 "n2p" it
# produced were just specs that never ran at the test stage. A failing spec at the
# test stage is the expected outcome, not a reason to stop the suite.
sed -i "s/failFast: !!karma.autoWatch/failFast: false/" karma.conf.js

# Turn the inline source map OFF - a defect that only surfaces once fail-fast is gone.
# The same autoWatch flag sets `sourcemap: karma.autoWatch ? 'inline' : false`, and
# karma 5.2.1's bundled source-map 0.7 cannot load lib/mappings.wasm on node 18: the
# first failure karma tries to pretty-print dies with
#   UnhandledRejection: You must provide the URL of lib/mappings.wasm
# and takes the karma server down with it, before any summary line is printed. That
# is why run.log and fix-patch-run.log carried no "Executed N of M" line at all.
# Dropping the map keeps the unminified build - the one that demonstrably runs here -
# and removes the crash trigger. Stacks get uglier; parse_log keys on spec names.
sed -i "s/sourcemap: karma.autoWatch ? 'inline' : false/sourcemap: false/" karma.conf.js

# Both strings were verified byte-identical at all three base commits of this era
# (10f393a5, 8438da9e, c688c2f5). If a commit ever drifts, fail the build here rather
# than ship an image whose graded stages stop at their first failure.
if grep -q "failFast: !!karma.autoWatch" karma.conf.js; then
    echo "ERROR: failFast is still tied to autoWatch; a graded stage would stop at its first failure" >&2
    exit 1
fi
if grep -q "sourcemap: karma.autoWatch" karma.conf.js; then
    echo "ERROR: inline sourcemap still enabled; karma will crash formatting the first failure on node 18" >&2
    exit 1
fi"""

# Hard gate, appended as the LAST thing prepare.sh does. It is deliberately not
# wrapped in `|| true`: both npm installs above are, so a failed install has to be
# caught here or not at all.
#
# Why a load test and not `[ -d node_modules/karma ]`: npm creates a package
# directory early and fills it as it goes, so a truncated download or a failed
# post-install leaves the directory in place. An existence check passes, the image
# builds "successfully", and the graded stage then dies on its first command and
# emits an empty log - which the harness reads as "0 passed, 0 failed", i.e. zero
# FAILURES rather than a broken image. The instance ships silently wrong instead of
# loudly broken. So the gate must execute the same module graph the graded stage
# will, at build time.

# 2.x era: `gulp --tasks` loads gulpfile.js, which requires karma, jasmine, the
# browserify/rollup preprocessor chain and every other devDependency `gulp unittest`
# needs, then prints the task tree and exits. Read-only - it starts no browser and
# runs no test.
_DEPS_GATE_GULP = """
./node_modules/.bin/gulp --tasks > /dev/null
echo DEPS_OK"""

# 3.0 era: there is no gulpfile. Requiring karma.conf.js is the equivalent entry
# point - it pulls in rollup.config.js and the whole @rollup/* plugin chain that the
# preprocessor needs. The two require.resolve calls cover the runner and the test
# framework, which karma.conf.js references by name but does not itself load.
_DEPS_GATE_KARMA = """
node -e 'require("./karma.conf.js"); require.resolve("karma"); require.resolve("jasmine-core"); console.log("DEPS_OK")'"""

_RUN_CMD_GULP = "./node_modules/.bin/gulp unittest 2>&1"

# --grep is required, not decorative: karma.conf.js builds the spec pattern as
# 'test/specs/**/*' + grep + '*.js', and yargs leaves grep undefined without the
# flag, producing a pattern that matches no file. Passing it bare makes it true,
# which the config maps to ''.
_RUN_CMD_ROLLUP = (
    "./node_modules/.bin/karma start karma.conf.js "
    "--single-run --auto-watch --browsers chrome --reporters spec --grep 2>&1"
)


class ChartJsIntervalBase_7924_TO_5858(Image):
    """Shared full-history clone for the 2.x half of the interval."""

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
        return "node:10-buster"

    def browser_setup(self) -> str:
        return _BROWSER_SETUP_BUSTER

    def image_tag(self) -> str:
        return "base-7924_to_5858_node10_fullhist"

    def workdir(self) -> str:
        return "base-7924_to_5858_node10_fullhist"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

{self.browser_setup()}

{_CHROMIUM_WRAPPER}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class ChartJsIntervalBaseNode18_7924_TO_5858(ChartJsIntervalBase_7924_TO_5858):
    """Shared full-history clone for the 3.0-beta half of the interval."""

    def dependency(self) -> Union[str, "Image"]:
        return "node:18-slim"

    def browser_setup(self) -> str:
        return _BROWSER_SETUP_BOOKWORM

    def image_tag(self) -> str:
        return "base-7924_to_5858_node18_fullhist"

    def workdir(self) -> str:
        return "base-7924_to_5858_node18_fullhist"


class ChartJsIntervalDefault_7924_TO_5858(Image):
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
    def is_rollup_era(self) -> bool:
        return self.pr.number >= _ROLLUP_ERA_MIN_PR

    def dependency(self) -> Optional[Image]:
        if self.is_rollup_era:
            return ChartJsIntervalBaseNode18_7924_TO_5858(self.pr, self.config)
        return ChartJsIntervalBase_7924_TO_5858(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        karma_setup = (
            _KARMA_SETUP_ROLLUP if self.is_rollup_era else _KARMA_SETUP_GULP
        ) + (_DEPS_GATE_KARMA if self.is_rollup_era else _DEPS_GATE_GULP)
        run_cmd = _RUN_CMD_ROLLUP if self.is_rollup_era else _RUN_CMD_GULP
        drop_fixtures = _drop_stale_fixtures(self.pr)

        test_body = """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{repo}
"""

        return [
            File(".", "fix.patch", f"{_sanitize_patch(self.pr.fix_patch)}"),
            File(".", "test.patch", f"{_sanitize_patch(self.pr.test_patch)}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: clean tree at $(git rev-parse HEAD)"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Peer/optional warnings are noisy in both eras and are not fatal; the assertion
# below is what actually decides whether the install produced a usable tree.
npm install --no-audit --no-fund || true
if [ ! -d node_modules/karma ]; then
    echo "ERROR: npm install did not produce node_modules/karma" >&2
    exit 1
fi

# karma-spec-reporter is the only reporter available in either era that prints
# one line per spec, which is what parse_log keys tests on. Neither era declares
# it, so it is added here - --no-save keeps package.json clean for git apply.
npm install --no-save --no-audit --no-fund karma-spec-reporter@0.0.32 || true
if [ ! -d node_modules/karma-spec-reporter ]; then
    echo "ERROR: karma-spec-reporter did not install" >&2
    exit 1
fi

{karma_setup}
""".format(repo=self.pr.repo, sha=self.pr.base.sha, karma_setup=karma_setup),
            ),
            File(
                ".",
                "run.sh",
                test_body.format(repo=self.pr.repo) + drop_fixtures + run_cmd + "\n",
            ),
            File(
                ".",
                "test-run.sh",
                test_body.format(repo=self.pr.repo)
                + "git apply --whitespace=nowarn --binary /home/test.patch\n"
                # after the patch: it is what creates the fixture .js files whose
                # .png never arrived (rotation.js, rounded-rect.js on PR 5858)
                + drop_fixtures
                + run_cmd
                + "\n",
            ),
            File(
                ".",
                "fix-run.sh",
                test_body.format(repo=self.pr.repo)
                + "git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch\n"
                + drop_fixtures
                + run_cmd
                + "\n",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "ChartJsIntervalDefault_7924_TO_5858 dependency must be an Image"
            )

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() returns an Image, so DockerfileEnhancer.enhance() hands
        # this text back verbatim and injects nothing. The clone comes from the
        # era base; this layer pins it to THIS PR's commit and then strips the
        # history down to it.
        #
        # The hardening runs after prepare.sh, exactly as in the reference PR
        # image. Its `git checkout --detach ${BASE_COMMIT}` is a no-op on the
        # working tree - HEAD is already that commit - so the karma.conf.js edit
        # prepare.sh made survives into the shipped image.
        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register("chartjs", INTERVAL)
class CHART_JS_7924_TO_5858(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ChartJsIntervalDefault_7924_TO_5858(self.pr, self._config)

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
        """Parse karma-spec-reporter output into stable, unique test ids.

        Both eras run jasmine through karma-spec-reporter, which prints a nested
        tree: describes at increasing indent, then one `✓`/`✗`/`-` line per spec.
        Keying on `<describe>::<describe>::<spec>` rather than the leaf title
        alone matters here - Chart.js reuses spec titles across suites, and a
        leaf-only key would merge distinct specs and hide transitions.

        Known, measured limitation carried over from Chart_js_5841_to_1653:
        Chart.js declares a few duplicate it() titles inside a single describe,
        so jasmine itself gives them one name and no id shape can separate them.
        The merge is conservative - failure wins at the test stage while the fix
        stage needs both halves green - so a collision can only suppress a
        transition, never invent one.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # ASCII "x" is deliberately NOT a failure marker. karma-spec-reporter
        # prints "✓" / "✗" / "-" and never a plain x, while Chart.js has a
        # describe literally named "x mode" (Core.Interaction, the x-axis
        # interaction mode). Accepting bare x turns that heading into a phantom
        # failed test called "mode" in every stage and swallows the heading, so
        # its two specs get filed under their grandparent instead. Measured on
        # pr-5858: it added exactly one bogus failure to each of the run, test
        # and fix results.
        re_spec = re.compile(r"^(\s*)(✓|√|✗|×|-)\s+(.*\S)\s*$")
        re_suite = re.compile(r"^(\s*)(\S.*\S|\S)\s*$")
        re_noise = re.compile(
            r"^\s*(HeadlessChrome|Chrome|Chromium|Firefox|PhantomJS)\b"
            r"|^\s*(Executed|TOTAL|SUCCESS|FAILED|Finished|START|LOG|WARN|INFO|ERROR)\b"
            r"|^\s*\d+\)"
            r"|^\s*at\s"
            r"|^\s*\d+ (spec|test)s?, "
        )

        stack: list[tuple[int, str]] = []
        last_spec_indent: Optional[int] = None
        for raw in log.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue

            m = re_spec.match(line)
            if m:
                indent, mark, title = len(m.group(1)), m.group(2), m.group(3)
                last_spec_indent = indent
                prefix = "::".join(name for lvl, name in stack if lvl < indent)
                test_id = f"{prefix}::{title}" if prefix else title
                if mark in ("✓", "√"):
                    if test_id not in failed_tests:
                        skipped_tests.discard(test_id)
                        passed_tests.add(test_id)
                elif mark in ("✗", "×"):
                    passed_tests.discard(test_id)
                    skipped_tests.discard(test_id)
                    failed_tests.add(test_id)
                else:
                    if test_id not in passed_tests and test_id not in failed_tests:
                        skipped_tests.add(test_id)
                continue

            if re_noise.match(line):
                continue

            m = re_suite.match(line)
            if m:
                ws, name = m.group(1), m.group(2)
                indent = len(ws)
                if indent == 0 or "\t" in ws:
                    continue
                # karma-spec-reporter prints a nested describe at the SAME indent
                # as its parent's specs, and a failure's message/stack strictly
                # deeper than the spec it belongs to. `>` keeps the nested suite
                # and drops the failure body; `>=` would silently flatten every
                # nested describe out of the test id.
                if last_spec_indent is not None and indent > last_spec_indent:
                    continue
                last_spec_indent = None
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, name))

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


# --- plain-key routing -------------------------------------------------------
#
# Everything above is reached through `number_interval`. A raw dataset that does
# not carry that field collapses onto the key `chartjs/Chart.js` instead
# (instance.py:41-48), and whichever module registered that key LAST owns every
# chartjs PR in the file. Today that is Chart_js_5841_to_1653, so an
# interval-less dataset5 builds PRs 5858..7924 on the 2.7-era base image
# (`base-pr-4671-to-5841`, node:10 + browserify) - the wrong toolchain for the
# 3.0-beta half, and a config whose karma seds were never checked against these
# commits for the 2.x half.
#
# This dispatcher claims that key and hands PRs inside this interval to the
# class above. Everything else is delegated to whoever held the key immediately
# before this module was imported, captured below rather than hardcoded: the
# fallback therefore stays byte-identical to the current behaviour for every PR
# this interval does not cover, and it keeps tracking it if the package's import
# order ever changes.
#
# `__new__` returns the delegate itself rather than wrapping it, so this class
# owns no images, scripts or parse_log and cannot drift from what it dispatches
# to. Python skips `__init__` when `__new__` returns an object that is not an
# instance of the class, which is why there is none here.
#
# PRs 6344..7805 fall inside the bounds but between the two eras this file was
# verified against. They are claimed deliberately: prepare.sh asserts on every
# karma.conf.js rewrite, so such a PR fails loudly at build time if its config
# has drifted, instead of being quietly built on a neighbouring toolchain.
_PLAIN_KEY = "chartjs/Chart.js"
_PREVIOUS_PLAIN_OWNER = getattr(Instance, "_registry", {}).get(_PLAIN_KEY)


@Instance.register("chartjs", "Chart.js")
class ChartJsPlainKeyDispatcher(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _INTERVAL_MIN_PR <= pr.number <= _INTERVAL_MAX_PR:
            return CHART_JS_7924_TO_5858(pr, config, *args, **kwargs)

        if _PREVIOUS_PLAIN_OWNER is None:
            raise ValueError(
                f"chartjs PR {pr.number} is outside interval "
                f"{_INTERVAL_MIN_PR}-{_INTERVAL_MAX_PR} and no other config "
                f"claims '{_PLAIN_KEY}'; give the record a number_interval that "
                f"names the era config it belongs to"
            )
        return _PREVIOUS_PLAIN_OWNER(pr, config, *args, **kwargs)
