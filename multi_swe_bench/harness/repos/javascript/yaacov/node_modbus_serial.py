import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# node-modbus-serial (yaacov/node-modbus-serial) uses:
#   scripts.test = "mocha --recursive"
# at base commit 82be9bc (Feb 2020). Node 10/12 era per .travis.yml.
# Native dep `serialport@8` needs python + build-essential + libudev-dev at
# install time. Install is best-effort (|| true) so mocha still runs even when
# native bindings fail (the UDP test uses `dgram` + `mockery`, no serialport).
# Custom mocha reporter (shipped as /home/mocha-json-file-reporter.js) is a
# byte-for-byte copy of mocha 6.x's built-in JSON reporter with an extra
# `file` field on every emitted test, so downstream can key tests by
# `<source path>::<fullTitle>` instead of just `<fullTitle>`.
_TEST_COMMAND = "npx mocha --recursive --reporter /home/mocha-json-file-reporter.js test"

# Shipped verbatim as /home/mocha-json-file-reporter.js. Uses only mocha's
# public API (`mocha.reporters.Base`, `mocha.Runner.constants`) so it works
# unchanged across mocha 6/7/8/9/10.
_MOCHA_JSON_FILE_REPORTER = """'use strict';

function resolveMocha() {
  try { return require('mocha'); } catch (_) {}
  try { return require(process.cwd() + '/node_modules/mocha'); } catch (_) {}
  throw new Error('mocha-json-file-reporter: cannot locate mocha module');
}

var mocha = resolveMocha();
var Base = mocha.reporters.Base;
var constants = mocha.Runner.constants;
var EVENT_TEST_END = constants.EVENT_TEST_END;
var EVENT_RUN_END = constants.EVENT_RUN_END;
var EVENT_TEST_FAIL = constants.EVENT_TEST_FAIL;
var EVENT_TEST_PASS = constants.EVENT_TEST_PASS;
var EVENT_TEST_PENDING = constants.EVENT_TEST_PENDING;

module.exports = JSONFileReporter;

function JSONFileReporter(runner, options) {
  Base.call(this, runner, options);

  var self = this;
  var tests = [];
  var pending = [];
  var failures = [];
  var passes = [];

  runner.on(EVENT_TEST_END, function (test) { tests.push(test); });
  runner.on(EVENT_TEST_PASS, function (test) { passes.push(test); });
  runner.on(EVENT_TEST_FAIL, function (test) { failures.push(test); });
  runner.on(EVENT_TEST_PENDING, function (test) { pending.push(test); });

  runner.once(EVENT_RUN_END, function () {
    var obj = {
      stats: self.stats,
      tests: tests.map(clean),
      pending: pending.map(clean),
      failures: failures.map(clean),
      passes: passes.map(clean)
    };
    runner.testResults = obj;
    process.stdout.write(JSON.stringify(obj, null, 2));
  });
}

function clean(test) {
  var err = test.err || {};
  if (err instanceof Error) {
    err = errorJSON(err);
  }
  return {
    title: test.title,
    fullTitle: typeof test.fullTitle === 'function' ? test.fullTitle() : (test.fullTitle || ''),
    file: test.file || (test.parent && test.parent.file) || '',
    duration: test.duration,
    currentRetry: typeof test.currentRetry === 'function' ? test.currentRetry() : (test.currentRetry || 0),
    err: cleanCycles(err)
  };
}

function errorJSON(err) {
  var res = {};
  Object.getOwnPropertyNames(err).forEach(function (key) {
    res[key] = err[key];
  }, err);
  return res;
}

function cleanCycles(obj) {
  var cache = [];
  return JSON.parse(
    JSON.stringify(obj, function (key, value) {
      if (typeof value === 'object' && value !== null) {
        if (cache.indexOf(value) !== -1) return '' + value;
        cache.push(value);
      }
      return value;
    })
  );
}
"""


class ImageBase(Image):
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
        return "node:14"

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

        # Project convention: clone lives in the base image so the
        # DockerfileEnhancer rewrites it into the standard REPO_URL/
        # BASE_COMMIT-pinned form and injects the history-isolation
        # hardening block automatically.
        if self.config.need_clone:
            code = f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

# Debian buster reached EOL; rewrite sources to archive.debian.org.
RUN set -eux; \\
    if [ -f /etc/apt/sources.list ]; then \\
        sed -i 's|http://deb.debian.org|http://archive.debian.org|g; s|http://security.debian.org|http://archive.debian.org|g; /-updates/d; /-backports/d' /etc/apt/sources.list; \\
    fi; \\
    for f in /etc/apt/sources.list.d/*.list; do \\
        [ -f "$f" ] || continue; \\
        sed -i 's|http://deb.debian.org|http://archive.debian.org|g; s|http://security.debian.org|http://archive.debian.org|g; /-updates/d; /-backports/d' "$f"; \\
    done; true

# serialport@8 native module needs python + build-essential + libudev-dev.
RUN apt-get -o Acquire::Check-Valid-Until=false update \\
 && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 make g++ libudev-dev \\
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "mocha-json-file-reporter.js", _MOCHA_JSON_FILE_REPORTER),
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Native optional deps (serialport@8) may fail on modern base images; that
# is fine — mocha runs from devDependencies which install successfully.
npm install --no-audit --no-fund --unsafe-perm || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo}
{test_command}
""".format(repo=self.pr.repo, test_command=_TEST_COMMAND),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_command}
""".format(repo=self.pr.repo, test_command=_TEST_COMMAND),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{test_command}
""".format(repo=self.pr.repo, test_command=_TEST_COMMAND),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Base image already carries the pinned checkout + hardening (injected
        # by DockerfileEnhancer). PR image stays minimal per project standard.
        return f"""FROM {name}:{tag}


{copy_commands}
RUN bash /home/prepare.sh 
"""


@Instance.register("yaacov", "node-modbus-serial")
class NodeModbusSerial(Instance):
    """yaacov/node-modbus-serial — mocha JSON reporter, single invocation."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        """Parse the custom mocha reporter output; key tests by `<file>::<fullTitle>`."""
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        depth = 0
        start: Optional[int] = None
        json_blocks: list[str] = []

        for i, ch in enumerate(clean_log):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                if depth == 0 and start is not None:
                    json_blocks.append(clean_log[start : i + 1])
                    start = None

        repo_root = f"/home/{self.pr.repo}/"

        def _key(test: dict) -> str:
            # Leaf `it()` title only (e.g. "should not be open before #open"),
            # NOT the full describe hierarchy — matches project convention of
            # `<file>::<function-name>`. Fall back to fullTitle only if the
            # leaf title is empty (defensive, should never happen in mocha).
            title = test.get("title") or test.get("fullTitle") or ""
            file_path = (test.get("file") or "").strip()
            if file_path.startswith(repo_root):
                file_path = file_path[len(repo_root):]
            if file_path and title:
                return f"{file_path}::{title}"
            return title

        for block in json_blocks:
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(data, dict):
                continue

            for test in data.get("passes", []) or []:
                key = _key(test)
                if key:
                    passed_tests.add(key)

            for test in data.get("failures", []) or []:
                key = _key(test)
                if key:
                    failed_tests.add(key)

            for test in data.get("pending", []) or []:
                key = _key(test)
                if key:
                    skipped_tests.add(key)

        # R2: disjoint sets — a failed test is never counted as a pass.
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
