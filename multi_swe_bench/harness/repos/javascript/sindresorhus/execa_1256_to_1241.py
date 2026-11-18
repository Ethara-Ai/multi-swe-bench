"""sindresorhus/execa -- era 1241..1256 (Jul 2026), 10 PRs.

The v10 era: pure-ESM package (`"type": "module"`), no committed lockfile
(`.npmrc` sets `package-lock=false`), tests split over two runners:

  * test/**/*.js            -> AVA, run per file with `--tap`
  * test-d/**/*.test-d.ts   -> tsd, run once over the selected files

AVA's own default patterns exclude `test/helpers/**` and `test/fixtures/**`
(ava/lib/globs.js: `!**/test?(s)/**/{helper,fixture}?(s)/**/*`), so those two
directories are dropped from the plan -- they hold no tests and AVA refuses
them as targets. `test-d` helpers are plain `.ts` (test-d/ipc/graceful.ts,
test-d/return/result-ipc.ts) and are excluded by the `.test-d.ts` suffix.

Test scope is derived per PR from test.patch, identically in all three stages,
so a name means the same thing in run/test/fix.

Toolchain evidence (read at the era's base commits, not assumed):
  * PR 1241 base 68986b79d6: engines.node "^18.19.0 || >=20.5.0", ava ^6.3.0,
    typescript ^5.8.3, tsd ^0.32.0
  * PRs 1244..1256 bases: engines.node ">=22", ava ^8.0.1, typescript ^6.0.3,
    tsd ^0.33.0
  node:24.15.0-bookworm satisfies every row: ava 6.3.0 engines are
  "^18.18 || ^20.8 || ^22 || >=23" and ava 8.0.1 engines are
  "^22.20 || ^24.12 || >=26", and 24.15 >= 24.12.

Routing: every JSONL row carries number_interval "execa_1256_to_1241".
"""

from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "execa_1256_to_1241"

_NODE_IMAGE = "node:24.15.0-bookworm"
_BASE_TAG = "base-1241_to_1256"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

# test.patch -> test plan, one "<runner> <path>" line per graded file.
# $4 is the "b/<path>" side of the `diff --git` header.
_PLAN_AWK = (
    r'/^diff --git a\// { sub(/^b\//, "", $4); f = $4;'
    r" if (f ~ /^test\/(fixtures|helpers)\//) next;"
    r' if (f ~ /^test\/.+\.js$/) { print "ava " f; next }'
    r' if (f ~ /^test-d\/.+\.test-d\.ts$/) { print "tsd " f } }'
)

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

# Turns one AVA TAP stream, or one tsd run, into MSWB_RESULT lines.
#
#   mswb_digest.cjs tap <relative test file> <tap output file>
#   mswb_digest.cjs tsd <repo dir> <relative test-d file>...
#
# Test ids put the runner first ("ava:" / "tsd:") so report.py's cheating
# guard never mistakes the head of a test id for a repository path. The file
# path is supplied by the caller rather than taken from AVA's own title
# prefixing, which only kicks in when more than one file is in the run
# (ava/lib/reporters/tap.js) and would otherwise make ids depend on how many
# files a given stage happened to select.
_DIGEST_CJS = r"""'use strict';
const fs = require('fs');
const path = require('path');
const {createRequire} = require('module');

const mode = process.argv[2];

const emit = (status, id) => {
  console.log('MSWB_RESULT ' + status + ' ' + id);
};

const firstLine = (text) =>
  String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .find((line) => line.length > 0) || 'no output';

if (mode === 'tap') {
  const file = process.argv[3];
  const out = process.argv[4];

  if (!fs.existsSync(out)) {
    console.log('MSWB_SUITE_ERROR ava:' + file + ': no TAP output written');
    process.exit(0);
  }

  const text = fs.readFileSync(out, 'utf8');
  const lines = text.split('\n');
  const seen = Object.create(null);
  let count = 0;

  for (const raw of lines) {
    const line = raw.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
    const match = /^(not ok|ok)\s+\d+\s*-?\s*(.*)$/.exec(line);
    if (!match) {
      continue;
    }

    let title = match[2].trim();
    let status = match[1] === 'ok' ? 'PASSED' : 'FAILED';

    const directive = /\s+#\s+(SKIP|TODO)\b.*$/i.exec(title);
    if (directive) {
      title = title.slice(0, directive.index).trim();
      status = 'SKIPPED';
    }

    if (title === '') {
      title = '(untitled)';
    }

    // AVA rejects duplicate titles inside one file, so this only guards
    // against a crash report reusing a title.
    let id = 'ava:' + file + '::' + title;
    seen[id] = (seen[id] || 0) + 1;
    if (seen[id] > 1) {
      id = id + ' #' + seen[id];
    }

    emit(status, id);
    count++;
  }

  if (count === 0) {
    console.log('MSWB_SUITE_ERROR ava:' + file + ': ' + firstLine(text));
  }

  process.exit(0);
}

if (mode === 'tsd') {
  const repoDir = process.argv[3];
  const files = process.argv.slice(4);

  if (files.length === 0) {
    process.exit(0);
  }

  let tsd;
  try {
    // tsd (0.32/0.33) declares no `main`, only `exports: "./dist/index.js"`.
    // `require()` of an absolute package DIRECTORY bypasses the exports field
    // and then finds no entry point, so it must be resolved by package NAME
    // through a require anchored inside the repo.
    tsd = createRequire(path.join(repoDir, 'package.json'))('tsd');
  } catch (error) {
    console.log('MSWB_SUITE_ERROR tsd: cannot load tsd: ' + firstLine(error && error.message));
    process.exit(1);
  }

  const run = typeof tsd === 'function' ? tsd : tsd.default;

  run({cwd: repoDir, testFiles: files})
    .then((diagnostics) => {
      const broken = new Set();
      const foreign = [];

      for (const diagnostic of diagnostics || []) {
        if (diagnostic.severity !== 'error') {
          continue;
        }
        let name = diagnostic.fileName || '';
        if (path.isAbsolute(name)) {
          name = path.relative(repoDir, name);
        }
        name = name.split(path.sep).join('/');
        if (files.includes(name)) {
          broken.add(name);
        } else {
          foreign.push(name + ': ' + firstLine(diagnostic.message));
        }
      }

      for (const file of files) {
        emit(broken.has(file) ? 'FAILED' : 'PASSED', 'tsd:' + file);
      }

      for (const message of foreign.slice(0, 20)) {
        console.log('MSWB_SUITE_ERROR tsd: ' + message);
      }

      process.exit(0);
    })
    .catch((error) => {
      console.log('MSWB_SUITE_ERROR tsd: ' + firstLine(error && error.message));
      process.exit(1);
    });
} else if (mode !== 'tap') {
  console.log('MSWB_SUITE_ERROR digest: unknown mode ' + String(mode));
  process.exit(1);
}
"""

