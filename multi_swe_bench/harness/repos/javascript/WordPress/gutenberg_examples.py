"""Repo config for WordPress/gutenberg-examples (JavaScript).

Written against handoff/DOCKERFILE_FORMAT.md:

  * The base image's dependency() returns a *string*, so DockerfileEnhancer
    rewrites it and it receives the REPO_URL / BASE_COMMIT build args. The repo
    fetch therefore lives there.
  * Every toolchain RUN sits ABOVE the clone line, because
    _standardize_repo_fetch replaces that line with a block ending in
    CMD ["/bin/bash"].
  * The PR image is minimal: COPY patches and scripts, RUN prepare.sh.

WHY THIS CONFIG IS UNUSUAL: it hosts WordPress itself
-----------------------------------------------------
PR 174 adds end-to-end tests. The specs import `@wordpress/e2e-test-utils` and
call createNewPost() / insertBlock() against a global Puppeteer `page`, which
means they need a *live WordPress* to drive.

Upstream that instance is provided by `wp-env`, which starts WordPress in its
own Docker containers. That is unusable here -- the grading container has no
Docker daemon, so wp-env would need Docker-in-Docker. Instead this config
installs the whole stack directly into the image and reproduces the contract
`@wordpress/e2e-test-utils` expects by default:

    WP_BASE_URL = http://localhost:8888
    WP_USERNAME = admin
    WP_PASSWORD = password

MariaDB, PHP's built-in web server and WordPress core are installed at build
time (prepare.sh) so they are baked into the image; start-services.sh then
brings the daemons up at the start of each grading stage, because a fresh
container starts with nothing running.

Verified directly in this base image before the config was written: MariaDB
reaches ping in ~1s, `wp core install` succeeds, and both `/` and
`/wp-login.php` return HTTP 200 on port 8888.

Puppeteer uses the distro Chromium (PUPPETEER_EXECUTABLE_PATH) rather than
downloading its own, which keeps the arm64 half of a multi-arch build working,
and runs with --no-sandbox because there is no user namespace inside the
container.

WHAT THE GRADING SIGNAL IS
--------------------------
The test patch adds 8 e2e specs and DELETES the repo's only two unit tests
(test/examples.js and its snapshot). The fix patch is small: it adds
`@wordpress/e2e-test-utils` to devDependencies and a `test:e2e` script.

So at the test-patch stage the spec files cannot be imported -- the module is
not installed -- and Jest reports a suite-level failure with no individual test
lines. After the fix patch the dependency exists and the specs run:

    run   no e2e specs exist yet          -> 0 tests
    test  8 suites fail to import         -> 8 failures
    fix   8 suites run                    -> 8 passes

That is a FAIL -> PASS (f2p) transition.

Because of that, parse_log MUST record suite-level failures. A spec file that
fails to import runs no tests at all and would otherwise vanish from the counts
entirely, which is exactly the case that carries the signal here.

WHY TEST_CMD CALLS wp-scripts DIRECTLY, NOT `npm run test:e2e`
--------------------------------------------------------------
The obvious command is `npm run test:e2e`, and it is wrong. That npm script is
added by the *fix* patch -- it does not exist at the base commit:

    scripts at base: build, build:all, build:non-block, env:start, ..., test
    test:e2e at base: absent

So `npm run test:e2e` would die with "Missing script" in BOTH the run and
test-patch stages, before Jest ever started. Both stages would report zero
tests, the suite-level failures above would never be produced, and the instance
would be graded on essentially no data.

`@wordpress/scripts` is a devDependency at the base commit, so invoking its
binary through npx resolves in every stage and Jest actually runs each time.
"""

from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# THE TEST COMMAND
#
# --runInBand is not optional: every spec drives the SAME WordPress instance,
# and Jest's default parallel workers would have several browsers creating
# posts in one database at once, making results depend on timing.
# --verbose forces one line per test, which parse_log needs.
#
# Invoked through npx rather than `npm run test:e2e` because that npm script is
# added by the fix patch and does not exist at the base commit -- see the module
# docstring. wp-scripts itself IS a base devDependency, so npx resolves it in
# every stage.
# ---------------------------------------------------------------------------
# --testTimeout=120000 because Jest's 30s default is not enough here: the
# editor is served by PHP's built-in server and each spec has to boot the whole
# Gutenberg app. Measured ~9s per spec once warm, but the first is far slower.
TEST_CMD = "npx wp-scripts test-e2e --runInBand --verbose --testTimeout=120000"

