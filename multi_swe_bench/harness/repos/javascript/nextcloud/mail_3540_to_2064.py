import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase_3540_to_2064(Image):
    """Base image for nextcloud/mail PRs #2064-#3540 (2019-2020, master branch).

    Uses php:7.4-cli, Composer 1 (Horde PEAR repos), Node.js 14 (mochapack + node-sass).
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
        return "php:7.4-cli"

    def image_tag(self) -> str:
        return "base-php7_4"

    def workdir(self) -> str:
        return "base-php7_4"

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

        return f"""FROM {image_name}



WORKDIR /home/

{self.global_env}

RUN apt-get update && apt-get install -y \
    git \
    unzip \
    libzip-dev \
    libicu-dev \
    libpng-dev \
    libsqlite3-dev \
    python2 \
    make \
    g++ \
    && docker-php-ext-install zip intl gd pdo_sqlite

RUN curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer --1

RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi && \
    curl -fsSL https://nodejs.org/dist/v14.21.3/node-v14.21.3-linux-$NODE_ARCH.tar.xz -o /tmp/node.tar.xz && \
    tar -xf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm /tmp/node.tar.xz

{code}
{self.clear_env}

"""


class ImageDefault_3540_to_2064(Image):
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
        return ImageBase_3540_to_2064(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _nc_stable_branch(self) -> str:
        if self.pr.number >= 3540:
            return "stable20"
        return "stable18"

    def files(self) -> list[File]:
        nc_branch = self._nc_stable_branch()

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

git clone --recursive --depth 1 -b {nc_branch} https://github.com/nextcloud/server.git /home/core

cd /home/core
php occ maintenance:install --database sqlite --admin-user admin --admin-pass admin
php occ app:check-code mail || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cp -r /home/{pr.repo} /home/core/apps/mail

cd /home/core/apps/mail
composer install --prefer-dist --no-progress --no-suggest || true

if grep -q '\\.spec\\.js' /home/test.patch 2>/dev/null; then
    npm install || true
fi

cd /home/core
php occ app:enable mail

""".format(pr=self.pr, nc_branch=nc_branch),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/core/apps/mail

HAS_PHP_TESTS=false
HAS_JS_TESTS=false

if grep -q 'tests/.*\\.php' /home/test.patch 2>/dev/null; then
    HAS_PHP_TESTS=true
fi
if grep -q '\\.spec\\.js' /home/test.patch 2>/dev/null; then
    HAS_JS_TESTS=true
fi

if [ "$HAS_PHP_TESTS" = true ]; then
    php -d memory_limit=512M vendor/bin/phpunit -c tests/phpunit.unit.xml tests/Unit --testdox --no-coverage --fail-on-warning
fi

if [ "$HAS_JS_TESTS" = true ]; then
    npm test
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/core/apps/mail
git apply /home/test.patch

HAS_PHP_TESTS=false
HAS_JS_TESTS=false

if grep -q 'tests/.*\\.php' /home/test.patch 2>/dev/null; then
    HAS_PHP_TESTS=true
fi
if grep -q '\\.spec\\.js' /home/test.patch 2>/dev/null; then
    HAS_JS_TESTS=true
fi

if [ "$HAS_PHP_TESTS" = true ]; then
    php -d memory_limit=512M vendor/bin/phpunit -c tests/phpunit.unit.xml tests/Unit --testdox --no-coverage --fail-on-warning
fi

if [ "$HAS_JS_TESTS" = true ]; then
    npm test
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/core/apps/mail
git apply /home/test.patch /home/fix.patch

HAS_PHP_TESTS=false
HAS_JS_TESTS=false

if grep -q 'tests/.*\\.php' /home/test.patch 2>/dev/null; then
    HAS_PHP_TESTS=true
fi
if grep -q '\\.spec\\.js' /home/test.patch 2>/dev/null; then
    HAS_JS_TESTS=true
fi

