from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


BUNDLE_PR_NUMBERS = [
    7780, 7789, 7799, 7905, 7947, 7982, 7987, 7992, 7995, 8042,
    8059, 8066, 8308, 8324, 8342, 8381, 8413, 8437, 8471, 8561,
]

NUMBER_INTERVAL = "-".join(str(n) for n in BUNDLE_PR_NUMBERS)

ALIAS_KEY = "serverless_8561_to_7780"

BASE_TAG = "base-sls-7780-8561"

RESULTS_START = "-----MSB_MOCHA_RESULTS_START-----"
RESULTS_END = "-----MSB_MOCHA_RESULTS_END-----"

RESULT_FILE = "/home/mocha_results.txt"
GLOB_FILE = "/home/mocha_glob.txt"

MOCHA_REPORTER_JS = """'use strict';

var fs = require('fs');

var NL = String.fromCharCode(10);
var OUTPUT = process.env.MSB_REPORT_FILE || '/home/mocha_results.txt';
var ROOT = process.env.MSB_REPO_ROOT || '';

function relativePath(file) {
  if (!file) {
    return '<unknown>';
  }
  var out = String(file);
  // The declared repo root first, then the resolved cwd: mocha reports the
  // PHYSICAL path, so a repo reached through a symlink would not match ROOT.
  var roots = [ROOT, process.cwd()];
  for (var i = 0; i < roots.length; i++) {
    if (roots[i] && out.indexOf(roots[i]) === 0) {
      out = out.slice(roots[i].length);
      break;
    }
  }
  while (out.charAt(0) === '/') {
    out = out.slice(1);
  }
  return out || '<unknown>';
}

// A result is ONE line. Several suites here build their title from a
// multi-line template literal, so fullTitle() comes back containing newlines;
// written out raw, each such result spans two physical lines and neither is a
// parseable result. Measured on pr-7780: 7 of 2464 results lost that way.
// Whitespace is collapsed instead of the title being truncated, so the name
// stays unique and stays identical across the three acts.
function oneLine(s) {
  var out = String(s);
  out = out.split(String.fromCharCode(13)).join(' ');
  out = out.split(NL).join(' ');
  out = out.split(String.fromCharCode(9)).join(' ');
  while (out.indexOf('  ') !== -1) {
    out = out.split('  ').join(' ');
  }
  return out.trim();
}

function MsbReporter(runner) {
  try {
    fs.writeFileSync(OUTPUT, '');
  } catch (e) {
    // fall through; the appends below recreate it
  }

  function record(status) {
    return function (test) {
      var name = '';
      var file = '';
      try {
        name = test.fullTitle();
      } catch (e) {
        name = (test && test.title) || '';
      }
      if (!name) {
        return;
      }
      try {
        file = test.file || (test.parent && test.parent.file) || '';
      } catch (e) {
        file = '';
      }
      // No duration in the name: a name carrying "1ms" differs between acts
      // and manufactures false transitions.
      var line = 'mocha:' + oneLine(relativePath(file)) + ' > '
        + oneLine(name) + ' ' + status;
      try {
        fs.appendFileSync(OUTPUT, line + NL);
      } catch (e) {
        // a single unwritable row must not abort the run
      }
    };
  }

  runner.on('pass', record('PASSED'));
  runner.on('fail', record('FAILED'));
  runner.on('pending', record('SKIPPED'));

  // Why a test failed has to reach the log. This reporter replaces mocha's
  // own, so without this the act records WHICH tests failed and never WHY,
  // and the failure has to be reconstructed instead of quoted. It goes to
  // stdout, outside the result block, so it can never be read as a result.
  runner.on('fail', function (test, err) {
    var name = '';
    try {
      name = test.fullTitle();
    } catch (e) {
      name = (test && test.title) || '';
    }
    var text = (err && (err.stack || err.message)) || String(err);
    var lines = String(text).split(NL).slice(0, 12);
    process.stdout.write('[mocha-fail] ' + oneLine(name) + NL);
    for (var i = 0; i < lines.length; i++) {
      process.stdout.write('[mocha-fail]     ' + lines[i] + NL);
    }
  });
}

module.exports = MsbReporter;
"""

_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

export CI=true
export ADBLOCK=1
export SLS_IGNORE_WARNING='*'
export MSB_REPORT_FILE={result_file}
export MSB_REPO_ROOT=/home/{repo}

MSB_GLOB=$(cat {glob_file})
if [ -z "$MSB_GLOB" ]; then
    echo "FATAL: no mocha glob was frozen at image-build time."
    exit 2
fi
echo "mocha glob: $MSB_GLOB"

