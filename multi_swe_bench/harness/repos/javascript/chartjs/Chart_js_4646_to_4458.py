import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "node:10-buster"

TEST_CMD = "./node_modules/.bin/gulp unittest"


class ChartJsImageBase(Image):
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
        return "base-pr-4458-to-4646"

    def workdir(self) -> str:
        return "base-pr-4458-to-4646"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}
{self.clear_env}
"""


class ChartJsImageDefault(Image):
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
        return ChartJsImageBase(self.pr, self._config)

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
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: clean tree at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

APT_OPTS="-o Acquire::Retries=8 -o Acquire::http::Timeout=60 -o Acquire::ftp::Timeout=60"

if ! apt-get $APT_OPTS update; then
    sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
           -e 's|security.debian.org|archive.debian.org|g' \\
           -e '/buster-updates/d' /etc/apt/sources.list
    apt-get $APT_OPTS -o Acquire::Check-Valid-Until=false update
fi

if ! apt-get $APT_OPTS install -y --no-install-recommends chromium fonts-liberation; then
    apt-get $APT_OPTS -o Acquire::Check-Valid-Until=false update
    apt-get $APT_OPTS install -y --no-install-recommends --fix-missing chromium fonts-liberation
fi
rm -rf /var/lib/apt/lists/*

printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"\\n' \\
    > /usr/local/bin/chromium-no-sandbox
chmod +x /usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true

npm install --no-save karma-spec-reporter@0.0.32
if [ ! -d node_modules/karma-spec-reporter ]; then
    echo "ERROR: karma-spec-reporter did not install" >&2
    exit 1
fi

sed -i "s/'progress'/'spec'/g" karma.conf.js
if ! grep -q "'spec'" karma.conf.js; then
    echo "ERROR: reporter swap did not apply to karma.conf.js" >&2
    exit 1
fi

sed -i "s/browsers: \\['Firefox'\\]/browsers: []/g" karma.conf.js
sed -i "s/config.browsers.push('Chrome');/config.browsers.push('ChromeHeadless');/g" karma.conf.js
if ! grep -q "ChromeHeadless" karma.conf.js; then
    echo "ERROR: browser swap did not apply to karma.conf.js" >&2
    exit 1
fi
if grep -qE "browsers: \\['Firefox'\\]|push\\('Chrome'\\)" karma.conf.js; then
    echo "ERROR: a non-headless browser is still configured in karma.conf.js" >&2
    exit 1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
{test_cmd} 2>&1
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch
{test_cmd} 2>&1
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch
{test_cmd} 2>&1
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh
"""


@Instance.register("chartjs", "Chart_js_4646_to_4458")
class CHART_JS_4646_TO_4458(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ChartJsImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_spec = re.compile(r"^(\s*)(✓|√|✗|×|x|-)\s+(.*\S)\s*$")
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
                elif mark in ("✗", "×", "x"):
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
                if last_spec_indent is not None and indent >= last_spec_indent:
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