# prepare.sh's hard gate: every declared dependency resolves, both test
# runners load. Deep enough that a half-finished `npm install` cannot pass.
_GATE_CJS = r"""'use strict';
const fs = require('fs');
const path = require('path');
const {createRequire} = require('module');

const repoDir = process.argv[2];
const pkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
const declared = Object.keys(Object.assign({}, pkg.dependencies, pkg.devDependencies));

const missing = declared.filter(
  (name) => !fs.existsSync(path.join(repoDir, 'node_modules', name, 'package.json')),
);

if (missing.length > 0) {
  console.error('gate: missing dependencies: ' + missing.join(', '));
  process.exit(1);
}

// The runners are checked through their CLI entry points, not `require()`:
// ava is ESM-only (`"type": "module"`, no `require` condition in `exports`),
// so requiring it from CJS throws even on a perfectly good install.
// prepare.sh then executes `ava --version` and `tsd --version`, which loads
// each package for real.
// Load tsd exactly as mswb_digest.cjs does (by package NAME, through a require
// anchored in the repo): tsd declares no `main`, so a by-directory require
// resolves to nothing. Doing it here means a broken tsd load fails the build.
try {
  const tsd = createRequire(path.join(repoDir, 'package.json'))('tsd');
  const run = typeof tsd === 'function' ? tsd : tsd.default;
  if (typeof run !== 'function') {
    console.error('gate: tsd loaded but is not callable');
    process.exit(1);
  }
} catch (error) {
  console.error('gate: cannot load tsd: ' + (error && error.message));
  process.exit(1);
}

for (const binary of ['ava', 'tsd', 'tsc']) {
  const entry = path.join(repoDir, 'node_modules', '.bin', binary);
  if (!fs.existsSync(entry)) {
    console.error('gate: missing executable ' + entry);
    process.exit(1);
  }
  try {
    fs.accessSync(entry, fs.constants.X_OK);
  } catch (error) {
    console.error('gate: not executable: ' + entry);
    process.exit(1);
  }
}

console.log('gate: ' + declared.length + ' declared dependencies present');
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false"""


def _plan_cmds(repo: str) -> str:
    """Write /tmp/msb-plan.txt from test.patch."""
    return f"""cd /home/{repo}

awk '{_PLAN_AWK}' /home/test.patch | sort -u > /tmp/msb-plan.txt
[ -s /tmp/msb-plan.txt ] || {{ echo "=== MSB: no runnable test files in test.patch ==="; exit 1; }}
cat /tmp/msb-plan.txt
"""


