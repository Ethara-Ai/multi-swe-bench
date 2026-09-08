import json
import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.babel.babel_dispatcher import (
    BabelSharedImageBase,
)

_BASE_TAG = "base-classic-jest"
_NODE_MAJOR = "8"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_JSON_BEGIN = "##### MSWEB-JEST-JSON-BEGIN"
_JSON_END = "##### MSWEB-JEST-JSON-END"

_JEST_JSON = "/home/msweb-jest.json"
_JEST_EMIT_JS = "/home/msweb_jest_emit.js"

_LINK_WORKSPACE = """for d in packages/*/; do
    [ -f "${d}package.json" ] || continue
    name=$(node -p "try{JSON.parse(require('fs').readFileSync('${d}package.json','utf8')).name||''}catch(e){''}" 2>/dev/null)
    [ -n "$name" ] || continue
    target="node_modules/$name"
    [ -e "$target" ] && continue
    mkdir -p "$(dirname "$target")"
    ln -s "$PWD/${d%/}" "$target" 2>/dev/null || true
done
"""

_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")

_EMIT_SRC = r'''var fs = require('fs');

var PREFIX = '__PREFIX__';
var out = [];

function norm(s) {
  return String(s)
    .replace(/[^\x20-\x7e]/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/^ | $/g, '');
}

var report;
try {
  report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
} catch (e) {
  process.stderr.write('msweb jest emit: ' + e.message + '\n');
  process.exit(0);
}

var suites = report.testResults || [];
for (var i = 0; i < suites.length; i++) {
  var suite = suites[i];
  var full = String(suite.name || '').replace(/\\/g, '/');
  var at = full.indexOf(PREFIX);
  var rel = at !== -1 ? full.slice(at + PREFIX.length) : full.replace(/^\/+/, '');
  var cases = suite.assertionResults || [];
  if (cases.length === 0 && suite.status === 'failed') {
    out.push('TESTCASE FAILED ' + norm(rel + '::<test suite failed to run>'));
    continue;
  }
  for (var k = 0; k < cases.length; k++) {
    var c = cases[k];
    var title = c.fullName || c.title || '';
    if (!title) continue;
    var st = c.status === 'passed' ? 'PASSED'
           : c.status === 'failed' ? 'FAILED' : 'SKIPPED';
    out.push('TESTCASE ' + st + ' ' + norm(rel + '::' + title));
  }
}

process.stdout.write(out.length ? out.join('\n') + '\n' : '');
'''


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

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh

if ! yarn install --ignore-engines --frozen-lockfile; then
    sed -i '/@lerna.*collect-updates/d' package.json || true
    sed -i '/nicolo-ribaudo\\/lerna/d' package.json || true
    sed -i '/collect-updates@https/,/^[^[:space:]]/ { /^[^[:space:]]/!d; /collect-updates@https/d }' yarn.lock || true
    yarn install --ignore-engines || true
fi

if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi

make build || true
make test-clean || true

git checkout -- . 2>/dev/null || true
git clean -fd 2>/dev/null || true

node -e "require.resolve('jest')"
node -e "require.resolve('babel-jest')"
test -f Makefile
test -f node_modules/jest/bin/jest.js

bash /home/check_git_changes.sh
"""

_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test
export FORCE_COLOR=0

cd /home/{repo}
test -f node_modules/jest/bin/jest.js
"""

_GRADED_BODY = """
__LINK_WORKSPACE__
if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi

__LINK_WORKSPACE__
make build || true

jest_status=0
rm -f {json}
node node_modules/jest/bin/jest.js --maxWorkers=2 --ci --json --outputFile={json} || jest_status=$?
echo "##### MSWEB-JEST-EXIT: $jest_status"
node {emit} {json} || true
""".format(json=_JEST_JSON, emit=_JEST_EMIT_JS).replace("__LINK_WORKSPACE__", _LINK_WORKSPACE)


class BabelClassicJestImageBase(Image):
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


class BabelClassicJestImageDefault(Image):
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
        return BabelClassicJestImageBase(self.pr, self._config)

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
                "msweb_jest_emit.js",
                _EMIT_SRC.replace("__PREFIX__", f"/home/{self.pr.repo}/"),
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


@Instance.register("babel", "babel_classic_jest")
class babel_classic_jest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BabelClassicJestImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        text = _ANSI_ESCAPE.sub("", test_log or "").replace("\r", "")

        for line in text.split("\n"):
            case = _TESTCASE_RE.match(line)
            if not case:
                continue
            status, name = case.group(1), _normalise_identity(case.group(2))
            if not name:
                continue
            if status == "FAILED":
                failed.add(name)
            elif status == "SKIPPED":
                skipped.add(name)
            else:
                passed.add(name)

        if passed or failed or skipped:
            passed -= failed
            passed -= skipped
            skipped -= failed
            return TestResult(
                passed_count=len(passed),
                failed_count=len(failed),
                skipped_count=len(skipped),
                passed_tests=passed,
                failed_tests=failed,
                skipped_tests=skipped,
            )

        start = text.rfind(_JSON_BEGIN)
        end = text.rfind(_JSON_END)
        if start == -1 or end == -1 or end <= start:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        try:
            report = json.loads(text[start + len(_JSON_BEGIN) : end].strip())
        except (ValueError, TypeError):
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        prefix = f"/home/{self.pr.repo}/"
        for suite in report.get("testResults") or []:
            path = (suite.get("name") or "").replace("\\", "/")
            idx = path.find(prefix)
            rel = path[idx + len(prefix) :] if idx != -1 else path.lstrip("/")

            cases = suite.get("assertionResults") or []

            if not cases and suite.get("status") == "failed":
                failed.add(
                    _normalise_identity(f"{rel}::<test suite failed to run>")
                )
                continue

            for case in cases:
                name = case.get("fullName") or case.get("title") or ""
                if not name:
                    continue
                ident = _normalise_identity(f"{rel}::{name}")
                status = case.get("status")
                if status == "passed":
                    passed.add(ident)
                elif status == "failed":
                    failed.add(ident)
                else:
                    skipped.add(ident)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
