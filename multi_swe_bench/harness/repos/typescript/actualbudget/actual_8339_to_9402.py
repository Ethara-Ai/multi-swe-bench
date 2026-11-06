from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.22.1-bookworm"
_IMAGE_NODE = "24.22.1"
_EXTRA_NODES = ("22.18.0", "24.18.1")
_BASE_TAG = "base-8339_to_9402"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

_FILES_AWK = r'/^diff --git a\// { sub(/^b\//, "", $4); if ($4 ~ /^packages\/[^\/]+\/.+\.(test|spec)\.[cm]?[jt]sx?$/ && $4 !~ /\.test-d\./) print $4 }'

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
  case "$dir" in
    packages/loot-core)
      echo "$dir node" >> /tmp/msb-plan.txt
      echo "$dir web" >> /tmp/msb-plan.txt
      ;;
    *) echo "$dir default" >> /tmp/msb-plan.txt ;;
  esac
done
"""


def _test_cmds(repo: str) -> str:
    return f"""{_plan_cmds(repo)}
fatal=0
while read -r dir env; do
  files=$(grep "^$dir/" /tmp/msb-test-files.txt | sed "s#^$dir/##" | tr '\\n' ' ')
  out="/tmp/msb-vitest-${{dir##*/}}-$env.json"
  rm -f "$out"
  echo "=== MSB_SUITE: $dir@$env ==="
  rc=0
  case "$dir@$env" in
    packages/loot-core@node)
      (cd "$dir" && ENV=node yarn vitest --run --passWithNoTests --reporter=json --outputFile="$out" $files) < /dev/null || rc=$? ;;
    packages/loot-core@web)
      (cd "$dir" && ENV=web yarn vitest --run -c vitest.web.config.ts --passWithNoTests --reporter=json --outputFile="$out" $files) < /dev/null || rc=$? ;;
    packages/sync-server@default)
      (cd "$dir" && NODE_ENV=test NODE_OPTIONS="$NODE_OPTIONS --experimental-vm-modules" yarn vitest --run --passWithNoTests --reporter=json --outputFile="$out" $files) < /dev/null || rc=$? ;;
    *)
      (cd "$dir" && yarn vitest --run --passWithNoTests --reporter=json --outputFile="$out" $files) < /dev/null || rc=$? ;;
  esac
  [ "$rc" -le 1 ] || {{ echo "=== MSB: vitest in $dir@$env exited with status $rc ==="; fatal=$rc; }}
  node /home/mswb_digest.js "$dir@$env" "/home/{repo}/$dir" "$out"
  [ -f "$out" ] || [ "$rc" -eq 0 ] || {{ echo "=== MSB: vitest in $dir@$env wrote no report (status $rc) ==="; fatal=1; }}
done < /tmp/msb-plan.txt

exit "$fatal"
"""


class Actual8339To9402ImageBase(Image):
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

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    PATH=/opt/node/current/bin:$PATH

RUN set -eu; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) arch=x64 ;; \\
      arm64) arch=arm64 ;; \\
      *) echo "unsupported architecture"; exit 1 ;; \\
    esac; \\
    mkdir -p /opt/node; \\
    cd /tmp; \\
    for v in {" ".join(_EXTRA_NODES)}; do \\
      tarball="node-v$v-linux-$arch.tar.gz"; \\
      curl -fsSLO --retry 5 "https://nodejs.org/dist/v$v/$tarball"; \\
      curl -fsSL --retry 5 "https://nodejs.org/dist/v$v/SHASUMS256.txt" | awk -v f="$tarball" '$2 == f' > SHASUMS256.txt; \\
      [ -s SHASUMS256.txt ] || {{ echo "no checksum for $tarball"; exit 1; }}; \\
      sha256sum -c SHASUMS256.txt; \\
      mkdir "/opt/node/v$v"; \\
      tar -xzf "$tarball" -C "/opt/node/v$v" --strip-components=1; \\
      rm -f "$tarball" SHASUMS256.txt; \\
      "/opt/node/v$v/bin/corepack" enable; \\
    done; \\
    corepack enable; \\
    ln -s /usr/local /opt/node/v{_IMAGE_NODE}; \\
    ln -sfn /opt/node/v{_IMAGE_NODE} /opt/node/current; \\
    for v in {" ".join(_EXTRA_NODES)} {_IMAGE_NODE}; do \\
      [ "$("/opt/node/v$v/bin/node" -v)" = "v$v" ] || {{ echo "node $v missing"; exit 1; }}; \\
    done

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Actual8339To9402ImageDefault(Image):
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
        return Actual8339To9402ImageBase(self.pr, self.config)

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

node_version="$(tr -d ' \\r\\nv' < .nvmrc)"
[ -d "/opt/node/v$node_version" ] || {{ echo "prepare.sh: Node $node_version from .nvmrc is not in the base image"; exit 1; }}
ln -sfn "/opt/node/v$node_version" /opt/node/current
[ "$(node -v)" = "v$node_version" ] || {{ echo "prepare.sh: Node $node_version is not active"; exit 1; }}

yarn_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
[ "$(yarn --version)" = "$yarn_version" ] || {{ echo "prepare.sh: yarn $yarn_version is not active"; exit 1; }}

for attempt in 1 2 3; do
  yarn install --immutable && break
  [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: yarn install failed 3 times"; exit 1; }}
  sleep 30
done
{_plan_cmds(self.pr.repo)}
while read -r dir env; do
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
            raise ValueError("Actual8339To9402ImageDefault dependency must be an Image")
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
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_NODE_LINKER=node-modules \\
    YARN_ENABLE_TELEMETRY=0 \\
    YARN_ENABLE_PROGRESS_BARS=false \\
    ELECTRON_SKIP_BINARY_DOWNLOAD=1

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("actualbudget", "actual_8339_to_9402")
class ACTUAL_8339_TO_9402(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Actual8339To9402ImageDefault(self.pr, self._config)

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