# Brought up at the start of every grading stage. Idempotent by design.
START_SERVICES = "bash /home/start-services.sh"

BASE_IMAGE = "node:18-bookworm"

# System-level setup only; this runs before the repo exists.
TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git less unzip \
        default-mysql-server \
        php php-cli php-mysql php-xml php-mbstring php-curl php-zip php-gd \
        chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/wp \
        https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp \
    && wp --info --allow-root

# Puppeteer must use the distro Chromium. Its own download has no arm64 build,
# so letting it fetch one breaks the arm64 half of a multi-arch build.
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV JEST_PUPPETEER_CONFIG=/home/jest-puppeteer.config.js

# The defaults @wordpress/e2e-test-utils assumes when wp-env is absent.
ENV WP_BASE_URL=http://localhost:8888
ENV WP_USERNAME=admin
ENV WP_PASSWORD=password
ENV CI=true
ENV NODE_ENV=test

# `php -S` is single-threaded by default. The Gutenberg editor pulls dozens of
# assets and REST calls concurrently, and a single-threaded server deadlocks on
# them -- observed as Puppeteer's "Execution context was destroyed, most likely
# because of a navigation". Workers make the built-in server usable here.
ENV PHP_CLI_SERVER_WORKERS=8

# WordPress is PINNED, and the pin is load-bearing. `wp core download` with no
# version fetches latest (7.x), whose editor no longer carries the selectors
# @wordpress/e2e-test-utils@9.x targets -- insertBlock() then dies with
# "No node found for selector: .edit-post-header [aria-label=\"Add block\"]".
# PR 174 merged 2023-03-09, when 6.1.1 was current.
ENV WP_VERSION=6.1.1
"""


class WordPressGutenbergExamplesImageBase(Image):
    """Level 1: per-PR base image -- toolchain plus the repository checkout.

    Tagged `base-pr-<number>` rather than a shared `base`: a shared tag bakes in
    one BASE_COMMIT and stays pinned to whichever PR built it first, so a later
    PR whose base commit is unreachable from that sha dies in prepare.sh with
    `fatal: unable to read tree`.
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
        return BASE_IMAGE

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

        # TOOLCHAIN_SETUP must stay above `code`: the enhancer replaces that
        # line with clone + checkout + hardening + CMD.
        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{code}

{self.clear_env}

"""
        )


class WordPressGutenbergExamplesImageDefault(Image):
    """Level 2: per-PR image -- patches, run scripts, and the warm-up build."""

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
        return WordPressGutenbergExamplesImageBase(self.pr, self._config)

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
            # Kept OUTSIDE the repo on purpose: writing it into the worktree
            # would leave the tree dirty and fail check_git_changes.sh.
            File(
                ".",
                "jest-puppeteer.config.js",
                """// Puppeteer cannot use its sandbox inside a container (no user
// namespaces), and must be pointed at the distro Chromium because its own
// download has no arm64 build.
module.exports = {
	launch: {
		executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium',
		headless: 'new',
		args: [
			'--no-sandbox',
			'--disable-setuid-sandbox',
			'--disable-dev-shm-usage',
			'--disable-gpu',
		],
	},
};

""",
            ),
            # Deliberately NOT `set -e`: this runs at the top of every run
            # script and must be idempotent. On the second and third stages the
            # daemons are already up and the start commands must no-op rather
            # than abort the stage.
            #
            # Readiness is polled, never slept on -- a fixed sleep is exactly
            # what makes runs flaky on a loaded host.
            File(
                ".",
                "start-services.sh",
                """#!/bin/bash
# Bring up MariaDB and the PHP web server. Safe to call repeatedly.

mkdir -p /var/run/mysqld
chown -R mysql:mysql /var/run/mysqld /var/lib/mysql 2>/dev/null

# The docroot must exist before `php -S` is started: PHP's built-in server
# exits immediately with "Directory /var/www/wp does not exist." rather than
# serving an empty tree. prepare.sh calls this helper once BEFORE WordPress has
# been downloaded (it needs MariaDB up in order to create the database), so the
# directory is created here rather than relying on call order.
mkdir -p /var/www/wp

