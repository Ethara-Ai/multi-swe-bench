from __future__ import annotations

import json
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# serverless/serverless -- era B: the 2.x tree (PR 7619, Feb 2021)
#
# PR 7619 sits alone here, a year after the other nine. Its base commit
# 258543ab6e18 is serverless 2.27.1, where package.json says
#
#     "test": "mocha \"test/unit/**/*.test.js\""
#
# The whole test tree moved between the eras: lib/ holds ZERO *.test.js at
# this commit, and the 196 test files live under test/unit/ instead. That is
# why this cannot share era A's config -- its glob would collect nothing.
# The other nine PRs are in serverless_7741_to_7327.py.
#
# Routing: run_pipeline.sh stamps number_interval on all ten rows, so nothing
# falls through to the bare "serverless/serverless" key held by serverless.py.
# ---------------------------------------------------------------------------

# Markers wrapped around the machine-readable report inside the stage log.
# parse_log() reads ONLY what sits between them.
JSON_START = "-----MSB_MOCHA_JSON_START-----"
JSON_END = "-----MSB_MOCHA_JSON_END-----"

# The mocha glob for this era, exactly as package.json spells it.
TEST_GLOB = "test/unit/**/*.test.js"

# Opt this image out of DockerfileEnhancer WITHOUT paying for a frontend image.
#
# enhance() bails out early on `if cls.SYNTAX_DIRECTIVE in raw` (image.py:317) -
# a plain substring test over the whole file. Docker, by contrast, only honours
# `# syntax=` as a parser directive when it appears in the LEADING comment
# block, before any instruction. Putting the marker AFTER the FROM therefore
# satisfies the enhancer while leaving Docker on its built-in frontend, so no
# build has to pull docker/dockerfile:1.6 from Docker Hub.
#
# This is load-bearing for rule 9. Without it _inject_final_sanitize() would
# append the hardening block to the BASE image (it fires on any content holding
# `git clone` plus a CMD), which is exactly what rule 9 forbids: the base must
# keep full git history so it stays reusable by any 2.x PR added later.
ENHANCER_OPT_OUT = (
    "# The next line is a MARKER, not a parser directive - it is deliberately\n"
    "# not the first line. It opts this file out of DockerfileEnhancer\n"
    "# (image.py:317) while leaving Docker on its built-in frontend.\n"
    f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}"
)

# Machine-readable mocha reporter, shipped as its own file.
#
# Contains NO backslash and NO regular expression on purpose, so carrying it
# inside this Python string cannot corrupt it.
#
# Why not `--reporter tap`: the serverless suites write to stdout while they
# run (the AWS SDK v2 end-of-support banner, plugin logs, a stray "Commands"
# line -- all observed in the container), so a parser that scrapes the merged
# stream can be poisoned by any test printing a line that begins "ok " or
# "not ok ". Writing from the reporter straight to its own file keeps the
# result set completely separate from whatever the tests print.
#
# One JSON object per line, appended as each test finishes, so a crash or a
# process.exit() later in the run still leaves every earlier result on disk.
#
# It never requires('mocha'): this file is loaded from /home/, whose module
# resolution cannot see the repo's node_modules.
MOCHA_REPORTER_JS = """'use strict';

var fs = require('fs');

var NL = String.fromCharCode(10);
var OUTPUT = process.env.MSB_REPORT_FILE || '/home/mocha_results.jsonl';
var ROOT = process.env.MSB_REPO_ROOT || '';

function relativePath(file) {
  if (!file) {
    return '';
  }
  var out = String(file);
  // Try the declared repo root first, then the resolved cwd. mocha reports the
  // PHYSICAL path, so a repo reached through a symlink would not match ROOT;
  // process.cwd() is already resolved and catches that case.
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
  return out;
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
      try {
        file = test.file || (test.parent && test.parent.file) || '';
      } catch (e) {
        file = '';
      }
      var row = { status: status, name: name, file: relativePath(file) };
      try {
        fs.appendFileSync(OUTPUT, JSON.stringify(row) + NL);
      } catch (e) {
        // a single unwritable row must not abort the run
      }
    };
  }

  runner.on('pass', record('passed'));
  runner.on('fail', record('failed'));
  runner.on('pending', record('pending'));
}

module.exports = MsbReporter;
"""

