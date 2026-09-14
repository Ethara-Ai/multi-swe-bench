from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:14-bullseye"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MARKER_RE = re.compile(r"^(TESTPASS|TESTFAIL|TESTSKIP) (.+?)\s*$")

_LENIENT_TSCONFIG = """\
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmitOnError": false,
    "noUnusedLocals": false
  }
}
"""

_REPORTER_JS = """\
const path = require('path');

const SRC_DIR = 'packages/polling/tests/src/';

function identify(test) {
  const file = test && test.file ? path.basename(test.file) : 'index.spec.js';
  const source = SRC_DIR + file.replace(/\\.js$/, '.ts');
  const title = typeof test.fullTitle === 'function' ? test.fullTitle() : String(test.title);
  return source + '::' + title;
}

module.exports = function FlatReporter(runner) {
  runner.on('pass', function (test) {
    console.log('TESTPASS ' + identify(test));
  });
  runner.on('fail', function (test) {
    console.log('TESTFAIL ' + identify(test));
  });
  runner.on('pending', function (test) {
    console.log('TESTSKIP ' + identify(test));
  });
};
"""

_BUILD_AND_TEST = """\
rm -rf packages/polling/lib packages/polling/tests/build
find packages -name "*.tsbuildinfo" -delete
node_modules/.bin/tsc -b packages/polling packages/algorithm
node_modules/.bin/tsc -p packages/polling/tests/tsconfig.lenient.json || echo "tsc: the test build reported type diagnostics"
test -f packages/polling/tests/build/index.spec.js || { echo "run: the test build emitted no javascript"; exit 1; }
node_modules/.bin/mocha --reporter /home/flat-reporter.js --timeout 20000 --exit $(ls -r packages/polling/tests/build/*.spec.js)
"""


class LuminoImageBase(Image):
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
        return _NODE_IMAGE

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class LuminoImageDefault(Image):
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
        return LuminoImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 || true

cat > packages/polling/tests/tsconfig.lenient.json << 'LENIENTEOF'
{_LENIENT_TSCONFIG}LENIENTEOF

cat > /home/flat-reporter.js << 'REPORTEREOF'
{_REPORTER_JS}REPORTEREOF

test -x node_modules/.bin/mocha || {{ echo "prepare.sh: mocha was not installed"; exit 1; }}
test -x node_modules/.bin/tsc || {{ echo "prepare.sh: typescript was not installed"; exit 1; }}
test -e node_modules/@lumino/polling || {{ echo "prepare.sh: the yarn workspace link for @lumino/polling is missing"; exit 1; }}
node -e "require('chai'); require('mocha'); console.log('DEPS_OK')"
node_modules/.bin/tsc -b packages/polling packages/algorithm
node_modules/.bin/tsc -p packages/polling/tests/tsconfig.lenient.json
test -f packages/polling/tests/build/index.spec.js || {{ echo "prepare.sh: the baseline test build emitted no javascript"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
{_BUILD_AND_TEST}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
{_BUILD_AND_TEST}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_BUILD_AND_TEST}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("LuminoImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("jupyterlab", "lumino")
class Lumino(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return LuminoImageDefault(self.pr, self._config)

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

        log = _ANSI_RE.sub("", test_log)

        for line in log.splitlines():
            match = _MARKER_RE.match(line)
            if not match:
                continue
            tag, name = match.group(1), match.group(2)
            if tag == "TESTPASS":
                passed_tests.add(name)
            elif tag == "TESTFAIL":
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
