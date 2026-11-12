"""serverless/serverless -- era 13739..13989 (Jul 2026 .. Oct 2026), 12 PRs.

The v4 monorepo era: npm workspaces (packages/*), lockfileVersion 3,
`packageManager: npm@12.0.1`, `devEngines.runtime.node: ^24.15.0`, jest 30
per workspace (sf-core, serverless, engine, mcp), plus one Go module
(binary-installer/, go 1.26.5 .. 1.27.2) that PR 13917 is entirely about.

Test scope is derived per PR from test.patch, identically in all three stages:
  * packages/<pkg>/**.test.js       -> jest in that workspace (--runTestsByPath)
  * binary-installer/**_test.go     -> `go test -json ./...` in the module
Paths under integration/ or e2e/ are excluded: they deploy to real AWS.

Routing: every JSONL row carries number_interval "serverless_13989_to_13739".
"""

from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "serverless_13989_to_13739"

_NODE_IMAGE = "node:24.15.0-bookworm"
_NPM_VERSION = "12.0.1"
_GO_VERSION = "1.27.2"
# Official checksums from https://go.dev/dl/?mode=json for go1.27.2.
_GO_SHA256 = {
    "amd64": "ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5",
    "arm64": "94f3e30b8e374bc285e7dadc11e0865726b9bc6e85b841ccceaabc0214c6b7c8",
}
_BASE_TAG = "base-13739_to_13989"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

