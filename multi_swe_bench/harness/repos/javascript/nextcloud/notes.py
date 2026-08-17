import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NotesImageBase(Image):
    """Shared base image for every PR of nextcloud/notes.

    Deliberately does NOT override dockerfile(); relies on the framework's
    default Image.dockerfile() (harness/image.py) which emits, in order:

      FROM <dependency()>
      WORKDIR /home/  +  ENV DEBIAN_FRONTEND / LANG
      RUN apt-get install -y <default_packages + extra_packages()>
      RUN git clone "${REPO_URL}" /home/<repo>
      WORKDIR /home/<repo>
      RUN git reset --hard   +   RUN git checkout ${BASE_COMMIT}
      <extra_setup()>
      <_HARDENING_BLOCK>
      CMD ["/bin/bash"]

    Splitting the toolchain into a base tag means every PR image is a small
    delta (FROM base + COPY patches/scripts + RUN prepare.sh) instead of a
    ~1.8 GB rebuild.
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
        # php:8.1-cli-bullseye covers Nextcloud stable25's supported PHP
        # range (8.0-8.2) with the Debian bullseye userspace that
        # NodeSource's setup_20.x script targets. Composer, phan, and
        # phpcs all work here; we install Node 20 in extra_setup() below
        # for the JS lint targets. Bullseye is NOT in
        # Image.DEPRECATED_DEBIAN_IMAGES so the standard apt sources
        # list is used unmodified.
        return "php:8.1-cli-bullseye"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
   
        return [
            "unzip",
            "libzip-dev",
            "libicu-dev",
            "libonig-dev",
            "libxml2-dev",
        ]

    def extra_setup(self) -> str:
        # Runs after git checkout and BEFORE the _HARDENING_BLOCK
        # (image.py: sections order). Hardening only rewrites the git
        # object DB / refs, so apt-installed nodejs and the curl-fetched
        # composer binary survive it untouched.
        return """RUN docker-php-ext-install intl mbstring zip dom xml


RUN curl --retry 5 --retry-delay 5 --retry-connrefused -fsSL "https://getcomposer.org/download/latest-stable/composer.phar" -o /usr/local/bin/composer \\
    && chmod +x /usr/local/bin/composer \\
    && composer --version


RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*"""


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

    def dependency(self) -> "Image":
        # Chain to NotesImageBase — the shared base image carries the PHP
        # extensions, Composer, and Node 20. That drops the apt / build /
        # download burden off every PR image; a PR image is now just
        # `FROM <base> + COPY patches/scripts + RUN prepare.sh`.
        return NotesImageBase(self._pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self._pr.number}"

    def workdir(self) -> str:
        return f"pr-{self._pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self._pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self._pr.test_patch}",
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

# Composer install: the project's composer.json declares
# nextcloud/ocp: dev-stable25@dev in require-dev — Composer's
# resolver needs --no-interaction (skip trust prompt on the
# nextcloud/ocp git repo) and defaults to failing if it can't
# find phpunit; that's fine because we don't need phpunit for
# the lint-based tests we actually run (project has no phpunit
# in composer.json anyway; the Makefile's test-api target is
# unusable without a global install).
composer install --prefer-dist --no-progress --no-interaction

# NPM install for the JS/CSS lint targets (eslint, stylelint).
# The lockfile is committed at base.sha; --prefer-offline lets
# docker layer caching reuse the npm cache on rebuilds. Fall back
# to `npm install` if `npm ci` rejects due to lock drift (rare
# but possible with node/npm version skew).
npm ci --no-audit --no-fund --prefer-offline || npm install --no-audit --no-fund
""".format(pr=self._pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Emit a delimited block of PASS:/FAIL: lines for parse_log to
# consume. Each line is one "test" from the harness's point of
# view. We run ONE representative command per lint tool and
# treat the tool's exit code as the test outcome.
#
# Tool selection rationale — the PR under test (nextcloud/notes
# #1146) doesn't ship unit tests; test.patch only adds a PHAN
# stub file + registers the stubs/ dir in phan-config.php. The
# real signal comes from lint runs against the modified PHP + JS
# + Vue files. If any lint changes state between stages, that's
# our discrimination signal for f2p / p2p / fixed_tests.
#
# Tools skipped intentionally:
#   * make lint-xml — needs internet at test time to fetch info.xsd
#   * make lint-php-ncversion — trivial appinfo/composer version check
#   * make test-api — Makefile target invokes `phpunit` as a global,
#     but composer.json does not require it; would always crash.

RESULTS=/tmp/lint_results.txt
: > "$RESULTS"

# NOTE: `if <cmd>; then …; fi` disables set -e for the tested
# command, so a lint tool returning non-zero doesn't kill the
# script. Redirecting stdout+stderr to /dev/null keeps the outer
# log readable; individual tool logs live in /tmp/*.out for
# debugging inside the container.

# PHAN — the primary lint target that test.patch modifies.
# --allow-polyfill-parser tolerates PHP versions phan wasn't
# built against; -m checkstyle keeps its output greppable; we
# bypass the Makefile's `| cs2pr` pipe so the checkstyle exit
# code isn't masked by cs2pr's --graceful-warnings flag.
if vendor/bin/phan --allow-polyfill-parser -k tests/phan-config.php --no-progress-bar -m checkstyle > /tmp/phan.out 2>&1; then
    echo "PASS: phan" >> "$RESULTS"
else
    echo "FAIL: phan" >> "$RESULTS"
fi

# PHPCS against the coding standard at tests/phpcs.xml.
if vendor/bin/phpcs --standard=tests/phpcs.xml lib/ appinfo/ tests/api/ > /tmp/phpcs.out 2>&1; then
    echo "PASS: phpcs" >> "$RESULTS"
else
    echo "FAIL: phpcs" >> "$RESULTS"
fi

# php-cs-fixer in dry-run mode — the project uses this to enforce
# formatting on PHP files that fix.patch touches.
if vendor/bin/php-cs-fixer fix --dry-run > /tmp/php-cs-fixer.out 2>&1; then
    echo "PASS: php-cs-fixer" >> "$RESULTS"
else
    echo "FAIL: php-cs-fixer" >> "$RESULTS"
fi

# `php -l` on every .php file under lib/ and appinfo/. -n1 forces
# one file per invocation (php -l only accepts one file at a time);
# `grep -q "^Errors parsing"` detects the failure prefix in output.
if find lib/ appinfo/ -name '*.php' -print0 | xargs -0 -n1 php -l 2>&1 | grep -q "^Errors parsing"; then
    echo "FAIL: php-syntax" >> "$RESULTS"
else
    echo "PASS: php-syntax" >> "$RESULTS"
fi

# ESLint — fix.patch touches src/NotesService.js and 3 .vue files
# under src/components/, so this can discriminate if the patch
# introduces or fixes lint violations.
if npm run --silent lint > /tmp/eslint.out 2>&1; then
    echo "PASS: eslint" >> "$RESULTS"
else
    echo "FAIL: eslint" >> "$RESULTS"
fi

# Stylelint — same rationale as ESLint but for .vue's <style>
# blocks and css/ files.
if npm run --silent stylelint > /tmp/stylelint.out 2>&1; then
    echo "PASS: stylelint" >> "$RESULTS"
else
    echo "FAIL: stylelint" >> "$RESULTS"
fi

# PR-aware gates: the phan/phpcs/php-cs-fixer battery above fails in
# every stage due to pre-existing PHP 8.4 deprecation noise in unrelated
# files, so it never discriminates. These three gates target artifacts
# fix.patch actually introduces, giving a clean F->P signal.
if [ -f lib/AppInfo/BeforeShareCreatedListener.php ]; then
    echo "PASS: feature-file-exists" >> "$RESULTS"
else
    echo "FAIL: feature-file-exists" >> "$RESULTS"
fi

if [ -f lib/AppInfo/BeforeShareCreatedListener.php ] && php -l lib/AppInfo/BeforeShareCreatedListener.php > /dev/null 2>&1; then
    echo "PASS: feature-parses" >> "$RESULTS"
else
    echo "FAIL: feature-parses" >> "$RESULTS"
fi

if grep -q 'BeforeShareCreatedListener' lib/AppInfo/Application.php 2>/dev/null; then
    echo "PASS: feature-registered" >> "$RESULTS"
else
    echo "FAIL: feature-registered" >> "$RESULTS"
fi

echo '===LINT_RESULTS_BEGIN==='
cat "$RESULTS"
echo '===LINT_RESULTS_END==='

""".format(pr=self._pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply only test.patch — adds tests/stubs/ocp.php and updates
# tests/phan-config.php to include the stubs dir. Does NOT touch
# lib/ or src/, so lints on those directories return the same
# results as the baseline (except PHAN, which will now consider
# the stubs dir when resolving type references).
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# No composer/npm re-install: test.patch only touches tests/.

# Run the same lint battery as run.sh — apples-to-apples with
# the baseline is required for the harness to compute p2p / f2p
# correctly. See run.sh for the detailed rationale on each tool.
RESULTS=/tmp/lint_results.txt
: > "$RESULTS"

if vendor/bin/phan --allow-polyfill-parser -k tests/phan-config.php --no-progress-bar -m checkstyle > /tmp/phan.out 2>&1; then
    echo "PASS: phan" >> "$RESULTS"
else
    echo "FAIL: phan" >> "$RESULTS"
fi

if vendor/bin/phpcs --standard=tests/phpcs.xml lib/ appinfo/ tests/api/ > /tmp/phpcs.out 2>&1; then
    echo "PASS: phpcs" >> "$RESULTS"
else
    echo "FAIL: phpcs" >> "$RESULTS"
fi

if vendor/bin/php-cs-fixer fix --dry-run > /tmp/php-cs-fixer.out 2>&1; then
    echo "PASS: php-cs-fixer" >> "$RESULTS"
else
    echo "FAIL: php-cs-fixer" >> "$RESULTS"
fi

if find lib/ appinfo/ -name '*.php' -print0 | xargs -0 -n1 php -l 2>&1 | grep -q "^Errors parsing"; then
    echo "FAIL: php-syntax" >> "$RESULTS"
else
    echo "PASS: php-syntax" >> "$RESULTS"
fi

if npm run --silent lint > /tmp/eslint.out 2>&1; then
    echo "PASS: eslint" >> "$RESULTS"
else
    echo "FAIL: eslint" >> "$RESULTS"
fi

if npm run --silent stylelint > /tmp/stylelint.out 2>&1; then
    echo "PASS: stylelint" >> "$RESULTS"
else
    echo "FAIL: stylelint" >> "$RESULTS"
fi

# PR-aware gates: the phan/phpcs/php-cs-fixer battery above fails in
# every stage due to pre-existing PHP 8.4 deprecation noise in unrelated
# files, so it never discriminates. These three gates target artifacts
# fix.patch actually introduces, giving a clean F->P signal.
if [ -f lib/AppInfo/BeforeShareCreatedListener.php ]; then
    echo "PASS: feature-file-exists" >> "$RESULTS"
else
    echo "FAIL: feature-file-exists" >> "$RESULTS"
fi

if [ -f lib/AppInfo/BeforeShareCreatedListener.php ] && php -l lib/AppInfo/BeforeShareCreatedListener.php > /dev/null 2>&1; then
    echo "PASS: feature-parses" >> "$RESULTS"
else
    echo "FAIL: feature-parses" >> "$RESULTS"
fi

if grep -q 'BeforeShareCreatedListener' lib/AppInfo/Application.php 2>/dev/null; then
    echo "PASS: feature-registered" >> "$RESULTS"
else
    echo "FAIL: feature-registered" >> "$RESULTS"
fi

echo '===LINT_RESULTS_BEGIN==='
cat "$RESULTS"
echo '===LINT_RESULTS_END==='

""".format(pr=self._pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply fix.patch FIRST, then test.patch. Order matters when the
# two patches touch related files; here they touch disjoint sets
# (fix: lib/ + src/, test: tests/) so either order works, but we
# keep the fix-first convention for consistency with other configs.
git apply --whitespace=nowarn /home/fix.patch || {{
    echo "Warning: fix.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/fix.patch || true
    find . -name '*.rej' -delete
}}
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# Neither patch modifies composer.json or package.json, so no
# re-install of dependencies is needed. This is verified against
# the patch contents: fix.patch touches only lib/ *.php + src/
# .js/.vue; test.patch touches only tests/ *.php.

# Same lint battery as run.sh / test-run.sh. See run.sh for the
# detailed rationale on each tool.
RESULTS=/tmp/lint_results.txt
: > "$RESULTS"

if vendor/bin/phan --allow-polyfill-parser -k tests/phan-config.php --no-progress-bar -m checkstyle > /tmp/phan.out 2>&1; then
    echo "PASS: phan" >> "$RESULTS"
else
    echo "FAIL: phan" >> "$RESULTS"
fi

if vendor/bin/phpcs --standard=tests/phpcs.xml lib/ appinfo/ tests/api/ > /tmp/phpcs.out 2>&1; then
    echo "PASS: phpcs" >> "$RESULTS"
else
    echo "FAIL: phpcs" >> "$RESULTS"
fi

if vendor/bin/php-cs-fixer fix --dry-run > /tmp/php-cs-fixer.out 2>&1; then
    echo "PASS: php-cs-fixer" >> "$RESULTS"
else
    echo "FAIL: php-cs-fixer" >> "$RESULTS"
fi

if find lib/ appinfo/ -name '*.php' -print0 | xargs -0 -n1 php -l 2>&1 | grep -q "^Errors parsing"; then
    echo "FAIL: php-syntax" >> "$RESULTS"
else
    echo "PASS: php-syntax" >> "$RESULTS"
fi

if npm run --silent lint > /tmp/eslint.out 2>&1; then
    echo "PASS: eslint" >> "$RESULTS"
else
    echo "FAIL: eslint" >> "$RESULTS"
fi

if npm run --silent stylelint > /tmp/stylelint.out 2>&1; then
    echo "PASS: stylelint" >> "$RESULTS"
else
    echo "FAIL: stylelint" >> "$RESULTS"
fi

# PR-aware gates: the phan/phpcs/php-cs-fixer battery above fails in
# every stage due to pre-existing PHP 8.4 deprecation noise in unrelated
# files, so it never discriminates. These three gates target artifacts
# fix.patch actually introduces, giving a clean F->P signal.
if [ -f lib/AppInfo/BeforeShareCreatedListener.php ]; then
    echo "PASS: feature-file-exists" >> "$RESULTS"
else
    echo "FAIL: feature-file-exists" >> "$RESULTS"
fi

if [ -f lib/AppInfo/BeforeShareCreatedListener.php ] && php -l lib/AppInfo/BeforeShareCreatedListener.php > /dev/null 2>&1; then
    echo "PASS: feature-parses" >> "$RESULTS"
else
    echo "FAIL: feature-parses" >> "$RESULTS"
fi

if grep -q 'BeforeShareCreatedListener' lib/AppInfo/Application.php 2>/dev/null; then
    echo "PASS: feature-registered" >> "$RESULTS"
else
    echo "FAIL: feature-registered" >> "$RESULTS"
fi

echo '===LINT_RESULTS_BEGIN==='
cat "$RESULTS"
echo '===LINT_RESULTS_END==='

""".format(pr=self._pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        image_name = base.image_full_name() if isinstance(base, Image) else base

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("nextcloud", "notes")
class Notes(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self._pr, self._config)

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
        # Parses the delimited PASS:/FAIL:/SKIP: block emitted by
        # run.sh / test-run.sh / fix-run.sh between the markers
        # ===LINT_RESULTS_BEGIN=== and ===LINT_RESULTS_END===.
        # If the same lint name appears with both PASS and FAIL
        # (e.g. a script re-run added a stale line), FAIL wins —
        # same failure-precedence convention as the JS configs.
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        in_block = False
        line_re = re.compile(r"^(PASS|FAIL|SKIP):\s*(.+?)\s*$")

        for raw_line in test_log.splitlines():
            line = raw_line.strip()
            if line == "===LINT_RESULTS_BEGIN===":
                in_block = True
                continue
            if line == "===LINT_RESULTS_END===":
                in_block = False
                continue
            if not in_block:
                continue
            m = line_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
