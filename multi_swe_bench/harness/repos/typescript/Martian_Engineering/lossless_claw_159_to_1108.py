from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.4-bookworm"
_IMAGE_NODE = "22.23.4"
_BASE_TAG = "base-159_to_1108"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

_FILES_AWK = r'/^diff --git a\// { sub(/^b\//, "", $4); if ($4 ~ /^test\/.+\.(test|spec)\.[cm]?[jt]sx?$/) print $4 }'

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""

_DIGEST_JS = """const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const out = process.argv[3];
if (!fs.existsSync(out)) {
  console.log('MSWB_SUITE_ERROR: no vitest report');
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
    console.log('MSWB_SUITE_ERROR ' + rel + ': ' + String(file.message || '').split('\\n')[0]);
  }
}
"""


def _test_cmds(repo: str) -> str:
    return f"""cd /home/{repo}

awk '{_FILES_AWK}' /home/test.patch | sort -u > /tmp/msb-test-files.txt
[ -s /tmp/msb-test-files.txt ] || {{ echo "=== MSB: no vitest files found in test.patch ==="; exit 1; }}

out=/tmp/msb-vitest.json
rm -f "$out"
fatal=0
rc=0
node_modules/.bin/vitest run --dir test --passWithNoTests --reporter=json --outputFile="$out" $(cat /tmp/msb-test-files.txt) < /dev/null || rc=$?
[ "$rc" -le 1 ] || {{ echo "=== MSB: vitest exited with status $rc ==="; fatal=$rc; }}
node /home/mswb_digest.js "/home/{repo}" "$out"
[ -f "$out" ] || [ "$rc" -eq 0 ] || {{ echo "=== MSB: vitest wrote no report (status $rc) ==="; fatal=1; }}

exit "$fatal"
"""


class LosslessClaw159To1108ImageBase(Image):
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

RUN [ "$(node -v)" = "v{_IMAGE_NODE}" ] || {{ echo "node {_IMAGE_NODE} missing"; exit 1; }}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class LosslessClaw159To1108ImageDefault(Image):
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
        return LosslessClaw159To1108ImageBase(self.pr, self.config)

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

[ "$(node -v)" = "v{_IMAGE_NODE}" ] || {{ echo "prepare.sh: Node {_IMAGE_NODE} is not active"; exit 1; }}

for attempt in 1 2 3; do
  npm ci && break
  [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: npm ci failed 3 times"; exit 1; }}
  sleep 30
done

test -x node_modules/.bin/vitest || {{ echo "prepare.sh: vitest is not installed"; exit 1; }}
node -e "require('node:sqlite')" || {{ echo "prepare.sh: node:sqlite is not available"; exit 1; }}
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
            raise ValueError("LosslessClaw159To1108ImageDefault dependency must be an Image")
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
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("Martian-Engineering", "lossless_claw_159_to_1108")
class LOSSLESS_CLAW_159_TO_1108(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return LosslessClaw159To1108ImageDefault(self.pr, self._config)

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