def _resync_deps(repo: str) -> str:
    """After a patch: re-install when it changed package.json. PR 1251 drops
    cross-spawn and takes a new runtime dependency, so the patched tree needs
    dependencies the pre-patch install never fetched. There is no committed
    lockfile (.npmrc: package-lock=false), so `npm install` is the only
    install form available."""
    return f"""cd /home/{repo}
if git status --porcelain | awk '{{print $NF}}' | grep -qx 'package.json'; then
  echo "=== MSB: patch changed package.json, re-syncing node_modules ==="
  for attempt in 1 2 3; do
    npm install --no-audit --no-fund && break
    [ "$attempt" -lt 3 ] || {{ echo "=== MSB: npm install after patch failed 3 times ==="; exit 1; }}
    sleep 30
  done
fi
"""


def _test_cmds(repo: str) -> str:
    """The one test command, shared verbatim by run.sh, test-run.sh and
    fix-run.sh. Files absent at a given stage are skipped, not fatal: a test
    file added by test.patch does not exist during the baseline stage."""
    return f"""{_plan_cmds(repo)}
fatal=0

while read -r kind file; do
  [ "$kind" = "ava" ] || continue
  if [ ! -f "$file" ]; then
    echo "=== MSB: $file absent at this stage ==="
    continue
  fi
  out="/tmp/msb-ava-$(printf '%s' "$file" | tr -c 'A-Za-z0-9._-' '_').tap"
  rm -f "$out"
  echo "=== MSB_SUITE: ava $file ==="
  rc=0
  timeout 3600 /home/{repo}/node_modules/.bin/ava --tap "$file" > "$out" 2>&1 < /dev/null || rc=$?
  [ "$rc" -le 1 ] || {{ echo "=== MSB: ava $file exited with status $rc ==="; fatal=$rc; }}
  node /home/mswb_digest.cjs tap "$file" "$out"
done < /tmp/msb-plan.txt

tsd_files=""
while read -r kind file; do
  [ "$kind" = "tsd" ] || continue
  if [ ! -f "$file" ]; then
    echo "=== MSB: $file absent at this stage ==="
    continue
  fi
  tsd_files="$tsd_files $file"
done < /tmp/msb-plan.txt

if [ -n "$tsd_files" ]; then
  echo "=== MSB_SUITE: tsd$tsd_files ==="
  rc=0
  timeout 3600 node /home/mswb_digest.cjs tsd "/home/{repo}" $tsd_files < /dev/null || rc=$?
  [ "$rc" -eq 0 ] || {{ echo "=== MSB: tsd digest exited with status $rc ==="; fatal=$rc; }}
fi

exit "$fatal"
"""


class Execa1256To1241ImageBase(Image):
    """Shared era base: toolchain + full clone. No pin, no scrub -- the PR
    layer does both, so every PR in the era can still reach its own commit."""

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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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
# Declared so the build arg the harness passes to every image is not an
# unused-arg warning. NEVER referenced below: this base is shared by the whole
# era, so consuming it would pin one era image to a single PR's commit.
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential ca-certificates curl git jq python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version && git --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Execa1256To1241ImageDefault(Image):
    """Per-PR instance image: pin, provision, scrub."""

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
        return Execa1256To1241ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswb_digest.cjs", _DIGEST_CJS),
            File(".", "mswb_gate.cjs", _GATE_CJS),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

export CI=true
export NPM_CONFIG_UPDATE_NOTIFIER=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false

# --- Section 1: PIN -------------------------------------------------------
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "{sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{sha}"
git checkout --detach {sha}
bash /home/check_git_changes.sh

# --- Section 2: PROVISION -------------------------------------------------
# No committed lockfile (.npmrc: package-lock=false), so `npm install` is the
# only install form; the era's package.json ranges pin the majors.
for attempt in 1 2 3; do
  npm install --no-audit --no-fund && break
  [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: npm install failed 3 times"; exit 1; }}
  sleep 30
done

# --- Section 3: GATE ------------------------------------------------------
# Every declared dependency resolves and both runners load, so a partial
# install cannot pass. Then each runner is executed for real.
node /home/mswb_gate.cjs /home/{repo} || {{ echo "prepare.sh: dependency gate failed"; exit 1; }}
/home/{repo}/node_modules/.bin/ava --version
/home/{repo}/node_modules/.bin/tsd --version
echo DEPS_OK
""",
            ),
            File(".", "run.sh", f"{_RUN_HEADER}\n\n{_test_cmds(repo)}"),
            File(
                ".",
                "test-run.sh",
                f"""{_RUN_HEADER}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{_resync_deps(repo)}
{_test_cmds(repo)}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{_RUN_HEADER}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_resync_deps(repo)}
{_test_cmds(repo)}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    CI=true \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("sindresorhus", NUMBER_INTERVAL)
class EXECA_1256_TO_1241(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Execa1256To1241ImageDefault(self.pr, self._config)

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

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            match = _RESULT_RE.match(raw.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "PASSED":
                passed.add(name)
            elif status == "FAILED":
                failed.add(name)
            else:
                skipped.add(name)

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
