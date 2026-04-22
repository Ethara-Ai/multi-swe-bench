import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase_11125_to_6935(Image):
    """Base image for nextcloud/mail PRs #6935-#11125 (2022-2025, main branch).

    Uses php:8.1-cli, Composer 2, Node.js installed per-PR in prepare.sh.
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
        return "php:8.1-cli"

    def image_tag(self) -> str:
        return "base-php8_1"

    def workdir(self) -> str:
        return "base-php8_1"

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
    && docker-php-ext-install zip intl gd pdo_sqlite

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

{code}
{self.clear_env}

"""


class ImageDefault_11125_to_6935(Image):
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
        return ImageBase_11125_to_6935(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _nc_stable_branch(self) -> str:
        if self.pr.number >= 11125:
            return "stable30"
        if self.pr.number >= 8231:
            return "stable27"
        return "stable25"

    def _node_major_version(self) -> int:
        if self.pr.number >= 8231:
            return 20
        return 16

    def files(self) -> list[File]:
        nc_branch = self._nc_stable_branch()
        node_version = self._node_major_version()

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

ARCH=$(dpkg --print-architecture)
if [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi
NODE_VER=$(curl -fsSL https://nodejs.org/dist/latest-v{node_version}.x/ | grep -oP 'node-v\\K[0-9.]+' | head -1)
curl -fsSL https://nodejs.org/dist/v$NODE_VER/node-v$NODE_VER-linux-$NODE_ARCH.tar.xz -o /tmp/node.tar.xz
tar -xf /tmp/node.tar.xz -C /usr/local --strip-components=1
rm /tmp/node.tar.xz

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
composer install --prefer-dist --no-progress --no-interaction || true

if grep -q '\\.spec\\.js' /home/test.patch 2>/dev/null; then
    npm install || true
fi

cd /home/core
php occ app:enable mail

""".format(pr=self.pr, nc_branch=nc_branch, node_version=node_version),
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
    npx jest src/tests/unit --verbose
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
    npx jest src/tests/unit --verbose
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
    npx jest src/tests/unit --verbose
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


@Instance.register("nextcloud", "mail_11125_to_6935")
class Mail_11125_to_6935(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_11125_to_6935(self.pr, self._config)

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

        # PHPUnit --testdox: ✔ pass, ✘ fail, ↩/∅/☢ skip/incomplete/risky
        phpunit_passed = re.compile(r"^\s*✔\s+(.+)$")
        phpunit_failed = re.compile(r"^\s*✘\s+(.+)$")
        phpunit_skipped = re.compile(r"^\s*[↩∅☢]\s+(.+)$")

        # Jest --verbose: ✓ pass, ✕/✗/× fail, PASS/FAIL file prefix
        jest_pass = re.compile(r"^\s*✓\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        jest_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        jest_file = re.compile(r"^\s*(PASS|FAIL)\s+(.+)$")
        jest_summary = re.compile(r"^(Test Suites|Tests|Snapshots|Time|Ran all)\s*:")

        current_phpunit_class = None
        current_jest_suite = None
        is_jest_section = False
        jest_summary_reached = False

        for line in test_log.splitlines():
            stripped = line.strip()

            if jest_summary.match(stripped):
                jest_summary_reached = True
                continue

            jest_file_match = jest_file.match(stripped)
            if jest_file_match:
                is_jest_section = True
                jest_summary_reached = False
                current_jest_suite = None
                continue

            # PHPUnit class: "Suite Name (Full\Namespace\Class)"
            if (
                stripped
                and not stripped.startswith(("✔", "✘", "↩", "∅", "☢", "│", "✓", "✕", "✗", "×"))
                and not re.match(r"^\d+\)", stripped)
                and "(" in stripped
                and ")" in stripped
                and stripped.endswith(")")
                and not is_jest_section
            ):
                current_phpunit_class = stripped
                continue

            if current_phpunit_class and not is_jest_section:
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

            # Jest results (stop at summary / failure details)
            if is_jest_section and not jest_summary_reached:
                jest_pass_match = jest_pass.match(stripped)
                if jest_pass_match:
                    test_name = jest_pass_match.group(1).strip()
                    if current_jest_suite:
                        passed_tests.add(f"{current_jest_suite} : {test_name}")
                    else:
                        passed_tests.add(test_name)
                    continue

                jest_fail_match = jest_fail.match(stripped)
                if jest_fail_match:
                    test_name = jest_fail_match.group(1).strip()
                    if current_jest_suite:
                        failed_tests.add(f"{current_jest_suite} : {test_name}")
                    else:
                        failed_tests.add(test_name)
                    continue

                # Jest suite: plain text, not a test result or ● failure detail
                if (
                    stripped
                    and not stripped.startswith(("✓", "✕", "✗", "×", "●"))
                    and not re.match(r"^(PASS|FAIL)\s+", stripped)
                ):
                    current_jest_suite = stripped
                    continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
