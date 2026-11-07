from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.4-bookworm"
_BASE_TAG = "base-19363_to_21773"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

_FILES_AWK = r'/^diff --git a\// { sub(/^b\//, "", $4); if ($4 ~ /^packages\/[^\/]+\/.+\.(test|spec)\.[cm]?[jt]sx?$/ && $4 !~ /\.test-d\./) print $4 }'

_CHANGED_AWK = r'/^packages\/[^\/]+\// && $0 !~ /\.(test|spec|test-d)\.[cm]?[jt]sx?$/ && $0 !~ /\/__(snapshots|fixtures)__\// { print $1 "/" $2 }'

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""

_DIGEST_JS = """const fs = require('fs');
const path = require('path');

const label = process.argv[2];
const pkgDir = process.argv[3];
const out = process.argv[4];
if (!fs.existsSync(out)) {
  console.log('MSWB_SUITE_ERROR ' + label + ': no vitest report');
  process.exit(0);
}
const report = JSON.parse(fs.readFileSync(out, 'utf8'));
for (const file of report.testResults || []) {
  const rel = path.relative(pkgDir, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map((x) => String(x).trim()).join(' > ');
    const status = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    console.log('MSWB_RESULT ' + status + ' ' + label + '/' + rel + '::' + title);
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('MSWB_SUITE_ERROR ' + label + '/' + rel + ': ' + String(file.message || '').split('\\n')[0]);
  }
}
"""


def _plan_cmds(repo: str) -> str:
    return f"""cd /home/{repo}

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
[ -s /tmp/msb-test-files.txt ] || {{ echo "=== MSB: no vitest files found in test.patch ==="; exit 1; }}

: > /tmp/msb-plan.txt
for dir in $(sed -E 's#^(packages/[^/]+)/.*#\\1#' /tmp/msb-test-files.txt | sort -u); do
  for cfg in node edge; do
    if [ -f "$dir/vitest.$cfg.config.js" ]; then
      echo "$dir $cfg" >> /tmp/msb-plan.txt
    fi
  done
done
[ -s /tmp/msb-plan.txt ] || {{ echo "=== MSB: no vitest config found for the test packages ==="; exit 1; }}
"""


def _test_cmds(repo: str) -> str:
    return f"""{_plan_cmds(repo)}
{{ git diff --name-only HEAD; git ls-files --others --exclude-standard; }} | awk -F/ '{_CHANGED_AWK}' | sort -u > /tmp/msb-changed.txt
rebuild=""
for dir in $(cat /tmp/msb-changed.txt); do
  if [ -f "$dir/package.json" ]; then
    rebuild="$rebuild --filter=$(node -p "require('./$dir/package.json').name")"
  fi
done
if [ -n "$rebuild" ]; then
  echo "=== MSB: rebuilding changed packages:$rebuild ==="
  ./node_modules/.bin/turbo build $rebuild --concurrency 4 || {{ echo "=== MSB: rebuilding changed packages failed ==="; exit 1; }}
fi

fatal=0
while read -r dir cfg; do
  files=$(grep "^$dir/" /tmp/msb-test-files.txt | sed "s#^$dir/##" | tr '\\n' ' ')
  out="/tmp/msb-vitest-${{dir##*/}}-$cfg.json"
  rm -f "$out"
  echo "=== MSB_SUITE: $dir@$cfg ==="
  rc=0
  vitest="/home/{repo}/$dir/node_modules/.bin/vitest"
  [ -x "$vitest" ] || vitest="/home/{repo}/node_modules/.bin/vitest"
  (cd "$dir" && "$vitest" --config "vitest.$cfg.config.js" --run --passWithNoTests --reporter=json --outputFile="$out" $files) < /dev/null || rc=$?
  [ "$rc" -le 1 ] || {{ echo "=== MSB: vitest in $dir@$cfg exited with status $rc ==="; fatal=$rc; }}
  node /home/mswb_digest.js "$dir@$cfg" "/home/{repo}/$dir" "$out"
  [ -f "$out" ] || [ "$rc" -eq 0 ] || {{ echo "=== MSB: vitest in $dir@$cfg wrote no report (status $rc) ==="; fatal=1; }}
done < /tmp/msb-plan.txt

exit "$fatal"
"""


class Ai19363To21773ImageBase(Image):
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

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN node --version && npm --version && git --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Ai19363To21773ImageDefault(Image):
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
        return Ai19363To21773ImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
            File(".", "mswb_digest.js", _DIGEST_JS),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "{self.pr.base.sha}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "{self.pr.base.sha}"
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

pnpm_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
npm install -g "pnpm@$pnpm_version"
[ "$(pnpm --version)" = "$pnpm_version" ] || {{ echo "prepare.sh: pnpm $pnpm_version is not active"; exit 1; }}

{_plan_cmds(self.pr.repo)}
for attempt in 1 2 3; do
  pnpm install --frozen-lockfile && break
  [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: pnpm install failed 3 times"; exit 1; }}
  sleep 30
done

builds=""
for dir in $(cut -d' ' -f1 /tmp/msb-plan.txt | sort -u); do
  name="$(node -p "require('./$dir/package.json').name")"
  builds="$builds --filter=$name^..."
done
./node_modules/.bin/turbo build $builds --concurrency 4 || {{ echo "prepare.sh: building the workspace dependencies failed"; exit 1; }}

while read -r dir cfg; do
  vitest="$dir/node_modules/.bin/vitest"
  [ -x "$vitest" ] || vitest=node_modules/.bin/vitest
  [ -x "$vitest" ] || {{ echo "prepare.sh: no vitest binary for $dir"; exit 1; }}
  (cd "$dir" && node -e "require.resolve('vitest/package.json')") || {{ echo "prepare.sh: vitest does not resolve in $dir"; exit 1; }}
done < /tmp/msb-plan.txt
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Ai19363To21773ImageDefault dependency must be an Image")
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
    NODE_ENV=test \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    HUSKY=0 \\
    TURBO_TELEMETRY_DISABLED=1 \\
    DO_NOT_TRACK=1 \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("vercel", "ai_19363_to_21773")
class AI_19363_TO_21773(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Ai19363To21773ImageDefault(self.pr, self._config)

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
