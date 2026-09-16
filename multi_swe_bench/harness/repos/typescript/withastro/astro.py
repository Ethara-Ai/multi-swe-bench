import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:14.21.3-bullseye"
YARN_VERSION = "1.22.10"

_PKG_DIR = "packages/astro"

_RESOLVE = [
    "mocha",
    "chai",
    "cheerio",
    "execa",
    "node-fetch",
    "magic-string",
    "vite",
    "esbuild",
]

_DIST_FILES = [
    "packages/astro/dist/config.js",
    "packages/astro/dist/build/index.js",
    "packages/astro/dist/preview/index.js",
    "packages/astro/dist/runtime/vite/config.js",
    "packages/astro-parser/dist/index.js",
    "packages/markdown/remark/dist/index.js",
]

_REPORT_DONE = "/tmp/mocha-report.done"

_TEST_CMD = (
    "yarn workspace astro run build\n"
    f"cd {_PKG_DIR}\n"
    "completed=0\n"
    "for f in test/*.test.js; do\n"
    f"    rm -f {_REPORT_DONE}\n"
    "    rc=0\n"
    f'    MSB_TEST_FILE="$f" MSB_REPORT_DONE={_REPORT_DONE} timeout 900 node_modules/.bin/mocha --timeout 60000 --exit --reporter /home/mocha-report.cjs "$f" || rc=$?\n'
    f"    if [ -s {_REPORT_DONE} ]; then\n"
    "        completed=$((completed + 1))\n"
    "    else\n"
    '        echo "FAILED $f > <file failed to run>"\n'
    '        echo "    mocha exited with code $rc before the run finished"\n'
    "    fi\n"
    "done\n"
    'echo "mocha-report: $completed files completed"\n'
    'if [ "$completed" -eq 0 ]; then\n'
    '    echo "no spec file ran to completion" >&2\n'
    "    exit 1\n"
    "fi\n"
)

_SCRIPT_HEADER = (
    "#!/bin/bash\nset -eo pipefail\n\n"
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n\n"
)

