import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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


# apply_patch.sh -- the dataset's diffs carry an index hash but no binary
# payload. Measured on this dataset: 0 binary hunks in both patches, so the
# restore path never fires here; it is kept because it costs nothing and a
# future PR of this repo (it ships PNG/SVG icons under web-extension/) could
# need it. Plain `git apply` first, `--3way` only as a fallback -- the primary
# apply is what a failure must be counted from (FLOW VERDICT DISCIPLINE).
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/gopassbridge
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


# jest_test_report.py -- turn Jest's machine-readable result file into the
# trailing-keyword lines parse_log reads.
#
# Why the JSON and not the console reporter: jest's verbose output encodes the
# suite path in a "PASS <file>" banner, the describe nesting in INDENTATION, and
# appends a per-test duration -- three things that must be reassembled by regex,
# with workers interleaving the blocks. `--json --outputFile` hands over the
# same information already structured: an absolute file path, the ancestorTitles
# list and the title, with no ANSI and no duration. A name built from those is
# byte-identical across the three acts, which is what stops a false transition
# (FLOW audit 4B).
_JEST_TEST_REPORT_PY = '''"""Turn one Jest run into per-test results.

usage: jest_test_report.py <jest-json> <repo-root>

Emits one line per assertion, in the trailing-keyword form parse_log reads:

    jest:tests/unit/search.test.js > search method > shows message PASSED
    jest:tests/unit/search.test.js > search method > shows message FAILED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests at all (the runner never started -- NOT
"zero tests passed"). 2 is the case GATE 0 exists to catch.
"""
import json
import os
import sys

STATUS = {
    "passed": "PASSED",
    "failed": "FAILED",
    "pending": "SKIPPED",
    "skipped": "SKIPPED",
    "todo": "SKIPPED",
    "disabled": "SKIPPED",
}


def main():
    path, root = sys.argv[1], sys.argv[2]
    if not root.endswith(os.sep):
        root += os.sep

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("jest_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    lines, failed = [], False
    for suite in data.get("testResults", []):
        name = suite.get("name", "")
        rel = name[len(root):] if name.startswith(root) else os.path.basename(name)
        for a in suite.get("assertionResults", []):
            status = STATUS.get(a.get("status", ""), "FAILED")
            parts = [rel] + list(a.get("ancestorTitles", [])) + [a.get("title", "")]
            lines.append("jest:%s %s" % (" > ".join(p for p in parts if p), status))
            if status == "FAILED":
                failed = True

    if not lines:
        sys.stderr.write(
            "jest_test_report: the run reported no tests; the runner never started\\n")
        for key in ("numTotalTestSuites", "numFailedTestSuites"):
            sys.stderr.write("jest_test_report: %s=%s\\n" % (key, data.get(key)))
        return 2

    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh -- the repo's own suite is `npm test`, which is npm-run-all over
# test:lint AND test:jest. Only test:jest yields per-test evidence; test:lint is
# prettier/eslint/web-ext over the whole tree, so a formatting nit in the PR's
# diff would abort the act before a single test ran and score the PR 0/0/0.
# The jest binary is invoked from node_modules directly rather than through npx,
# so the act never consults the network to resolve it.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/gopassbridge
JSON=/tmp/jest.json
OUT=/tmp/jest.out
rm -f "$JSON" "$OUT"
./node_modules/.bin/jest --json --outputFile="$JSON" > "$OUT" 2>&1
jest_rc=$?
cat "$OUT"
echo "jest exit=${jest_rc}"
echo "----- per-test results -----"
python3 /home/jest_test_report.py "$JSON" /home/gopassbridge
"""


class GopassbridgeImageBase(Image):
    """Shared base image, one per PR (`base-pr-<N>`).

    dockerfile() is deliberately NOT overridden: the harness's own
    Image.dockerfile() already emits the mandated order (FROM -> apt -> clone ->
    WORKDIR -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening
    scrub + its four assertions -> CMD), and DockerfileEnhancer then prepends
    the BuildKit directive, the build ARGs (BASE_COMMIT left EMPTY), the env
    block, the labels and the cert links. Nothing commit-specific is written
    here: build_dataset passes BASE_COMMIT and REPO_URL as docker build args,
    read straight from the dataset.

    The tag carries the PR number because the base's CONTENT is per-PR (it is
    checked out at that PR's base commit). A shared `:base` tag would be one
    name for two different images -- FLOW Issue 25.
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

    # Node 14 (bullseye): the repo's CI pins Node 10 (.travis.yml) and its
    # lockfile pins jest 24, which predates Node 16's fs/vm changes. 14 is the
    # newest line that runs this suite unmodified -- measured 174/174 passing at
    # base_commit -- while still resolving apt from a non-EOL Debian and
    # publishing both linux/amd64 and linux/arm64.
    def dependency(self) -> Union[str, "Image"]:
        return "node:14-bullseye-slim"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The harness already installs ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo and wget. This suite is pure jsdom --
        # no browser, no native addon -- so it needs nothing further.
        return []

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so the lockfile is this PR's
        # own. --frozen-lockfile makes the resolution reproducible instead of
        # drifting to whatever is newest at build time.
        #
        # NOT `|| true`: this is the toolchain, not a nice-to-have cache. A
        # warm-up that fails quietly ships an image with no node_modules and
        # leaves every act at the mercy of the network (FLOW Issue 14, GATE 1).
        # The version probe is what proves the install actually took effect.
        return (
            "RUN yarn install --frozen-lockfile --non-interactive && \\\n"
            "    ./node_modules/.bin/jest --version && \\\n"
            "    node --version"
        )


class GopassbridgeImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else, which is the shape of
    Main_Tasks/pr_specific dockerfile.dockerfile. The clone, the checkout and
    the history scrub already happened in the base."""

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
        return GopassbridgeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "jest_test_report.py", _JEST_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
yarn install --frozen-lockfile --non-interactive || true
./node_modules/.bin/jest --version
node --version
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/gopassbridge
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/gopassbridge
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

cd /home/gopassbridge
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

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("gopasspw", "gopassbridge")
class Gopassbridge(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GopassbridgeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # The RAW runner output sits in the same log, and libtest's own failure
        # line -- `test <name> ... FAILED` -- also ends in FAILED, so a whole-log
        # scan invents a second, bogus test called `test <name> ...` and inflates
        # failed_count. Measured on vortix pr-150 the moment the test act first
        # produced a failure: the report claimed 54 results where 53 were
        # emitted, the extra one being that phantom.
        #
        # The fallback to the whole text matters: a bare sequence of result lines
        # with no marker (the config audit's parse_log probe, 4C) must still
        # parse, and a log that never reached the marker must still yield 0/0/0.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what jest_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword, so no duration or
        # count can leak into it and manufacture a false transition between acts.
        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
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
