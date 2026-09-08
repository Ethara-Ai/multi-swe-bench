import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.babel.babel_dispatcher import (
    BabelSharedImageBase,
)

_BASE_TAG = "base-classic-mocha"
_NODE_MAJOR = "8"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_STAGE_TESTS = "/home/msweb-mocha.tests"
_REPORTER_JS = "/home/msweb_mocha_reporter.js"

_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _normalise_identity(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", name)).strip()


_HARDEN_BLOCK = """WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

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

_REPORTER_SRC = r"""var fs = require('fs');

var OUT = process.env.MSWEB_MOCHA_OUT || '/home/msweb-mocha.tests';
var lines = [];

var PREFIX = '__PREFIX__';

function ident(test) {
  var name = '';
  try {
    name = test.fullTitle ? test.fullTitle() : (test.title || '');
  } catch (e) {
    name = test.title || '';
  }
  var file = '';
  try {
    file = test && test.file ? String(test.file) : '';
    if (!file && test && test.parent && test.parent.file) {
      file = String(test.parent.file);
    }
  } catch (e) {
    file = '';
  }
  if (file) {
    file = file.replace(/\\/g, '/');
    var at = file.indexOf(PREFIX);
    file = at !== -1 ? file.slice(at + PREFIX.length) : file.replace(/^\/+/, '');
    name = file + '::' + name;
  }
  name = String(name).replace(/[^\x20-\x7e]/g, ' ').replace(/\s+/g, ' ');
  return name.replace(/^\s+|\s+$/g, '');
}

function record(status, test) {
  var name = ident(test);
  if (!name) {
    var file = test && test.file ? String(test.file) : '';
    name = (file || 'unknown') + '::<uncaught error outside test suite>';
  }
  lines.push('TESTCASE ' + status + ' ' + name);
}

function flush() {
  try {
    fs.writeFileSync(OUT, lines.length ? lines.join('\n') + '\n' : '');
  } catch (e) {
    process.stderr.write('msweb reporter: ' + e.message + '\n');
  }
}

module.exports = function (runner) {
  runner.on('pass', function (test) {
    record('PASSED', test);
  });
  runner.on('fail', function (test) {
    record('FAILED', test);
  });
  runner.on('pending', function (test) {
    record('SKIPPED', test);
  });
  runner.on('end', flush);
  process.on('exit', flush);
};
"""

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh

yarn install --ignore-engines || yarn --ignore-engines || true

if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi

make build || true
make test-clean || true

git checkout -- . 2>/dev/null || true
git clean -fd 2>/dev/null || true

node -e "require.resolve('mocha')"
node -e "require.resolve('babel-register')"
test -f Makefile
test -f node_modules/mocha/bin/_mocha
test -f scripts/_get-test-directories.sh
test -f test/mocha.opts
test -f __REPORTER__

bash /home/check_git_changes.sh
""".replace("__REPORTER__", _REPORTER_JS)

_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test
export FORCE_COLOR=0

cd /home/{repo}
test -f node_modules/mocha/bin/_mocha
"""

_GRADED_BODY = """
if git status --porcelain -uall | grep -qE 'package[.]json$'; then
    echo "applied patch changed a package.json; re-bootstrapping workspace"
    if [ -f node_modules/.bin/lerna ]; then
        ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
    fi
fi

make build || true

rm -f __TESTS__
mocha_status=0
MSWEB_MOCHA_OUT=__TESTS__ node node_modules/mocha/bin/_mocha \\
    $(sh scripts/_get-test-directories.sh) \\
    --opts test/mocha.opts \\
    --reporter __REPORTER__ || mocha_status=$?

if [ ! -s __TESTS__ ]; then
    echo "full-suite run reported nothing; retrying per test directory"
    : > __TESTS__
    for d in $(sh scripts/_get-test-directories.sh); do
        rm -f __PART__
        MSWEB_MOCHA_OUT=__PART__ node node_modules/mocha/bin/_mocha \\
            "$d" \\
            --opts test/mocha.opts \\
            --reporter __REPORTER__ > /dev/null 2>&1 || true
        if [ -s __PART__ ]; then
            cat __PART__ >> __TESTS__
        else
            echo "  no results from $d" >&2
        fi
    done
fi

echo "##### MSWEB-MOCHA-EXIT: $mocha_status"
cat __TESTS__ 2>/dev/null || true
make test-clean || true
""".replace("__TESTS__", _STAGE_TESTS).replace("__PART__", "/home/msweb-mocha.part").replace("__REPORTER__", _REPORTER_JS)


class BabelClassicMochaImageBase(Image):
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
        return BabelSharedImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        sections = [f"FROM {image_name}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(
            "RUN set -eux; \\\n"
            f'    test "$(node -p "process.versions.node.split(\'.\')[0]")" = "{_NODE_MAJOR}"; \\\n'
            "    command -v yarn; \\\n"
            f"    test -d /home/{self.pr.repo}/.git"
        )
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class BabelClassicMochaImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return BabelClassicMochaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        preamble = _SCRIPT_PREAMBLE.format(repo=self.pr.repo)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "msweb_mocha_reporter.js",
                _REPORTER_SRC.replace("__PREFIX__", f"/home/{self.pr.repo}/"),
            ),
            File(".", "prepare.sh", _PREPARE_SH.replace("__REPO__", self.pr.repo)),
            File(".", "run.sh", preamble + _GRADED_BODY),
            File(
                ".",
                "test-run.sh",
                preamble
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + _GRADED_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                preamble
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + _GRADED_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        harden_commands = _HARDEN_BLOCK.format(
            repo=self.pr.repo, sha=self.pr.base.sha
        )

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )

        sections = [f"FROM {name}:{tag}"]
        for part in (
            self.global_env,
            harden_commands,
            proxy_setup,
            copy_commands,
            prepare_commands,
            proxy_cleanup,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("babel", "babel_classic_mocha")
class babel_classic_mocha(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BabelClassicMochaImageDefault(self.pr, self._config)

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

        clean_log = _ANSI_ESCAPE.sub("", test_log or "").replace("\r", "")

        for line in clean_log.split("\n"):
            case = _TESTCASE_RE.match(line)
            if not case:
                continue
            status, name = case.group(1), _normalise_identity(case.group(2))
            if not name:
                continue
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if not (passed_tests or failed_tests or skipped_tests):
            fail_re = re.compile(r"^\s+(\d+)\)\s+(.+?)\s*:?\s*$")
            passing_re = re.compile(r"^\s+(\d+)\s+passing")
            pending_re = re.compile(r"^\s+(\d+)\s+pending")

            pass_count = 0
            skip_count = 0

            for line in clean_log.split("\n"):
                m = fail_re.match(line)
                if m:
                    failed_tests.add(_normalise_identity(m.group(2)))

                m = passing_re.match(line)
                if m:
                    pass_count = int(m.group(1))

                m = pending_re.match(line)
                if m:
                    skip_count = int(m.group(1))

            for i in range(pass_count):
                passed_tests.add(f"test_pass_{i + 1}")

            for i in range(skip_count):
                skipped_tests.add(f"test_pending_{i + 1}")

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