# Timeouts are sized for the SLOWEST case, not the observed one. Natively
# MariaDB answers ping in about a second, but during a multi-arch build the
# arm64 half runs under QEMU and a 60s budget is not close to enough -- that
# limit failed the build with "mariadb failed to start" while mysqld_safe was
# still coming up perfectly normally. Polling exits as soon as the service is
# ready, so a generous ceiling costs nothing on the native arch.
if ! mysqladmin ping --silent 2>/dev/null; then
  mysqld_safe >/tmp/mysqld.log 2>&1 &
  n=0
  until mysqladmin ping --silent 2>/dev/null; do
    n=$((n+1))
    if [ $n -gt 600 ]; then
      echo "start-services: mariadb failed to start" >&2
      tail -30 /tmp/mysqld.log >&2
      exit 1
    fi
    sleep 1
  done
  echo "start-services: mariadb ready after ${n}s"
else
  echo "start-services: mariadb already running"
fi

# NOTE: `curl -s` without -f on purpose. Readiness here means "the server
# answered", not "the page exists". On the first call the docroot is still
# empty and PHP correctly returns 404; with -f curl would treat that as failure
# and the poll would spin for its full timeout before giving up on a server
# that was actually running.
if ! curl -s -o /dev/null http://localhost:8888/ 2>/dev/null; then
  php -S 0.0.0.0:8888 -t /var/www/wp >/tmp/php-server.log 2>&1 &
  n=0
  until curl -s -o /dev/null http://localhost:8888/ 2>/dev/null; do
    n=$((n+1))
    if [ $n -gt 300 ]; then
      echo "start-services: php server failed to answer on :8888" >&2
      tail -30 /tmp/php-server.log >&2
      exit 1
    fi
    sleep 1
  done
  echo "start-services: wordpress ready after ${n}s"
else
  echo "start-services: wordpress already serving"
fi

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

# ---------------------------------------------------------------------------
# EVERYTHING BELOW IS NATIVE-ARCH ONLY.
#
# MariaDB does not come up under QEMU. On the arm64 half of a multi-arch build
# it was still not answering `mysqladmin ping` after 713 seconds, while
# mysqld_safe reported a completely normal start -- the daemon hangs rather
# than being slow, so raising the timeout does not help. (60s and 600s both
# failed; the amd64 half of the same build finished fine.)
#
# Skipping the foreign arch is safe for exactly the reason the rulebook gives
# for skipping the test warm-up there: docker_util.run() passes no platform to
# containers.run(), and build() loads only the native platform into the daemon,
# so run / test-run / fix-run and the final report are ALWAYS produced on the
# native arch. The foreign-arch image ships without a WordPress install and
# without node_modules -- it loses a warm environment, not correctness.
#
# It is also much faster: the arm64 half now does a checkout and stops, instead
# of emulating npm install, webpack and a database.
# ---------------------------------------------------------------------------
if [ "$(uname -m)" = "x86_64" ]; then

bash /home/start-services.sh

mysql -e "CREATE DATABASE IF NOT EXISTS wordpress;" || true
mysql -e "CREATE USER IF NOT EXISTS 'wp'@'localhost' IDENTIFIED BY 'wp';" || true
mysql -e "GRANT ALL ON wordpress.* TO 'wp'@'localhost'; FLUSH PRIVILEGES;" || true

mkdir -p /var/www/wp
cd /var/www/wp
wp core download --version="$WP_VERSION" --allow-root --quiet || true
wp config create --dbname=wordpress --dbuser=wp --dbpass=wp --dbhost=127.0.0.1 \\
    --allow-root --force --quiet || true

# Freeze the install. WordPress ships automatic background updates that fire on
# ordinary page loads, and the e2e warm-up at the end of this script generates
# plenty of those. Without this the carefully pinned 6.1.1 silently upgraded
# itself to 7.x DURING the build: the version guard below passed, and the
# finished image nonetheless shipped 7.1, whose editor lacks the selectors
# @wordpress/e2e-test-utils@9.x needs. The pin is worthless unless updates are
# disabled as well.
wp config set AUTOMATIC_UPDATER_DISABLED true --raw --allow-root || true
wp config set WP_AUTO_UPDATE_CORE false --raw --allow-root || true
wp config set DISABLE_WP_CRON true --raw --allow-root || true

bash /home/start-services.sh

wp core install --url=http://localhost:8888 --title=gutenberg-examples \\
    --admin_user=admin --admin_password=password \\
    --admin_email=admin@example.com --skip-email --allow-root || true