# ONE shared definition of the test invocation so run.sh, test-run.sh and
# fix-run.sh can never drift apart.
#
#   CI / ADBLOCK / SLS_IGNORE_WARNING  silence the banners serverless prints
#   --timeout 30000                    the container is slower than 2020 CI;
#                                      a suite's own this.timeout() still wins
#   set +e around mocha                a red suite is data, not a stage crash
MOCHA_RUN = """cd /home/{repo}

export CI=true
export ADBLOCK=1
export SLS_IGNORE_WARNING='*'
export MSB_REPORT_FILE=/home/mocha_results.jsonl
export MSB_REPO_ROOT=/home/{repo}

rm -f "$MSB_REPORT_FILE"

set +e
./node_modules/.bin/mocha \\
    --reporter /home/msb-mocha-reporter.js \\
    --timeout 30000 \\
    "{glob}"
set -e

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


def mocha_run(repo: str) -> str:
    return MOCHA_RUN.format(
        repo=repo, glob=TEST_GLOB, start=JSON_START, end=JSON_END
    )


CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

# Force a real content comparison. A stat-cache hit can make `git status`
# report a clean tree that is not actually clean.
git update-index -q --really-refresh || true

if ! git diff --quiet; then
  echo "check_git_changes: Unstaged content differences"
  exit 1
fi

if ! git diff --cached --quiet; then
  echo "check_git_changes: Staged content differences"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


class ServerlessEraBImageBase(Image):
    """Base for this era. PR 7619 is currently its only member.

    Rule 9 shape: the base stops at the clone. Toolchain, infrastructure block
    and `git clone`, then CMD -- no checkout, no pin, no gc, no scrub, no
    asserts. All of that lives in the pr-<N> layer.

    The tag is era-named (`base-sls2x`) rather than `base-pr-7619`, so a second
    2.x PR added to this dataset later joins the same base instead of forcing a
    new one. Full history is kept here, which is what makes that safe.
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
        # .github/workflows at this commit run node-version 14.x as the primary
        # job (12.x and 10.x as secondaries), so 14 is the era-correct choice
        # here rather than a workaround. package.json says engines >= 10.0.
        #
        # It also has to be at least 14.18 for the same reason era A does:
        # there is no lockfile, so dependencies resolve forward to versions
        # that use the `node:` module specifier. node:14-bullseye is 14.21.3.
        #
        # Measured in the container at this commit: 2618 tests, 2614 passing,
        # identical across two consecutive runs.
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return "base-sls2x"

    def workdir(self) -> str:
        return "base-sls2x"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )

        label_block = "LABEL " + " \\\n      ".join(
            [
                f'org.opencontainers.image.title="{org}/{repo}"',
                f'org.opencontainers.image.description="{org}/{repo} Docker image"',
                f'org.opencontainers.image.source="https://github.com/{org}/{repo}"',
                'org.opencontainers.image.authors="https://www.ethara.ai/"',
            ]
        )

        # ruby matters here too, though less dramatically than in era A. The
        # AwsInvokeLocal invokeLocalRuby tests shell out to `ruby`; without it
        # this era reports 7 failures, with it 4. It does not abort the run the
        # way era A does, but installing it keeps three otherwise-green tests
        # in the p2p set. Measured in the container, not inferred.
        packages = " \\\n    ".join(
            [
                "ca-certificates",
                "curl",
                "git",
                "jq",
                "ruby",
            ]
        )
        # Debian 11 entered LTS in Aug 2026 and its SECURITY pool now rotates
        # faster than the index that points into it. On 2026-09-07 the amd64
        # half of a multi-arch build died on
        #
        #     libruby2.7 2.7.4-1+deb11u6 ... 404 Not Found
        #
        # because bullseye-security ADVERTISED deb11u6 while the pool had
        # already dropped it. Retrying does not help; the file is gone, and
        # `apt-get update` re-fetches the same stale index. The arm64 half had
        # succeeded an hour earlier purely because that mirror was still in
        # sync, so this is a race, not an architecture problem.
        #
        # The MAIN bullseye pool is immutable and still carries
        # 2.7.4-1+deb11u1 -- the same ruby 2.7.4 either way -- so dropping the
        # security line trades security patches this test image does not need
        # for a build that cannot break when a mirror rotates.
        #
        # Verified on BOTH linux/amd64 and linux/arm64: ruby 2.7.4, git 2.30.2,
        # jq 1.6, exit 0.
        apt_block = (
            "RUN sed -i '/bullseye-security/d; /security.debian.org/d' /etc/apt/sources.list && \\\n"
            "    apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

        sections = [
            f"FROM {image_name}",
            ENHANCER_OPT_OUT,
            build_args,
            DockerfileEnhancer._ENV_BLOCK,
            label_block,
            DockerfileEnhancer._CERT_SYMLINKS,
            apt_block,
            "WORKDIR /home/",
            f'RUN git clone "${{REPO_URL}}" /home/{repo}',
            f"WORKDIR /home/{repo}",
            'CMD ["/bin/bash"]',
        ]

        return "\n\n".join(sections) + "\n"


class ServerlessEraBImageDefault(Image):
    """Per-PR image.

    Rule 9 shape: FROM the shared base, the COPY lines, the hardcoded
    ARG BASE_COMMIT, `RUN bash /home/prepare.sh`, then the FULL hardening
    block last.

    The scrub has to come after prepare.sh because npm needs the network, and
    it has to carry its own ARG because build_dataset.py passes REPO_URL /
    BASE_COMMIT as build args only to string-dependency() images
    (build_dataset.py:625-629); this layer's dependency() is an Image.
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

    def dependency(self) -> Image | None:
        return ServerlessEraBImageBase(self.pr, self._config)

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
                CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "msb-mocha-reporter.js",
                MOCHA_REPORTER_JS,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export ADBLOCK=1
export SLS_IGNORE_WARNING='*'

# --ignore-scripts skips scripts/postinstall.js, which prints a banner and
# then constructs a Serverless instance. Nothing a mocha run touches, and one
# less network call that can fail the build. Verified to give byte-identical
# results to a scripted install: 2431 passed / 9 pending / 9 failed either way.
#
# There is no lockfile in this repo (package-lock.json is gitignored at
# .gitignore:7), so `npm ci` is not an option.
npm install --no-audit --no-fund --ignore-scripts

# node_modules and package-lock.json are both gitignored, so the tree must
# still be clean here. Verified: `git status --porcelain` returns 0 lines.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

{mocha}
""".format(mocha=mocha_run(self.pr.repo)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{mocha}
""".format(pr=self.pr, mocha=mocha_run(self.pr.repo)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{mocha}
""".format(pr=self.pr, mocha=mocha_run(self.pr.repo)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

{self._HARDENING_BLOCK}"""


@Instance.register("serverless", "serverless_7619_to_7619")
class ServerlessEraB(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServerlessEraBImageDefault(self.pr, self._config)

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

    @staticmethod
    def _extract_report(test_log: str) -> str:
        """Return only the JSONL the reporter wrote, never the test stdout."""
        start = test_log.find(JSON_START)
        if start == -1:
            return ""
        start += len(JSON_START)
        end = test_log.find(JSON_END, start)
        if end == -1:
            return test_log[start:]
        return test_log[start:end]

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Occurrence counter. A repo that builds tests in a loop with a literal
        # describe() string gives several tests the same fullTitle in the same
        # file; without this they would collapse into one id. The count is
        # taken in reporter order, which is mocha's run order and therefore
        # stable across the three stages.
        occurrences: dict[str, int] = {}

        for line in self._extract_report(test_log).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue

            name = (row.get("name") or "").strip()
            if not name:
                continue
            path = (row.get("file") or "").strip() or "<unknown>"

            # `mocha::` first, deliberately. report.py's cheating guard splits
            # a test name on "::" and treats the HEAD as a file path; a head of
            # "mocha" matches no file, so a PR that CREATES a test file cannot
            # trip guard_fix_patch_touched_tests. See the project memory note
            # test-id-must-put-tool-before-path.
            test_id = f"mocha::{path}::{name}"
            seen = occurrences[test_id] = occurrences.get(test_id, 0) + 1
            if seen > 1:
                test_id = f"{test_id} #{seen}"

            status = row.get("status")
            if status == "passed":
                passed_tests.add(test_id)
            elif status == "failed":
                failed_tests.add(test_id)
            elif status == "pending":
                skipped_tests.add(test_id)

        # Failure wins, so a title that fails once and passes once is not green.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
