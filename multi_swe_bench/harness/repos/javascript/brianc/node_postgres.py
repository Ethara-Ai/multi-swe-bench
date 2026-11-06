import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.15.0-bookworm"
_YARN_VERSION = "1.22.22"
_BASE_TAG = "base"
_APT_PACKAGES = ["git", "ca-certificates", "coreutils"]
_REPORT_MARKER = "MSB_NODE_POSTGRES_REPORT"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


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


_SUITE_REPORTER_CJS = r"""'use strict';
const fs = require('fs');
const path = require('path');

const suitePath = path.join(process.cwd(), 'test', 'suite.js');
const reportPath = process.env.MSB_SUITE_REPORT;

function record(status, name, err) {
  if (status === 'failed') process.exitCode = 1;
  if (reportPath) fs.appendFileSync(reportPath, JSON.stringify({ name: String(name), status }) + '\n');
  if (err) process.stdout.write('\n' + (err.stack || String(err)) + '\n');
}

if (fs.existsSync(suitePath)) {
  const Suite = require(suitePath);
  Suite.prototype.run = function (test, cb) {
    process.stdout.write('  ' + test.name + ' ');
    if (!test.action) {
      process.stdout.write('? - SKIPPED\n');
      record('skipped', test.name);
      return cb();
    }
    let settled = false;
    let timer = null;
    const finish = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      process.stdout.write(err ? 'FAILED!\n' : '✔\n');
      record(err ? 'failed' : 'passed', test.name, err);
      cb();
    };
    timer = setTimeout(() => finish(new Error('test: ' + test.name + ' did not complete within ' + test.timeout + 'ms')), test.timeout);
    test.run(finish);
  };
  Suite.__msbReporting = true;
}
"""


_REPORT_CJS = r"""'use strict';
const fs = require('fs');

const [, , manifest, marker] = process.argv;

const tests = [];
const seen = new Map();
const clean = (v) => String(v === undefined || v === null ? '' : v).replace(/\s+/g, ' ').trim();

function add(name, status) {
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  tests.push({ name: n > 1 ? `${name} #${n}` : name, status });
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (err) {
    return '';
  }
}

function mocha(file, report, rc) {
  let data = null;
  try {
    const text = readText(report);
    data = text.trim() ? JSON.parse(text) : null;
  } catch (err) {
    process.stderr.write(`report: ${file}: ${err.message}\n`);
  }
  let failed = 0;
  for (const [key, status] of [['passes', 'passed'], ['failures', 'failed'], ['pending', 'skipped']]) {
    for (const t of (data && data[key]) || []) {
      if (status === 'failed') failed += 1;
      add(`${file}::${clean(t.fullTitle || t.title)}`, status);
    }
  }
  if (rc !== '0' && failed === 0) add(`${file}::<file failed to run>`, 'failed');
}

function suite(file, report, rc) {
  let failed = 0;
  for (const line of readText(report).split('\n')) {
    if (!line.trim()) continue;
    let entry;
    try {
      entry = JSON.parse(line);
    } catch (err) {
      continue;
    }
    if (entry.status === 'failed') failed += 1;
    add(`${file}::${clean(entry.name)}`, entry.status);
  }
  if (rc !== '0' && failed === 0) add(`${file}::<file failed to run>`, 'failed');
}

for (const line of readText(manifest).split('\n')) {
  if (!line.trim()) continue;
  const [kind, file, report, rc] = line.split('\t');
  if (kind === 'mocha') mocha(file, report, rc);
  else if (kind === 'suite') suite(file, report, rc);
}

process.stdout.write('\n' + marker + '\n' + JSON.stringify({ msb_tests: tests }) + '\n');
"""


_RUN_ENV = """export CI=true TZ=UTC LC_ALL=C.UTF-8 NODE_ENV=test FORCE_COLOR=0 NO_COLOR=1
export NODE_OPTIONS="--max-old-space-size=4096"
export PGUSER=postgres PGPASSWORD=postgres PGHOST=localhost PGDATABASE=ci_db_test PGTESTNOSSL=true"""


class NodePostgresImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_APT_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)
        parts = [f"FROM {base_img}"]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            "WORKDIR /home/",
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}",
            apt_command,
            f'RUN npm install -g --force yarn@{_YARN_VERSION} && test "$(yarn --version)" = "{_YARN_VERSION}"',
            "RUN git config --global --add safe.directory '*'",
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        parts.append('CMD ["/bin/bash"]')
        return "\n\n".join(parts) + "\n"