if [ "$HAS_PHP_TESTS" = true ]; then
    php -d memory_limit=512M vendor/bin/phpunit -c tests/phpunit.unit.xml tests/Unit --testdox --no-coverage --fail-on-warning
fi

if [ "$HAS_JS_TESTS" = true ]; then
    npm test
fi

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


@Instance.register("nextcloud", "mail_3540_to_2064")
class Mail_3540_to_2064(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_3540_to_2064(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # PHPUnit --testdox: ✔ pass, ✘ fail, ↩ skip
        phpunit_passed = re.compile(r"^\s*✔\s+(.+)$")
        phpunit_failed = re.compile(r"^\s*✘\s+(.+)$")
        phpunit_skipped = re.compile(r"^\s*↩\s+(.+)$")

        # Mocha/mochapack spec reporter: ✓ pass, N) fail (inline)
        mocha_passed = re.compile(r"^\s*✓\s+(.+)$")
        mocha_failed = re.compile(r"^\s*\d+\)\s+(.+)$")

        current_phpunit_class = None
        mocha_suite_stack = []
        mocha_summary_reached = False

        for line in test_log.splitlines():
            stripped = line.strip()

            # Stop mocha parsing at summary line (prevents double-counting
            # from failure details section that follows)
            if re.match(r"^\s*\d+\s+passing", stripped):
                mocha_summary_reached = True
                continue
            if re.match(r"^\s*\d+\s+failing", stripped):
                mocha_summary_reached = True
                continue

            # PHPUnit class detection: "Suite Name (Full\Namespace\Class)"
            if (
                stripped
                and not stripped.startswith(("✔", "✘", "↩", "∅", "│", "✓"))
                and not re.match(r"^\d+\)", stripped)
                and "(" in stripped
                and ")" in stripped
                and stripped.endswith(")")
            ):
                current_phpunit_class = stripped
                continue

            # PHPUnit test results
            if current_phpunit_class:
                passed_match = phpunit_passed.match(stripped)
                if passed_match:
                    test_name = passed_match.group(1).strip()
                    passed_tests.add(f"{current_phpunit_class} : {test_name}")
                    continue

                failed_match = phpunit_failed.match(stripped)
                if failed_match:
                    test_name = failed_match.group(1).strip()
                    failed_tests.add(f"{current_phpunit_class} : {test_name}")
                    continue

                skipped_match = phpunit_skipped.match(stripped)
                if skipped_match:
                    test_name = skipped_match.group(1).strip()
                    skipped_tests.add(f"{current_phpunit_class} : {test_name}")
                    continue

            # Mocha results (only before summary to avoid failure details)
            if not mocha_summary_reached:
                mocha_pass_match = mocha_passed.match(stripped)
                if mocha_pass_match:
                    test_name = mocha_pass_match.group(1).strip()
                    suite = mocha_suite_stack[-1] if mocha_suite_stack else None
                    if suite:
                        passed_tests.add(f"{suite} : {test_name}")
                    else:
                        passed_tests.add(test_name)
                    continue

                mocha_fail_match = mocha_failed.match(stripped)
                if mocha_fail_match:
                    test_name = mocha_fail_match.group(1).strip()
                    suite = mocha_suite_stack[-1] if mocha_suite_stack else None
                    if suite:
                        failed_tests.add(f"{suite} : {test_name}")
                    else:
                        failed_tests.add(test_name)
                    continue

                # Mocha suite detection: non-empty lines that aren't test
                # results, appearing before any summary. Track by indent level.
                if (
                    stripped
                    and not stripped.startswith(("✓", "✔", "✘", "↩", "∅", "│"))
                    and not re.match(r"^\d+\)", stripped)
                    and not re.match(r"^\d+\s+(passing|failing|pending)", stripped)
                    and "(" not in stripped
                ):
                    indent = len(line) - len(line.lstrip())
                    # Pop suites at same or deeper indent level
                    while mocha_suite_stack and len(mocha_suite_stack) > indent // 2:
                        mocha_suite_stack.pop()
                    mocha_suite_stack.append(stripped)
                    continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
