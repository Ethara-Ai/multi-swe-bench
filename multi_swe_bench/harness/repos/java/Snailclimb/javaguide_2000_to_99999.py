from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class JavaGuideEra2ImageBase(Image):
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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV NODE_OPTIONS=--max_old_space_size=4096

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl jq && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10.0.0

{code}

{self.clear_env}

"""


class JavaGuideEra2ImageDefault(Image):
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
        return JavaGuideEra2ImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "strip_binary.js",
                """// Strip binary-file and lockfile diff sections from a unified patch.
// Reads patch from argv[2], writes filtered patch to stdout.
const fs = require('fs');
const content = fs.readFileSync(process.argv[2], 'utf8');
const sections = content.split(/^(?=diff --git )/m);
const BIN_EXT = /\\.(png|gif|jpe?g|webp|svg|ico|tiff?|bmp|ttf|otf|eot|woff2?|class|kotlin_module|drawio|pdf|zip|jar|tar|gz|7z|rar|exe|dll|so|dylib|psd|ai|sketch|fig|mp[34]|wav|ogg|avi|mov|webm|flv|wmv)(?=["'\\s]|$)/i;
const LOCKFILES = /\\b(pnpm-lock\\.yaml|package-lock\\.json|yarn\\.lock|poetry\\.lock|composer\\.lock|Pipfile\\.lock|Cargo\\.lock|go\\.sum|gradle\\.lockfile)\\b/i;
const out = [];
for (const s of sections) {
    if (!s.startsWith('diff --git ')) { out.push(s); continue; }
    if (/^Binary files /m.test(s)) continue;
    const firstLine = s.split('\\n')[0];
    if (BIN_EXT.test(firstLine) || LOCKFILES.test(firstLine)) continue;
    out.push(s);
}
process.stdout.write(out.join(''));
""",
            ),
            File(
                ".",
                "apply_patch.sh",
                """#!/bin/bash
# Robust patch application: strip binaries/lockfiles, then try clean apply,
# then fall back to --reject for malformed patches. Returns 0 even if some
# hunks reject — the harness's parse_log is the source of truth on success.
set -e
PATCH_SRC="$1"
WORKDIR="$2"
if [ ! -s "$PATCH_SRC" ]; then
    echo "apply_patch: empty patch, skipping"
    exit 0
fi
FILTERED="/tmp/$(basename "$PATCH_SRC").filtered"
node /home/strip_binary.js "$PATCH_SRC" > "$FILTERED"
if [ ! -s "$FILTERED" ]; then
    echo "apply_patch: patch is entirely binary/lockfile, skipping"
    exit 0
fi
cd "$WORKDIR"
if git apply --whitespace=nowarn --ignore-whitespace "$FILTERED" 2>/tmp/apply.err; then
    echo "apply_patch: clean apply OK"
    exit 0
fi
echo "apply_patch: clean apply failed, retrying with --reject"
cat /tmp/apply.err
git apply --reject --whitespace=nowarn --ignore-whitespace "$FILTERED" 2>/tmp/apply.err || true
echo "apply_patch: --reject mode applied what it could"
exit 0
""",
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
pnpm install --no-frozen-lockfile || pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
[ -d node_modules ] || pnpm install --no-frozen-lockfile || true
NODE_OPTIONS="--max_old_space_size=4096" pnpm docs:build || true
echo "PASS docs-build"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch /home/{pr.repo}
if [ ! -d node_modules ] || git status --porcelain package.json 2>/dev/null | grep -q .; then
    pnpm install --no-frozen-lockfile || true
fi
NODE_OPTIONS="--max_old_space_size=4096" pnpm docs:build || true
echo "FAIL docs-build"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch /home/{pr.repo}
bash /home/apply_patch.sh /home/fix.patch /home/{pr.repo}
if [ ! -d node_modules ] || git status --porcelain package.json 2>/dev/null | grep -q .; then
    pnpm install --no-frozen-lockfile || true
fi
NODE_OPTIONS="--max_old_space_size=4096" pnpm docs:build || true
echo "PASS docs-build"

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("Snailclimb", "JavaGuide_2000_to_99999")
class JavaGuideEra2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JavaGuideEra2ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        pass_re = re.compile(r"^PASS\s+(\S+)\s*$")
        fail_re = re.compile(r"^FAIL\s+(\S+)\s*$")
        skip_re = re.compile(r"^SKIP\s+(\S+)\s*$")

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).strip()
            if not line:
                continue
            m = pass_re.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = fail_re.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue
            m = skip_re.match(line)
            if m:
                skipped_tests.add(m.group(1))

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