class NodePostgresImageDefault(Image):
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
        return NodePostgresImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run_tests(self) -> str:
        repo = self.pr.repo
        return f"""results=/tmp/msb-results
rm -rf "$results"
mkdir -p "$results"
: > "$results/manifest.tsv"
status=0

cd /home/{repo}
yarn build || status=1

cd /home/{repo}/packages/pg
if [ -f test/unit/client/pgpass.file ]; then
    chmod 600 test/unit/client/pgpass.file
fi
for f in $(find test/unit -name '*-tests.js' | LC_ALL=C sort); do
    report="$results/suite-$(printf '%s' "$f" | tr '/' '_').ndjson"
    : > "$report"
    rc=0
    MSB_SUITE_REPORT="$report" timeout --kill-after=30 600 \\
        node -r /home/msb_pg_suite.cjs "$f" postgres:// < /dev/null || rc=$?
    printf 'suite\\t%s\\t%s\\t%s\\n' "packages/pg/$f" "$report" "$rc" >> "$results/manifest.tsv"
    [ "$rc" -eq 0 ] || status=1
done

cd /home/{repo}/packages/pg-connection-string
mocha_bin=$(node -p "require.resolve('mocha/bin/mocha.js', {{paths: [process.cwd()]}})")
for f in $(find test -maxdepth 1 -type f \\( -name '*.ts' -o -name '*.js' \\) ! -name '*.d.ts' | LC_ALL=C sort); do
    report="$results/mocha-$(printf '%s' "$f" | tr '/' '_').json"
    rc=0
    timeout --kill-after=30 600 \\
        node "$mocha_bin" --exit --reporter json --reporter-option output="$report" "$f" < /dev/null || rc=$?
    printf 'mocha\\t%s\\t%s\\t%s\\n' "packages/pg-connection-string/$f" "$report" "$rc" >> "$results/manifest.tsv"
    [ "$rc" -eq 0 ] || status=1
done

cd /home/{repo}
node /home/msb_report.cjs "$results/manifest.tsv" "{_REPORT_MARKER}"
exit "$status"
"""

    def _run_script(self, apply: str) -> str:
        return f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{apply}
{_RUN_ENV}

{self._run_tests()}"""

    def files(self) -> list[File]:
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha
        prepare = f"""#!/bin/bash
set -e

mkdir -p /home/{repo}
cd /home/{repo}
git init -q .
git config --local advice.detachedHead false
git remote add origin "https://github.com/{org}/{repo}.git"
fetched=0
for attempt in 1 2 3 4 5; do
    if git fetch --depth 1 --no-tags origin "{sha}"; then
        fetched=1
        break
    fi
    echo "prepare: fetch attempt ${{attempt}} failed; retrying in 15s" >&2
    sleep 15
done
test "$fetched" -eq 1
git checkout --detach {sha}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

export CI=true
export YARN_CACHE_FOLDER=/tmp/msb-yarn-cache
export NODE_OPTIONS="--max-old-space-size=4096"
node --version
yarn --version

installed=0
for attempt in 1 2 3; do
    yarn install --frozen-lockfile --ignore-scripts --non-interactive --network-timeout 600000 || true
    if [ -f node_modules/.yarn-integrity ]; then
        installed=1
        break
    fi
    echo "prepare: yarn install attempt ${{attempt}} did not complete; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1
rm -rf /tmp/msb-yarn-cache

yarn build
test -f packages/pg-protocol/dist/index.js

(cd packages/pg && node -r /home/msb_pg_suite.cjs -e "if (!require('./test/suite.js').__msbReporting) process.exit(1)")
node -e "require('/home/{repo}/packages/pg'); require('/home/{repo}/packages/pg-connection-string'); for (const dep of ['mocha/bin/mocha.js', 'tsx', 'chai']) require.resolve(dep, {{paths: ['/home/{repo}/packages/pg-connection-string']}}); for (const dep of ['async', 'pgpass']) require.resolve(dep, {{paths: ['/home/{repo}/packages/pg']}}); console.log('DEPS_OK')"
"""
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self._run_script("")),
            File(
                ".",
                "test-run.sh",
                self._run_script("git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._run_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
                ),
            ),
            File(".", "msb_pg_suite.cjs", _SUITE_REPORTER_CJS),
            File(".", "msb_report.cjs", _REPORT_CJS),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha).strip()
        parts = [f"FROM {self.dependency().image_full_name()}"]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            copy_commands.rstrip("\n"),
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            hardening,
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        return "\n\n".join(parts) + "\n"


@Instance.register("brianc", "node-postgres")
class NODE_POSTGRES(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NodePostgresImageDefault(self.pr, self._config)

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
        buckets: dict[str, set[str]] = {"passed": set(), "failed": set(), "skipped": set()}

        lines = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        report = None
        for index, line in enumerate(lines):
            if line.strip() != _REPORT_MARKER:
                continue
            for candidate in lines[index + 1 :]:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "msb_tests" in parsed:
                    report = parsed
                    break
            break

        for entry in (report or {}).get("msb_tests") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            status = str(entry.get("status") or "")
            if name and status in buckets:
                buckets[status].add(name)

        passed_tests = buckets["passed"]
        failed_tests = buckets["failed"]
        skipped_tests = buckets["skipped"]
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
