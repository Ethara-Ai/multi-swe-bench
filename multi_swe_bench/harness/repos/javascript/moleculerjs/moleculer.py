from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "/home/moleculer"
JEST_REPORT = "/tmp/jest-report.json"

TEST_CMD = (
    "./node_modules/.bin/jest --ci --forceExit --maxWorkers=1 "
    f"--json --outputFile={JEST_REPORT}"
)


class MoleculerImageBase(Image):
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
        return "node:14.21.3"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
            tail = f"""
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK.rstrip()}

{self.clear_env}

CMD ["/bin/bash"]
"""
        else:
            fetch = f"COPY {repo} /home/{repo}"
            tail = ""

        return f"""FROM {base_img}

# D17 ideal order places WORKDIR /home/ between the CA symlink farm (emitted by
# the enhancer, just above) and the first network RUN. Kept even though nothing
# here is relative to it -- the clone target is absolute -- because the run
# scripts' `cd /home/<repo>` contract is anchored on this convention.
WORKDIR /home/

# No apt layer, deliberately. The full `node` image is built on buildpack-deps
# and already ships git, ca-certificates, curl, patch and coreutils -- D10
# explicitly allows a minimal or absent apt block for official node images. It
# is also the only safe choice: node:14.21.3 is Debian 10 (buster) -- verified by
# running the image -- whose repositories are archived, and
# Image._get_apt_update_command's archive.debian.org rewrite fires only for a
# base literally named debian:*/gcc:*, never for node:*. An apt-get update here
# would 404 the build outright. Assert the tools instead, so
# a missing one fails loudly at build time rather than midway through a graded
# run.
RUN set -eux; \\
    command -v git; \\
    command -v curl; \\
    command -v patch; \\
    command -v csplit; \\
    node --version; \\
    npm --version; \\
    test -f /etc/ssl/certs/ca-certificates.crt

{fetch}
{tail}"""


