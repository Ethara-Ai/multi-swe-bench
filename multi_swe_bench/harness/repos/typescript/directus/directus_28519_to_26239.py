from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22-bookworm"
_N_VERSION = "12.0.0"
_NODE_RUNTIMES = "22.23.5 26.13.1"
_BASE_TAG = "base-directus_28519_to_26239"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

_FILES_AWK = (
    r'/^diff --git a\// { p = $4; sub(/^b\//, "", p); '
    r'if (p ~ /\.(test|spec)\.[cm]?[jt]sx?$/ && p !~ /^tests\// && p !~ /\.test-d\./) print p }'
)

_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const out = process.argv[3];
if (!fs.existsSync(out)) {
  console.log('MSWB_SUITE_ERROR no vitest report at ' + out);
  process.exit(0);
}
const report = JSON.parse(fs.readFileSync(out, 'utf8'));
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map((x) => String(x).trim()).join(' > ');
    const status = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    console.log('MSWB_RESULT ' + status + ' ' + rel + '::' + title);
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('MSWB_SUITE_ERROR ' + rel + ': ' + String(file.message || '').split('\n')[0]);
  }
}
"""

_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=6144
"""

_PLAN_SH = """awk '{awk}' /home/test.patch | sort -u > /tmp/msb-files.txt
[ -s /tmp/msb-files.txt ] || { echo "=== MSB: no vitest file found in test.patch ==="; exit 1; }

: > /tmp/msb-plan.txt
while read -r f; do
  d="$(dirname "$f")"
  while [ "$d" != "." ] && [ ! -f "$d/package.json" ]; do d="$(dirname "$d")"; done
  [ "$d" != "." ] || continue
  echo "$d" >> /tmp/msb-plan.txt
done < /tmp/msb-files.txt
sort -u -o /tmp/msb-plan.txt /tmp/msb-plan.txt
[ -s /tmp/msb-plan.txt ] || { echo "=== MSB: no workspace package owns the test files ==="; exit 1; }
"""

_SYNC_SH = """changed="$(git status --porcelain -uall | awk '{print $NF}')"
if [ -n "$changed" ]; then
  if echo "$changed" | grep -E '(^|/)(package\\.json|pnpm-lock\\.yaml|pnpm-workspace\\.yaml)$' > /dev/null; then
    pnpm install --frozen-lockfile || pnpm install --offline --no-frozen-lockfile || pnpm install --no-frozen-lockfile
  fi
  pnpm run build
fi
"""

_VITEST_SH = """fatal=0
while read -r dir; do
  files="$(grep "^$dir/" /tmp/msb-files.txt | sed "s#^$dir/##" | tr '\\n' ' ')"
  out="/tmp/msb-vitest-$(echo "$dir" | tr '/' '-').json"
  rm -f "$out"
  vitest="/home/{repo}/$dir/node_modules/.bin/vitest"
  [ -x "$vitest" ] || vitest="/home/{repo}/node_modules/.bin/vitest"
  echo "=== MSB_SUITE: $dir ==="
  rc=0
  (cd "$dir" && timeout 3600 "$vitest" run --passWithNoTests \\
      --reporter=json --outputFile="$out" $files) < /dev/null || rc=$?
  node /tmp/msb-digest.js "/home/{repo}" "$out"
  [ "$rc" -le 1 ] || { echo "=== MSB: vitest in $dir exited with status $rc ==="; fatal=$rc; }
done < /tmp/msb-plan.txt

exit "$fatal"
"""


def _plan() -> str:
    return _PLAN_SH.replace("{awk}", _FILES_AWK)


def _run_tests_sh(repo: str) -> str:
    return (
        _HEADER
        + f"\ncd /home/{repo}\n\n"
        + "cat > /tmp/msb-digest.js <<'MSWBJS'\n"
        + _DIGEST_JS
        + "MSWBJS\n\n"
        + _plan()
        + "\n"
        + _SYNC_SH
        + "\n"
        + _VITEST_SH.replace("{repo}", repo)
    )


def _stage_sh(repo: str, patches: str) -> str:
    apply = f"git apply --whitespace=nowarn {patches}\n\n" if patches else ""
    return _HEADER + f"\ncd /home/{repo}\n\n" + apply + "bash /home/run_tests.sh\n"


def _prepare_sh(repo: str, sha: str) -> str:
    return (
        _HEADER
        + f"""
cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e "{sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{sha}"
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

"""
        + _plan()
        + """
node_major="$(node -p "String(require('./package.json').engines.node).match(/[0-9]+/)[0]")"
node_version="$(ls -1 /usr/local/n/versions/node | grep -E "^$node_major\\." | sort -V | tail -1)"
[ -n "$node_version" ] || { echo "prepare.sh: no node runtime for major $node_major"; exit 1; }
n "$node_version"
test "$(node -v)" = "v$node_version"

pnpm_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
npm install -g "pnpm@$pnpm_version"
test "$(pnpm --version)" = "$pnpm_version"

for attempt in 1 2 3; do
  pnpm install --frozen-lockfile && break
  [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
  sleep 30
done

pnpm run build

while read -r dir; do
  vitest="$dir/node_modules/.bin/vitest"
  [ -x "$vitest" ] || vitest=node_modules/.bin/vitest
  [ -x "$vitest" ] || { echo "prepare.sh: no vitest binary for $dir"; exit 1; }
  (cd "$dir" && node -e "require.resolve('vitest/package.json')")
  "$vitest" --version > /dev/null
done < /tmp/msb-plan.txt

echo "DEPS_OK"
"""
    )


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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


class Directus28519To26239ImageBase(Image):
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
        return self.image_tag()

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

ENV LC_ALL=C.UTF-8 \\
    CI=true \\
    FORCE_COLOR=0 \\
    HUSKY=0 \\
    DO_NOT_TRACK=1 \\
    N_PREFIX=/usr/local \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NODE_OPTIONS=--max-old-space-size=6144 \\
    PNPM_CONFIG_WORKSPACE_CONCURRENCY=2

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g n@{_N_VERSION}

RUN for v in {_NODE_RUNTIMES}; do n install "$v"; done

RUN ls -1 /usr/local/n/versions/node

RUN node --version && npm --version && git --version

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Directus28519To26239ImageDefault(Image):
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
        return Directus28519To26239ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(repo, self.pr.base.sha)),
            File(".", "run_tests.sh", _run_tests_sh(repo)),
            File(".", "run.sh", _stage_sh(repo, "")),
            File(".", "test-run.sh", _stage_sh(repo, "/home/test.patch")),
            File(".", "fix-run.sh", _stage_sh(repo, "/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "Directus28519To26239ImageDefault dependency must be an Image"
            )

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout --detach ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}

{self.clear_env}
"""


@Instance.register("directus", "directus_28519_to_26239")
class DIRECTUS_28519_TO_26239(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Directus28519To26239ImageDefault(self.pr, self._config)

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
