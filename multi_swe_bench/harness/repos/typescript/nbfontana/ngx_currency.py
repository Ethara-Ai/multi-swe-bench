import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NgxCurrencyImageBase(Image):
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
        return "node:12"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # R11: node:12 is Debian stretch, which left deb.debian.org years ago;
        # a plain `apt-get update` 404s and the build dies with exit 100. The
        # stretch-updates suite was never archived at all, so it is dropped
        # rather than rewritten.
        apt = """RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/stretch-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends \\
        git ca-certificates chromium \\
    && rm -rf /var/lib/apt/lists/*"""

        # The clone belongs to this shared base image. Because dependency()
        # returns a string, DockerfileEnhancer rewrites the hardcoded clone
        # below into `git clone "${REPO_URL}"` + `git checkout ${BASE_COMMIT}`
        # + the history-hardening block + CMD, and build_dataset supplies both
        # build args. It must come last so the toolchain layers stay cacheable.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
{apt}
RUN printf '#!/bin/sh\\nexec /usr/bin/chromium --no-sandbox --disable-gpu --disable-dev-shm-usage --headless "$@"\\n' \\
        > /usr/local/bin/chromium-nosandbox && chmod +x /usr/local/bin/chromium-nosandbox
ENV CHROME_BIN=/usr/local/bin/chromium-nosandbox

RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}

{self.clear_env}

"""


class NgxCurrencyImageDefault(Image):
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
        return NgxCurrencyImageBase(self.pr, self.config)

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
                "karma.mswb.js",
                """const path = require('path');

const REPO_DIR = '/home/{pr.repo}';

require(path.join(REPO_DIR, 'node_modules', 'ts-node')).register({{
  project: path.join(REPO_DIR, 'tsconfig.json'),
  transpileOnly: true,
  compilerOptions: {{ module: 'commonjs' }},
}});

module.exports = function (config) {{
  // The repository's karma.conf.ts keys three webpack settings off `singleRun`:
  // ts-loader leaves `transpileOnly`, tslint-loader turns on emitErrors and
  // failOnHint, and NoEmitOnErrorsPlugin replaces ForkTsCheckerWebpackPlugin.
  // Under `karma start --single-run` the test patch's calls into a method that
  // does not exist yet therefore stop the bundle from ever being emitted and
  // karma waits forever instead of reporting failures. Load the repository
  // config with `singleRun` still falsy, then drop the two remaining
  // type/lint gates so a missing method surfaces as a failing test.
  config.singleRun = false;
  require(path.join(REPO_DIR, 'karma.conf.ts')).default(config);

  const webpackConfig = config.webpack;
  webpackConfig.module.rules = webpackConfig.module.rules.filter(
    (rule) => rule.loader !== 'tslint-loader'
  );
  webpackConfig.plugins = webpackConfig.plugins.filter(
    (plugin) => plugin.constructor.name !== 'ForkTsCheckerWebpackPlugin'
  );

  config.set({{
    basePath: REPO_DIR,
    webpack: webpackConfig,
    reporters: ['mocha'],
    mochaReporter: {{ showDiff: false, output: 'full' }},
    browsers: ['ChromeHeadlessNoSandbox'],
    autoWatch: false,
    singleRun: true,
  }});

  config.singleRun = true;
  config.autoWatch = false;
}};

""".format(pr=self.pr),
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
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

npm install --no-audit --no-fund || true
npx karma start /home/karma.mswb.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npx karma start /home/karma.mswb.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
npx karma start /home/karma.mswb.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
npx karma start /home/karma.mswb.js

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


@Instance.register("nbfontana", "ngx-currency")
class NgxCurrency(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NgxCurrencyImageDefault(self.pr, self._config)

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

        pass_regex = re.compile(r"^[\u2713\u2714]\s+(.+)$")
        fail_regex = re.compile(r"^[\u2717\u2716\u00d7]\s+(.+)$")
        skip_regex = re.compile(r"^[-\u2013]\s+(.+)$")
        suite_regex = re.compile(r"^[A-Za-z][^:]*$")

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        # karma-mocha-reporter indents each describe level by two spaces and
        # prints its tests two further in, so a test's suite chain is every
        # frame currently on the stack at a smaller indent. Every failure is
        # already listed inline above the "FAILED TESTS:" banner; the block
        # below that banner repeats them interleaved with assertion text and
        # webpack stack frames at those same indents, which would push
        # "at Context.<anonymous> (webpack:///test/...)" onto the stack and
        # put a source path inside a test name. Stop at the banner.
        suite_stack: list[tuple[int, str]] = []

        for line in clean_log.splitlines():
            if line.startswith("FAILED TESTS:"):
                break

            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())

            for regex, bucket in (
                (pass_regex, passed_tests),
                (fail_regex, failed_tests),
                (skip_regex, skipped_tests),
            ):
                match = regex.match(stripped)
                if not match:
                    continue
                parents = [title for depth, title in suite_stack if depth < indent]
                if not parents:
                    # A tick at column zero is the run summary
                    # ("16 tests completed"), never a test.
                    break
                # R20: report.py resolves a name to its file with
                # startswith(file + " > "), so the repo-relative spec path has
                # to lead. Both spec files are the kebab-cased top-level
                # describe title with the "Testing " prefix removed:
                # "Testing InputService" -> test/input-service.spec.ts.
                spec = re.sub(r"^Testing\s+", "", parents[0]).strip()
                spec = re.sub(r"(?<!^)(?=[A-Z])", "-", spec).lower()
                bucket.add(
                    " > ".join(
                        [f"test/{spec}.spec.ts", *parents, match.group(1).strip()]
                    )
                )
                break
            else:
                if indent >= 2 and suite_regex.match(stripped):
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    suite_stack.append((indent, stripped))

        # R2 — the sets MUST be disjoint or TestResult raises. Failure wins.
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