# test.patch -> list of test files (b/ side). Excludes AWS-backed suites.
_FILES_AWK = (
    r'/^diff --git a\// { sub(/^b\//, "", $4); f = $4;'
    r" if (f ~ /\/(integration|e2e)\//) next;"
    r" if (f ~ /^packages\/[^\/]+\/.+\.test\.[cm]?js$/ || f ~ /^binary-installer\/.+_test\.go$/) print f }"
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

# Converts a jest --json report or a `go test -json` stream into MSWB_RESULT
# lines. Test ids put the tool first ("jest:" / "go:") so report.py's
# cheating guard never mistakes the head of a test id for a file path.
_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const kind = process.argv[2];
const label = process.argv[3];
const out = process.argv[4];

if (!fs.existsSync(out)) {
  console.log('MSWB_SUITE_ERROR ' + label + ': no report written');
  process.exit(0);
}

if (kind === 'jest') {
  const pkgDir = process.argv[5];
  let report;
  try {
    report = JSON.parse(fs.readFileSync(out, 'utf8'));
  } catch (e) {
    console.log('MSWB_SUITE_ERROR ' + label + ': unreadable jest report');
    process.exit(0);
  }
  const files = (report.testResults || []).slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  for (const file of files) {
    const rel = path.relative(pkgDir, file.name);
    const results = file.assertionResults || [];
    // Jest allows identical titles in one file (e.g. two `rejects {"url": [Object]}`
    // cases). Number repeats in report order, which is source order and stable.
    const seen = {};
    for (const t of results) {
      const title = [...(t.ancestorTitles || []), t.title]
        .map((x) => String(x).replace(/\s+/g, ' ').trim())
        .join(' > ');
      let id = 'jest:' + label + '/' + rel + '::' + title;
      seen[id] = (seen[id] || 0) + 1;
      if (seen[id] > 1) id = id + ' #' + seen[id];
      const status =
        t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
      console.log('MSWB_RESULT ' + status + ' ' + id);
    }
    if (results.length === 0 && file.status === 'failed') {
      console.log(
        'MSWB_SUITE_ERROR ' + label + '/' + rel + ': ' + String(file.message || '').split('\n').find((l) => l.trim())
      );
    }
  }
} else if (kind === 'go') {
  const lines = fs.readFileSync(out, 'utf8').split('\n');
  for (const line of lines) {
    if (!line.startsWith('{')) continue;
    let ev;
    try {
      ev = JSON.parse(line);
    } catch (e) {
      continue;
    }
    if (!ev.Test) {
      if (ev.Action === 'fail') console.log('MSWB_SUITE_ERROR go:' + ev.Package + ': package failed');
      continue;
    }
    const status =
      ev.Action === 'pass' ? 'PASSED' : ev.Action === 'fail' ? 'FAILED' : ev.Action === 'skip' ? 'SKIPPED' : '';
    if (status) console.log('MSWB_RESULT ' + status + ' go:' + ev.Package + '::' + ev.Test);
  }
}
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_NO_WARNINGS=1
export NODE_OPTIONS="--max-old-space-size=4096 --experimental-vm-modules"
export SLS_TELEMETRY_DISABLED=1
export SERVERLESS_TELEMETRY_DISABLED=1
export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod"""


def _plan_cmds(repo: str) -> str:
    """Write /tmp/msb-test-files.txt and /tmp/msb-plan.txt from test.patch."""
    return f"""cd /home/{repo}

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
[ -s /tmp/msb-test-files.txt ] || {{ echo "=== MSB: no runnable test files in test.patch ==="; exit 1; }}

: > /tmp/msb-plan.txt
for dir in $(grep '^packages/' /tmp/msb-test-files.txt | sed -E 's#^(packages/[^/]+)/.*#\\1#' | sort -u); do
  echo "jest $dir" >> /tmp/msb-plan.txt
done
if grep -q '^binary-installer/' /tmp/msb-test-files.txt; then
  echo "go binary-installer" >> /tmp/msb-plan.txt
fi
[ -s /tmp/msb-plan.txt ] || {{ echo "=== MSB: empty test plan ==="; exit 1; }}
cat /tmp/msb-plan.txt
"""


def _resync_deps(repo: str) -> str:
    """After a patch: re-install only if it changed the root lockfile or a
    workspace manifest (e.g. PR 13989 adds @aws-sdk/client-lambda-web). Lockfile
    driven `npm ci`, so the result is deterministic. Integration fixtures'
    package.json files are not workspaces and are ignored."""
    return f"""cd /home/{repo}
if git status --porcelain | awk '{{print $NF}}' | grep -qE '^(package-lock\\.json|package\\.json|packages/[^/]+/package\\.json)$'; then
  echo "=== MSB: patch changed npm manifests, re-syncing node_modules ==="
  for attempt in 1 2 3; do
    npm ci --no-audit --no-fund && break
    [ "$attempt" -lt 3 ] || {{ echo "=== MSB: npm ci after patch failed 3 times ==="; exit 1; }}
    sleep 30
  done
fi
"""


def _test_cmds(repo: str) -> str:
    return f"""{_plan_cmds(repo)}
fatal=0
while read -r kind dir; do
  label="$dir"
  out="/tmp/msb-report-${{dir//\\//_}}.json"
  rm -f "$out"
  echo "=== MSB_SUITE: $kind $dir ==="
  rc=0
  if [ "$kind" = "jest" ]; then
    files=""
    for f in $(grep "^$dir/" /tmp/msb-test-files.txt); do
      [ -f "$f" ] && files="$files ${{f#$dir/}}"
    done
    if [ -z "$files" ]; then
      echo "=== MSB: no test files of $dir exist at this stage ==="
      continue
    fi
    (cd "$dir" && timeout 3600 /home/{repo}/node_modules/.bin/jest --ci --forceExit --maxWorkers=4 \\
        --json --outputFile="$out" --runTestsByPath $files) < /dev/null || rc=$?
    [ "$rc" -le 1 ] || {{ echo "=== MSB: jest in $dir exited with status $rc ==="; fatal=$rc; }}
    node /home/mswb_digest.js jest "$label" "$out" "/home/{repo}/$dir"
  else
    (cd "$dir" && timeout 3600 go test -json -count=1 ./... > "$out") < /dev/null || rc=$?
    [ "$rc" -le 1 ] || {{ echo "=== MSB: go test in $dir exited with status $rc ==="; fatal=$rc; }}
    node /home/mswb_digest.js go "$label" "$out"
  fi
done < /tmp/msb-plan.txt

exit "$fatal"
"""


class Serverless13989To13739ImageBase(Image):
    """Shared era base: toolchain + full clone. No pin, no scrub (PR layer does both)."""

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
    ca-certificates curl git jq python3 \\
    && rm -rf /var/lib/apt/lists/*

# Go for binary-installer/. Arch from dpkg so the download is correct on
# amd64 and arm64 even when the legacy builder leaves TARGETARCH empty.
RUN arch="$(dpkg --print-architecture)" && \\
    case "$arch" in \\
      amd64) sum="{_GO_SHA256['amd64']}" ;; \\
      arm64) sum="{_GO_SHA256['arm64']}" ;; \\
      *) echo "unsupported arch $arch"; exit 1 ;; \\
    esac && \\
    curl -fsSL "https://go.dev/dl/go{_GO_VERSION}.linux-$arch.tar.gz" -o /tmp/go.tgz && \\
    echo "$sum  /tmp/go.tgz" | sha256sum -c - && \\
    tar -C /usr/local -xzf /tmp/go.tgz && rm -f /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/root/go/bin:$PATH \\
    GOTOOLCHAIN=local

# packageManager: npm@{_NPM_VERSION}
RUN npm install -g npm@{_NPM_VERSION} && \\
    [ "$(npm --version)" = "{_NPM_VERSION}" ] && \\
    node --version && go version && git --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Serverless13989To13739ImageDefault(Image):
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
        return Serverless13989To13739ImageBase(self.pr, self.config)

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
            File(".", "mswb_digest.js", _DIGEST_JS),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

export CI=true
export HUSKY=0
export GOTOOLCHAIN=local
export GOFLAGS=-mod=mod

# --- Section 1: PIN -------------------------------------------------------
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "{sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{sha}"
git checkout --detach {sha}
bash /home/check_git_changes.sh

# --- Section 2: PROVISION -------------------------------------------------
{_plan_cmds(repo)}
for attempt in 1 2 3; do
  npm ci --no-audit --no-fund && break
  [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: npm ci failed 3 times"; exit 1; }}
  sleep 30
done

if grep -q '^go ' /tmp/msb-plan.txt; then
  (cd binary-installer && go mod download)
fi

# --- Section 3: GATE ------------------------------------------------------
# jest workspaces: the test runner resolves AND every declared dependency /
# devDependency of the workspace is installed (workspace or hoisted root
# node_modules), so a partial `npm ci` cannot pass.
# go module: compiles the code AND the test binaries (test-only deps).
[ -x node_modules/.bin/jest ] || {{ echo "prepare.sh: jest binary missing"; exit 1; }}
while read -r kind dir; do
  if [ "$kind" = "jest" ]; then
    (cd "$dir" && node -e "
const fs = require('fs'), path = require('path');
require.resolve('jest/package.json');
const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
const deps = Object.keys(Object.assign({{}}, pkg.dependencies, pkg.devDependencies));
const missing = deps.filter((d) => ![path.join('node_modules', d), path.join('/home/{repo}/node_modules', d)]
  .some((p) => fs.existsSync(path.join(p, 'package.json'))));
if (missing.length) {{ console.error('missing deps in $dir: ' + missing.join(', ')); process.exit(1); }}
console.log('$dir: ' + deps.length + ' deps present');
") || {{ echo "prepare.sh: dependency gate failed in $dir"; exit 1; }}
  else
    (cd "$dir" && go build ./... && go test -count=1 -run '^$' ./... >/dev/null) || {{ echo "prepare.sh: go build/test-compile failed in $dir"; exit 1; }}
  fi
done < /tmp/msb-plan.txt
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
    HUSKY=0 \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("serverless", NUMBER_INTERVAL)
class SERVERLESS_13989_TO_13739(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Serverless13989To13739ImageDefault(self.pr, self._config)

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