_MOCHA_REPORT_JS = r"""'use strict';
const fs = require('fs');
const path = require('path');

const Mocha = require(require.resolve('mocha', { paths: [process.cwd()] }));
const Base = Mocha.reporters.Base;
const { EVENT_TEST_PASS, EVENT_TEST_FAIL, EVENT_TEST_PENDING, EVENT_RUN_END } = Mocha.Runner.constants;

const ROOT = process.cwd() + path.sep;
const FILE = process.env.MSB_TEST_FILE || '<unknown file>';
const DONE = process.env.MSB_REPORT_DONE || '__DONE__';

function MsbReporter(runner, options) {
    Base.call(this, runner, options);
    const stats = this.stats;

    const names = new Map();
    const counts = new Map();

    const nameOf = (runnable) => {
        if (names.has(runnable)) return names.get(runnable);
        let file = String(runnable.file || (runnable.parent && runnable.parent.file) || FILE);
        if (file.startsWith(ROOT)) file = file.slice(ROOT.length);
        const parts = [file].concat(runnable.titlePath());
        let name = parts.map((p) => String(p).replace(/\s*[\r\n]+\s*/g, ' ')).join(' > ');
        const n = (counts.get(name) || 0) + 1;
        counts.set(name, n);
        if (n > 1) name += ' #' + n;
        names.set(runnable, name);
        return name;
    };

    runner.on(EVENT_TEST_PASS, (test) => console.log('PASSED ' + nameOf(test)));
    runner.on(EVENT_TEST_PENDING, (test) => console.log('SKIPPED ' + nameOf(test)));
    runner.on(EVENT_TEST_FAIL, (test, err) => {
        console.log('FAILED ' + nameOf(test));
        const msg = String((err && (err.stack || err.message)) || err).split('\n').slice(0, 6);
        for (const line of msg) console.log('    ' + line);
    });
    runner.once(EVENT_RUN_END, () => {
        const summary = 'mocha-report: ' + FILE + ' tests=' + stats.tests + ' passes=' + stats.passes +
            ' failures=' + stats.failures + ' pending=' + stats.pending;
        console.log(summary);
        fs.writeFileSync(DONE, summary + '\n');
    });
}

module.exports = MsbReporter;
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


class AstroImageBase(Image):
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
        return NODE_IMAGE

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

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN rm -f /usr/local/bin/yarn /usr/local/bin/yarnpkg \\
    && npm install -g yarn@{YARN_VERSION} \\
    && test "$(yarn --version)" = "{YARN_VERSION}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class AstroImageDefault(Image):
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
        return AstroImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        return _TEST_CMD.replace("__REPO__", self.pr.repo)

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        resolve_js = ", ".join(f"'{m}'" for m in _RESOLVE)
        dist_checks = "".join(f"test -f {p}\n" for p in _DIST_FILES)
        mocha_report_js = _MOCHA_REPORT_JS.replace("__DONE__", _REPORT_DONE)

        return (
            f"{_SCRIPT_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            "# Base commits on the deleted `next` branch are reachable from no ref a clone fetches.\n"
            "# Fetch the commit itself by SHA (never the PR head ref, which would bring the fix in).\n"
            f'if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then\n'
            "    fetched=0\n"
            "    for attempt in 1 2 3; do\n"
            f"        if git fetch --no-tags origin {sha}; then fetched=1; break; fi\n"
            '        echo "prepare: git fetch attempt $attempt failed; retrying in 15s"\n'
            "        sleep 15\n"
            "    done\n"
            '    if [ "$fetched" != 1 ]; then\n'
            f'        echo "prepare: could not fetch base commit {sha}" >&2\n'
            "        exit 1\n"
            "    fi\n"
            "    rm -f .git/FETCH_HEAD\n"
            "fi\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "# Result reporter the run scripts pass to mocha; written outside the repo. It is .cjs\n"
            "# because packages/astro is \"type\": \"module\".\n"
            "cat > /home/mocha-report.cjs <<'__MOCHA_REPORT_EOF__'\n"
            f"{mocha_report_js}"
            "__MOCHA_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "yarn --version\n"
            "\n"
            "# Same flags as the repo's CI job. --ignore-engines: a few example/docs workspaces\n"
            "# declare engines the pinned node does not satisfy, and none of them is loaded by the\n"
            "# graded suite. Chromium is only for the docs workspace's pa11y-ci (puppeteer 1.19,\n"
            "# which ships no linux/arm64 build); nothing the graded suite loads uses it.\n"
            "export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1\n"
            "installed=0\n"
            "for attempt in 1 2 3; do\n"
            "    if yarn install --frozen-lockfile --ignore-engines --non-interactive --network-timeout 600000; then installed=1; break; fi\n"
            '    echo "prepare: yarn install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'if [ "$installed" != 1 ]; then\n'
            '    echo "prepare: yarn install --frozen-lockfile failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "# astro's test-utils import its dist/, which needs @astrojs/parser and\n"
            "# @astrojs/markdown-remark built too. All build output is gitignored.\n"
            "yarn build:core\n"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, the runner and every module the specs and the fix load (magic-string is\n"
            "# only reachable through workspace hoisting), the built dist the specs import, and a dry\n"
            "# run proving every spec file loads under the reporter.\n"
            "test -s /home/mocha-report.cjs\n"
            "command -v timeout\n"
            f"{dist_checks}"
            f"cd /home/{repo}/{_PKG_DIR}\n"
            "test -x node_modules/.bin/mocha\n"
            f'node -e "for (const m of [{resolve_js}]) console.log(m + \' -> \' + require.resolve(m))"\n'
            "node_modules/.bin/mocha --version\n"
            f"rm -f {_REPORT_DONE}\n"
            "node_modules/.bin/mocha --dry-run --reporter /home/mocha-report.cjs 'test/*.test.js' > /tmp/mocha-dry-run.log\n"
            f"test -s {_REPORT_DONE}\n"
            "tail -n 1 /tmp/mocha-dry-run.log\n"
            "dry_tests=$(grep -c '^PASSED ' /tmp/mocha-dry-run.log)\n"
            'echo "mocha --dry-run: $dry_tests tests"\n'
            'test "$dry_tests" -gt 0\n'
            f"rm -f {_REPORT_DONE} /tmp/mocha-dry-run.log\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{self._test_cmd()}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{self._test_cmd()}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{self._test_cmd()}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("withastro", "astro")
class Astro(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AstroImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*)$")

        for line in log.splitlines():
            m = result_re.match(line.rstrip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