# HARD GATE. Every wp-cli call above ends in `|| true` so that a re-run over an
# existing install is not fatal -- but that also swallows a genuine failure.
# This was not hypothetical: with WP_VERSION unset, `wp core download
# --version=""` printed "Release not found", `|| true` hid it, and the image
# was built with no WordPress at all. The breakage only surfaced much later as
# eight timed-out e2e specs.
#
# Fail the build here instead, where the cause is still legible.
if [ ! -f /var/www/wp/wp-includes/version.php ]; then
  echo "prepare.sh: FATAL -- WordPress core is not installed at /var/www/wp" >&2
  echo "prepare.sh: WP_VERSION=[$WP_VERSION]" >&2
  exit 1
fi
echo "prepare.sh: WordPress present -- $(grep -m1 'wp_version =' /var/www/wp/wp-includes/version.php)"

# ---------------------------------------------------------------------------
# Node dependencies and the block build.
#
# `|| true` on install: native module compilation is a common and non-fatal
# failure on arm64, and the grading stages re-resolve what they need.
# ---------------------------------------------------------------------------
cd /home/{pr.repo}
npm install --no-audit --no-fund || true
npm run build:all || true

# `npm install` rewrites package-lock.json. That leaves the worktree dirty, and
# because the fix patch also patches package-lock.json, `git apply` in
# fix-run.sh then dies with "package-lock.json: patch does not apply" -- which
# loses the entire fix stage.
#
# node_modules/ and build/ are gitignored, so restoring the tracked files keeps
# the installed dependencies and the compiled blocks while returning the tree
# to exactly BASE_COMMIT, which is the state every patch expects.
git checkout -- .
bash /home/check_git_changes.sh

# Link the BUILT plugins, never the sources. This distinction is the single
# most important line in this file.
#
# block.json declares "editorScript": "file:./index.js" and index.php calls
# register_block_type( __DIR__ ), so WordPress loads index.js from the plugin's
# OWN directory. The sources under blocks-jsx/ are raw ESNext/JSX; linking them
# makes WordPress enqueue untranspiled code, the browser reports
# "SyntaxError: Cannot use import statement outside a module", and the block
# never registers -- so it is missing from the inserter and insertBlock() fails
# with a selector error that looks like an editor-version problem but is not.
#
# `npm run build:all` above emits proper plugin directories (compiled index.js,
# index.asset.php, the CSS, block.json and index.php copied in by
# --webpack-copy-php) under build/. Measured: linking sources registers 0
# blocks, linking build/ registers 7.
# Activate the repository ROOT as a single plugin -- exactly what `wp-env` does
# upstream, and what the project's own CI relies on.
#
# The root index.php carries a "Plugin Name: Gutenberg Examples" header and
# requires all 18 block entry points itself, each from the correct tree:
#
#     require ... 'blocks-non-jsx/03-editable/index.php';        <- source (ES5)
#     require ... 'build/blocks-jsx/03-editable-esnext/index.php'; <- built
#     require ... 'build/non-block-examples/format-api/index.php'; <- built
#
# Linking the 12 block directories individually instead looks equivalent and is
# not. Two blocks then compete in the inserter: searching the exact title
# "Example: Editable" also matches "Example: Editable (ESNext)", the wrong block
# gets inserted, and the spec's [data-type="..."] assertion returns null. That
# produced a 4-pass/4-fail split that reads like flakiness. Deferring to the
# root plugin removes the ambiguity and keeps this config from having to
# re-derive a mapping the repository already maintains.
mkdir -p /var/www/wp/wp-content/plugins
ln -sfn /home/{pr.repo} /var/www/wp/wp-content/plugins/{pr.repo}
wp plugin activate {pr.repo} --allow-root --path=/var/www/wp || true
wp plugin list --allow-root --path=/var/www/wp || true

# Warm the caches so the three grading stages are fast. Skipped on a foreign
# architecture, where this runs under QEMU at roughly 10x slower and buys
# nothing: grading always happens on the native arch.
if [ "$(uname -m)" = "x86_64" ]; then
  {test_cmd} || true
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

# Re-assert the version AFTER the warm-up. The earlier gate proves WordPress
# installed; this one proves it did not update itself while the warm-up drove
# the admin UI. Both are needed -- the first build to ship a wrong version
# passed the install gate and drifted afterwards.
if ! grep -q "wp_version = '$WP_VERSION'" /var/www/wp/wp-includes/version.php; then
  echo "prepare.sh: FATAL -- WordPress drifted from the pinned version" >&2
  echo "prepare.sh: wanted $WP_VERSION, found:" >&2
  grep -m1 "wp_version =" /var/www/wp/wp-includes/version.php >&2
  exit 1
fi
echo "prepare.sh: version still pinned at $WP_VERSION after warm-up"