class MoleculerImageDefault(Image):
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
        return MoleculerImageBase(self.pr, self._config)

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
                """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does not remove stray untracked ones, and the Dockerfile's HEAD/refs
# asserts only prove WHICH commit is checked out -- a dirty tree satisfies all of
# them. Without this, a graded stage could start from contaminated code and the
# f2p verdict would be untrustworthy.
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
""",
            ),
            File(
                ".",
                "apply_patch.sh",
                r"""#!/bin/bash
# Apply one patch as completely as possible, then ALWAYS exit 0. The caller must
# reach jest regardless: a stage that dies while patching reports zero tests,
# which the harness cannot tell apart from "the fix does not work". Whole-patch
# fast path first; per-file cascade only when something rejects, so one
# unappliable hunk cannot take the source fix down with it.
#
# Both of this PR's patches were verified to apply cleanly against the base
# tree at 832dd9cc (test.patch, then fix.patch, then both together) and neither
# carries a binary hunk, so the fast path is the expected route. The cascade is
# insurance, not the plan.

patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            # From HEAD, not the index: `git apply --3way` stages what it merges,
            # so `git checkout -- <path>` would restore the half-applied version.
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    # Exiting 0 stays deliberate -- the caller must still reach jest. But a patch
    # that did not fully apply must not be discoverable only by a human reading
    # the log. Drop a marker the run-scripts turn into an unmissable banner.
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

exit 0
""",
            ),
            File(
                ".",
                "jest-report.js",
                r"""// Turn jest's JSON report into one canonical line per test:
//
//     MSB-TEST-RESULT|<PASSED|FAILED|SKIPPED>|<repo-relative path> > <describes...> > <title>
//
// The pipe-delimited prefix is used instead of a bare "PASSED <name>" so that no
// line of jest's own console output can ever be mistaken for a result. Names are
// built from ancestorTitles because this repo has duplicate leaf titles inside a
// single spec file (see the SLOT 7 note in the config), and are prefixed with
// the repo-relative path so report._test_name_matches_files can bind a test to
// the file the gold patch touched.
const fs = require('fs');
const path = require('path');

const REPORT_FILE = '/tmp/jest-report.json';
const REPO_ROOT = '/home/moleculer';

let report;
try {
  report = JSON.parse(fs.readFileSync(REPORT_FILE, 'utf8'));
} catch (e) {
  // Emitting nothing is correct: the harness reads an absent name as "test not
  // present", never as "test failed". The run-scripts already fail loudly when
  // the report file is missing, so this path is only reached for a corrupt file.
  console.log('MSB-TEST-SUMMARY|raw=0|files=0|error=' + String(e && e.message));
  process.exit(0);
}

const STATUS = {
  passed: 'PASSED',
  failed: 'FAILED',
  pending: 'SKIPPED',
  skipped: 'SKIPPED',
  todo: 'SKIPPED',
  disabled: 'SKIPPED'
};

function relative(name) {
  const p = String(name || '');
  if (p.indexOf(REPO_ROOT + '/') === 0) return p.slice(REPO_ROOT.length + 1);
  return path.relative(REPO_ROOT, p).split(path.sep).join('/');
}

let raw = 0;
let files = 0;
let emptySuites = 0;

for (const file of report.testResults || []) {
  files++;
  const rel = relative(file.name);
  const results = file.assertionResults || [];

  if (results.length === 0) {
    // A suite that reported no assertions did not run -- a require/parse error
    // at collection time. jest can only describe that at FILE level, so emit a
    // file-level marker. It keeps the failure visible and per-file granular
    // instead of letting the whole stage silently blank out.
    if (file.status !== 'passed') {
      emptySuites++;
      console.log('MSB-TEST-RESULT|FAILED|' + rel + ' > <suite reported no tests>');
    }
    continue;
  }

  for (const t of results) {
    raw++;
    const status = STATUS[t.status] || 'SKIPPED';
    const name = rel + ' > ' + (t.ancestorTitles || []).concat([t.title || '']).join(' > ');
    console.log('MSB-TEST-RESULT|' + status + '|' + name);
  }
}

// `raw` is the number of assertionResults jest reported, BEFORE the parser
// de-duplicates into a set. Comparing it against the parsed count in report.json
// is what closes Config-QC 4A (name collisions) without guesswork.
console.log(
  'MSB-TEST-SUMMARY|raw=' + raw + '|files=' + files + '|emptySuites=' + emptySuites
);
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Plain `set -e`, not pipefail: this runs once, at PR-image BUILD time, and any
# failure here should stop the build.
#
# NOTE: prepare.sh is NOT replayed by --mode dataset -- prepare_script_path is
# only threaded into the envagent branch, and each graded stage is its own
# `docker run`. Anything exported here is therefore invisible to the test runner;
# environment the runner needs lives in the three stage scripts.
set -e

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# `|| true` on the install is required by the rubric (a native-module compile
# failure on arm64 is common and non-fatal here: gc-stats, event-loop-stats and
# @icebob/node-memwatch are all loaded through try/catch in src/metrics/commons.js
# and are not imported by any unit test). The cascade degrades in a defined
# order rather than giving up on the first node-gyp error.
npm ci --no-audit --no-fund --loglevel=error \\
    || npm ci --no-audit --no-fund --ignore-scripts --loglevel=error \\
    || npm install --no-audit --no-fund --ignore-scripts --loglevel=error \\
    || true

# THE HARD GATE THAT PAIRS WITH `|| true`. On its own that `|| true` would also
# swallow a TOTAL install failure and ship an image with no node_modules; all
# three stages would then die identically, every stage would parse (0,0,0), and
# the instance would be rejected with nothing in the log naming the cause. Under
# `set -e` this converts that case into a loud BUILD failure instead.
test -x ./node_modules/.bin/jest

node --version
npm --version
./node_modules/.bin/jest --version
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Baseline: no patches. See the exit-status contract note in test-run.sh; all
# three scripts are byte-identical apart from the patch application.
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report} /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# EXIT-STATUS CONTRACT. A failing suite is the EXPECTED state of this stage --
# the whole point is that the gold tests fail before the fix. Under a bare
# `set -e` jest's non-zero exit would abort before the report is read; under a
# bare `|| true` a runner that never started would look like a clean sweep of
# zero tests. So: capture the status explicitly, then gate on the report file.
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report} /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd {repo_dir}
rm -f {report} /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi

set +e
{test_cmd}
JEST_STATUS=$?
set -e

if [ ! -s {report} ]; then
    echo "FATAL: jest wrote no JSON report (exit $JEST_STATUS) -- the runner never started" >&2
    exit 1
fi

node /home/jest-report.js
""".format(repo_dir=REPO_DIR, report=JEST_REPORT, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        verify = (
            "RUN set -eux; \\\n"
            f"    cd /home/{repo}; \\\n"
            f'    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\\n'
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"; \\\n'
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{verify}

{self.clear_env}

"""


@Instance.register("moleculerjs", "moleculer")
class Moleculer(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MoleculerImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^MSB-TEST-RESULT\|(PASSED|FAILED|SKIPPED)\|(.+)$")

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