# mocha loads EVERY file matched by the glob before it runs a single test, so
# one file that throws at require time takes down the whole run: zero results
# for the other two thousand tests. That is not hypothetical here -- it is the
# normal shape of the test act, where a new test file requires a module the FIX
# patch has not created yet:
#
#   Error: Cannot find module './resolveCfImportValue'
#   Require stack:
#   - /home/{repo}/lib/plugins/aws/utils/resolveCfImportValue.test.js
#
# Measured on pr-7905: 2426 tests discarded by one such file.
#
# So a load failure is not accepted as an answer. The file at the head of the
# require stack is excluded and mocha is re-invoked, until it either runs or
# there is nothing left to exclude. The excluded file's own tests are then
# ABSENT for that act, which is the truth -- they could not be loaded -- while
# every test that COULD have run does run. The fix act needs no exclusion at
# all, because there the missing module exists.
MSB_IGNORE=()
MSB_ATTEMPT=1
while [ "$MSB_ATTEMPT" -le 10 ]; do
    rm -f "$MSB_REPORT_FILE"

    set +e
    ./node_modules/.bin/mocha \\
        --reporter {reporter} \\
        --timeout 30000 \\
        "${{MSB_IGNORE[@]}}" \\
        "$MSB_GLOB" > /tmp/msb_mocha_out 2>&1
    set -e
    cat /tmp/msb_mocha_out

    if [ -s "$MSB_REPORT_FILE" ]; then
        break
    fi

    MSB_BAD=$(sed -n 's#^- /home/{repo}/\\(.*\\.test\\.js\\)$#\\1#p' /tmp/msb_mocha_out | head -1)
    if [ -z "$MSB_BAD" ]; then
        break
    fi
    echo "run_tests: '$MSB_BAD' could not be loaded; excluding it and re-running"
    MSB_IGNORE+=(--ignore "$MSB_BAD")
    MSB_ATTEMPT=$((MSB_ATTEMPT + 1))
done

if [ "${{#MSB_IGNORE[@]}}" -gt 0 ]; then
    echo "run_tests: ${{#MSB_IGNORE[@]}} file(s) excluded because they could not be loaded"
fi

if [ ! -s "$MSB_REPORT_FILE" ]; then
    echo "FATAL: mocha produced no results -- the runner failed to start."
fi

echo "{start}"
if [ -f "$MSB_REPORT_FILE" ]; then
    cat "$MSB_REPORT_FILE"
fi
echo ""
echo "{end}"
"""

PATCH_BINARIES_JS = """'use strict';

var fs = require('fs');

var NL = String.fromCharCode(10);
var text = fs.readFileSync(process.argv[2], 'utf8');
var lines = text.split(NL);

var path = '';
var post = '';

function flush(isBinary) {
  if (isBinary && path) {
    process.stdout.write(post + ' ' + path + NL);
  }
}

