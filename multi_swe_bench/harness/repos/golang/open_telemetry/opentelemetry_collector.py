import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OtelCollectorImageBase(Image):
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
        return "golang:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # Single-newline join (not blank-line separated). DockerfileEnhancer
        # prepends its own blank line before re-emitting the post-FROM content;
        # using "\n\n" here would compound into consecutive blank lines. We
        # also omit items the enhancer already injects (ENV DEBIAN_FRONTEND,
        # ENV LANG, ENV TZ, proxy ARGs, cert symlinks) to avoid duplication.
        sections = [f"FROM {self.dependency()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(code)
        if self.clear_env:
            sections.append(self.clear_env)
        return "\n".join(sections) + "\n"


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
        return OtelCollectorImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                """#!/bin/bash
set -eo pipefail

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
                "find_modules.sh",
                '#!/bin/bash\n'
                'set -eo pipefail\n'
                'REPO_DIR="$1"\n'
                'shift\n'
                'PATCHES="$@"\n'
                '\n'
                'DIRS=$(cat $PATCHES 2>/dev/null | grep \'^diff --git\' | sed \'s|diff --git a/||;s| b/.*||\' | grep \'\\.go$\' | xargs -I{} dirname {} | sort -u)\n'
                '\n'
                'if [ -z "$DIRS" ]; then\n'
                '  echo "ROOT:./..."\n'
                '  exit 0\n'
                'fi\n'
                '\n'
                'declare -A MODULE_PKGS\n'
                '\n'
                'for DIR in $DIRS; do\n'
                '  CHECK="$REPO_DIR/$DIR"\n'
                '  MOD_DIR=""\n'
                '  while [ "$CHECK" != "$REPO_DIR" ] && [ "$CHECK" != "/" ]; do\n'
                '    if [ -f "$CHECK/go.mod" ]; then\n'
                '      MOD_DIR="$CHECK"\n'
                '      break\n'
                '    fi\n'
                '    CHECK=$(dirname "$CHECK")\n'
                '  done\n'
                '  if [ -z "$MOD_DIR" ]; then\n'
                '    MOD_DIR="$REPO_DIR"\n'
                '  fi\n'
                '  REL_MOD="${MOD_DIR#$REPO_DIR/}"\n'
                '  if [ "$MOD_DIR" = "$REPO_DIR" ]; then\n'
                '    REL_MOD="ROOT"\n'
                '  fi\n'
                '  if [ "$REL_MOD" = "ROOT" ]; then\n'
                '    REL_PKG="$DIR"\n'
                '  elif [ "$DIR" = "$REL_MOD" ]; then\n'
                '    REL_PKG="."\n'
                '  else\n'
                '    REL_PKG="${DIR#${REL_MOD}/}"\n'
                '  fi\n'
                '  EXISTING="${MODULE_PKGS[$REL_MOD]:-}"\n'
                '  if [ -n "$EXISTING" ]; then\n'
                '    MODULE_PKGS[$REL_MOD]="$EXISTING ./$REL_PKG"\n'
                '  else\n'
                '    MODULE_PKGS[$REL_MOD]="./$REL_PKG"\n'
                '  fi\n'
                'done\n'
                '\n'
                'for MOD in "${!MODULE_PKGS[@]}"; do\n'
                '  echo "$MOD:${MODULE_PKGS[$MOD]}"\n'
                'done\n',
            ),
            File(
                ".",
                "run_tests_per_module.sh",
                '#!/bin/bash\n'
                'set -eo pipefail\n'
                'REPO_DIR="$1"\n'
                'MODULE_LINES="$2"\n'
                'EXIT_CODE=0\n'
                '\n'
                'while IFS= read -r LINE; do\n'
                '  [ -z "$LINE" ] && continue\n'
                '  MOD="${LINE%%:*}"\n'
                '  PKGS="${LINE#*:}"\n'
                '  if [ "$MOD" = "ROOT" ]; then\n'
                '    MOD_DIR="$REPO_DIR"\n'
                '  else\n'
                '    MOD_DIR="$REPO_DIR/$MOD"\n'
                '  fi\n'
                '  if [ ! -d "$MOD_DIR" ]; then\n'
                '    echo "=== Skipping module: $MOD (directory does not exist) ==="\n'
                '    continue\n'
                '  fi\n'
                '  cd "$MOD_DIR"\n'
                '  VALID_PKGS=""\n'
                '  for PKG in $PKGS; do\n'
                '    PKG_DIR="${PKG#./}"\n'
                '    if [ "$PKG_DIR" = "..." ] || [ -d "$MOD_DIR/$PKG_DIR" ]; then\n'
                '      VALID_PKGS="$VALID_PKGS $PKG"\n'
                '    else\n'
                '      echo "=== Skipping package: $PKG (directory does not exist) ==="\n'
                '    fi\n'
                '  done\n'
                '  VALID_PKGS=$(echo "$VALID_PKGS" | xargs)\n'
                '  if [ -z "$VALID_PKGS" ]; then\n'
                '    echo "=== Skipping module: $MOD (no valid packages) ==="\n'
                '    continue\n'
                '  fi\n'
                '  echo "=== Testing module: $MOD (packages: $VALID_PKGS) ==="\n'
                '  go test -v -count=1 -timeout 15m $VALID_PKGS || EXIT_CODE=$?\n'
                'done <<< "$MODULE_LINES"\n'
                '\n'
                'exit $EXIT_CODE\n',
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

find /home/{pr.repo} -name go.mod -not -path "*/vendor/*" -print0 | while IFS= read -r -d "" GOMOD; do
  MOD_DIR=$(dirname "$GOMOD")
  echo "Running go mod tidy in $MOD_DIR"
  (cd "$MOD_DIR" && go mod tidy 2>&1) || echo "go mod tidy failed in $MOD_DIR (non-fatal)"
done

MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES" || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

# Patch may have modified go.mod/go.sum — run `go mod tidy` so cross-module
# transitive deps (multi-module monorepo) get their go.sum entries populated.
find /home/{pr.repo} -name go.mod -not -path "*/vendor/*" -print0 | while IFS= read -r -d "" GOMOD; do
  (cd "$(dirname "$GOMOD")" && go mod tidy 2>&1) || echo "go mod tidy failed in $(dirname "$GOMOD") (non-fatal)"
done

MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

# Patches may have modified go.mod/go.sum — run `go mod tidy` so cross-module
# transitive deps (multi-module monorepo) get their go.sum entries populated.
find /home/{pr.repo} -name go.mod -not -path "*/vendor/*" -print0 | while IFS= read -r -d "" GOMOD; do
  (cd "$(dirname "$GOMOD")" && go mod tidy 2>&1) || echo "go mod tidy failed in $(dirname "$GOMOD") (non-fatal)"
done

MODULE_LINES=$(bash /home/find_modules.sh /home/{pr.repo} /home/test.patch /home/fix.patch)
bash /home/run_tests_per_module.sh /home/{pr.repo} "$MODULE_LINES"
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        copy_section = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        if copy_section:
            sections.append(copy_section)
        sections.append("RUN bash /home/prepare.sh")
        if self.clear_env:
            sections.append(self.clear_env)
        return "\n\n".join(sections) + "\n"


@Instance.register("open-telemetry", "opentelemetry-collector")
class OpenTelemetryCollector(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"--- PASS: (\S+)"),
            re.compile(r"^ok\s+(\S+)"),
        ]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        for line in test_log.splitlines():
            line = line.strip()

            for r in re_pass_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    if name in failed_tests:
                        continue
                    skipped_tests.discard(name)
                    passed_tests.add(name)

            for r in re_fail_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    passed_tests.discard(name)
                    skipped_tests.discard(name)
                    failed_tests.add(name)

            for r in re_skip_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    if name in failed_tests:
                        continue
                    if name in passed_tests:
                        continue
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