else
  echo "prepare.sh: $(uname -m) is not the grading architecture."
  echo "prepare.sh: skipped the WordPress stack, npm install, the block build"
  echo "prepare.sh: and the test warm-up. Grading always runs on the native"
  echo "prepare.sh: arch, so this image only needs the checkout."
fi

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
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
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
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
# DELIBERATE ASYMMETRY: only this stage re-installs. The fix patch is what adds
# @wordpress/e2e-test-utils to devDependencies, so the specs cannot import it
# until node_modules is refreshed. run.sh and test-run.sh must NOT do this --
# installing there would hand the test-patch stage the very dependency whose
# absence is the grading signal. TEST_CMD itself is byte-identical in all three.
npm install --no-audit --no-fund || true
{start_services}
{test_cmd}

""".format(pr=self.pr, start_services=START_SERVICES, test_cmd=TEST_CMD),
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


# ---------------------------------------------------------------------------
# LOG PARSING  (Jest, via wp-scripts test-e2e)
#
# With --verbose Jest prints a suite header and one line per test:
#
#     PASS blocks-jsx/03-editable-esnext/e2e/basic.spec.js
#       V Example: Editable (ESNext) block should be available (2033 ms)
#     FAIL blocks-non-jsx/03-editable/e2e/basic.spec.js
#       X Example: Editable block should be available (51 ms)
#
# Two things this parser must get right:
#
#   1. The trailing "(2033 ms)" is variable between stages. Left in the id, the
#      same test would appear under two different names across stages and be
#      counted twice -- so it is stripped.
#   2. A suite that fails to IMPORT prints its FAIL header and no test lines at
#      all. That is precisely what happens at the test-patch stage here, where
#      @wordpress/e2e-test-utils is not yet installed. Recording the suite
#      failure is what preserves the grading signal.
# ---------------------------------------------------------------------------

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Jest prints "PASS"/"FAIL" then the spec path.
_RE_SUITE = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.spec\.js|\S+\.js)")
# Verbose per-test lines. Jest uses different glyphs per platform/locale.
_RE_TEST = re.compile(r"^\s*[✓✔✕✖×○✎√]\s+(.+)$")
_RE_TIMING = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")

_PASS_GLYPHS = "✓✔√"  # check marks
_FAIL_GLYPHS = "✕✖×"  # crosses
_SKIP_GLYPHS = "○✎"  # circle / pencil (skipped, todo)

KNOWN_FLAKY_TESTS: frozenset[str] = frozenset()


def parse_jest_log(log: str) -> TestResult:
    log = _RE_ANSI.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(status: str, test_id: str) -> None:
        if test_id in KNOWN_FLAKY_TESTS:
            return
        if status == "PASS":
            # A test that failed earlier in the same log stays failed.
            if test_id in failed_tests:
                return
            skipped_tests.discard(test_id)
            passed_tests.add(test_id)
        elif status == "FAIL":
            passed_tests.discard(test_id)
            skipped_tests.discard(test_id)
            failed_tests.add(test_id)
        elif status == "SKIP":
            if test_id not in passed_tests and test_id not in failed_tests:
                skipped_tests.add(test_id)

    current_suite: str | None = None
    suite_status: str | None = None
    suite_had_tests = False

    def close_suite() -> None:
        # A FAIL suite that printed no test lines never ran anything. Record the
        # suite itself so it cannot silently vanish from the counts.
        if current_suite and suite_status == "FAIL" and not suite_had_tests:
            record("FAIL", f"{current_suite}::[suite failed]")

    for line in log.splitlines():
        line = line.rstrip()

        match = _RE_SUITE.match(line)
        if match:
            close_suite()
            suite_status = match.group(1)
            current_suite = match.group(2)
            suite_had_tests = False
            continue

        match = _RE_TEST.match(line)
        if match and current_suite:
            glyph = line.lstrip()[0]
            name = _RE_TIMING.sub("", match.group(1)).strip()
            if not name:
                continue
            suite_had_tests = True
            if glyph in _PASS_GLYPHS:
                status = "PASS"
            elif glyph in _FAIL_GLYPHS:
                status = "FAIL"
            elif glyph in _SKIP_GLYPHS:
                status = "SKIP"
            else:
                continue
            record(status, f"{current_suite}::{name}")

    close_suite()

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("WordPress", "gutenberg-examples")
class WordPressGutenbergExamples(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WordPressGutenbergExamplesImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_jest_log(test_log)