for (var i = 0; i < lines.length; i++) {
  var line = lines[i];
  if (line.indexOf('diff --git ') === 0) {
    path = '';
    post = '';
    var parts = line.split(' ');
    var b = parts[parts.length - 1];
    if (b.indexOf('b/') === 0) {
      path = b.slice(2);
    }
  } else if (line.indexOf('index ') === 0 && path) {
    var body = line.slice(6).split(' ')[0];
    var halves = body.split('..');
    if (halves.length === 2) {
      post = halves[1];
    }
  } else if (line.indexOf('Binary files ') === 0) {
    flush(true);
    path = '';
    post = '';
  }
}
"""

_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

apply_one () {{
    local p="$1"

    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
        return 0
    fi
    echo "apply_patch: plain apply failed for $p"

    local excludes=()
    local row sha path
    while read -r row; do
        [ -n "$row" ] || continue
        sha="${{row%% *}}"
        path="${{row#* }}"
        excludes+=("--exclude=$path")
        if [ -z "$(printf '%s' "$sha" | tr -d '0')" ]; then
            rm -f "$path"
            echo "apply_patch: binary deletion $path"
        else
            mkdir -p "$(dirname "$path")"
            if git cat-file blob "$sha" > "$path"; then
                echo "apply_patch: restored binary $path from blob $sha"
            else
                echo "apply_patch: blob $sha for $path is not in this repository"
                return 1
            fi
        fi
    done < <(node /home/patch_binaries.js "$p")

    if [ "${{#excludes[@]}}" -gt 0 ]; then
        if git apply --whitespace=nowarn "${{excludes[@]}}" "$p"; then
            echo "apply_patch: applied $p with ${{#excludes[@]}} binary path(s) restored"
            return 0
        fi
        echo "apply_patch: still failing after the binary rescue, retrying with --3way"
        git apply --3way --whitespace=nowarn "${{excludes[@]}}" "$p"
    else
        echo "apply_patch: no binary hunks to rescue, retrying with --3way"
        git apply --3way --whitespace=nowarn "$p"
    fi
    echo "apply_patch: applied $p with --3way"
}}

for p in "$@"; do
    apply_one "$p"
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

# Force a real content comparison: a stat-cache hit can report a clean tree
# that is not actually clean.
git update-index -q --really-refresh || true

# node_modules and package-lock.json are gitignored, so untracked files must
# not count as a change; anything TRACKED being dirty means an act mutated the
# tree and the next act would not start clean.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


class Serverless8561To7780ImageBase(Image):
    """Clone-only base, shared by all twenty PRs in the bundle.

    It stops at the clone plus CMD: no checkout, no pin, no scrub. Nothing in
    it is commit-specific, so its content is byte-identical for every PR and
    one tag can serve all of them.
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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo

        return """FROM {image_name}

{global_env}

WORKDIR /home/

RUN sed -i '/bullseye-security/d; /security.debian.org/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    jq \\
    ruby \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
        )


class Serverless8561To7780ImageDefault(Image):
    """Per-PR layer: context files, the checkout, the scrub, then prepare.sh."""

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
        return Serverless8561To7780ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        run_tests = _RUN_TESTS_SH.format(
            repo=repo,
            result_file=RESULT_FILE,
            glob_file=GLOB_FILE,
            reporter="/home/msb-mocha-reporter.js",
            start=RESULTS_START,
            end=RESULTS_END,
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "patch_binaries.js", PATCH_BINARIES_JS),
            File(".", "msb-mocha-reporter.js", MOCHA_REPORTER_JS),
            File(".", "run_tests.sh", run_tests),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git reset --hard
git clean -fdq -e node_modules
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

export CI=true
export ADBLOCK=1
export SLS_IGNORE_WARNING='*'

# The mocha glob comes from THIS commit's package.json, never from a table in
# this file. It is frozen into a file so all three acts read one same value.
node -e "
var s = (require('./package.json').scripts || {{}}).test || '';
var m = s.match(/mocha[ ]+['\\"]([^'\\"]+)['\\"]/);
if (!m) {{ console.error('no mocha glob in scripts.test: ' + s); process.exit(1); }}
process.stdout.write(m[1]);
" > {glob_file}
test -s {glob_file}
echo "frozen mocha glob: $(cat {glob_file})"

# --ignore-scripts skips scripts/postinstall.js, which prints a banner and then
# constructs a Serverless instance: nothing a mocha run touches, and one less
# network call that can fail the build.
#
# There is no lockfile in this repo (package-lock.json is gitignored), so
# `npm ci` is not an option and every install resolves current versions of a
# 2020 dependency tree.
#
# Its exit status is deliberately NOT swallowed: this is the REAL install, so
# a failure must fail the build rather than yield an image where every act
# scores 0/0/0 with no error.
npm install --no-audit --no-fund --ignore-scripts

node --version
npm --version
test -d node_modules

# Restore the tree for the acts while KEEPING node_modules -- resolving the
# dependency tree at image-build time is the whole point of this step.
git reset --hard
git clean -fdq -e node_modules
bash /home/check_git_changes.sh
""".format(repo=repo, sha=sha, glob_file=GLOB_FILE),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        sha = self.pr.base.sha

        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


class Serverless8561To7780(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Serverless8561To7780ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _extract_results(test_log: str) -> str:
        """Return only what the reporter wrote, never the suites' stdout.

        Falls back to the whole text when the start marker is absent, so a log
        that never reached the marker (and any markerless probe) still parses.
        The result lines are distinctive enough that the fallback cannot invent
        tests out of ordinary shell output.
        """
        start = test_log.find(RESULTS_START)
        if start == -1:
            return test_log
        start += len(RESULTS_START)
        end = test_log.find(RESULTS_END, start)
        if end == -1:
            return test_log[start:]
        return test_log[start:end]

    def parse_log(self, test_log: str) -> TestResult:
        """Read the canonical result lines the reporter emitted.

            mocha:<path> > <full title> PASSED|FAILED|SKIPPED

        The reporter supplies the fully-qualified name, so nothing is
        reconstructed here: no marker glyphs, no describe stack inferred from
        indentation, no duration to strip.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        occurrences: dict[str, int] = {}

        line_re = re.compile(
            r"^(?P<name>\S+(?::|(?=.* > )).*?) (?P<status>PASSED|FAILED|SKIPPED)$"
        )
        body = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", self._extract_results(test_log))

        for line in body.splitlines():
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name = m.group("name").strip()
            status = m.group("status")

            seen = occurrences[name] = occurrences.get(name, 0) + 1
            if seen > 1:
                name = f"{name} #{seen}"

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


Instance.register("serverless", NUMBER_INTERVAL)(Serverless8561To7780)
Instance.register("serverless", ALIAS_KEY)(Serverless8561To7780)
