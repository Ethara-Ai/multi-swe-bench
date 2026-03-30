import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Branch-based routing:
#   master  (129 PRs, v1-v5)  => Yarn + Lerna + Jest
#   main    (224 PRs, v6+)    => PNPM + Nx + Vitest
#   v6/v8   (2 PRs)           => same as main
# ---------------------------------------------------------------------------

_MASTER_BRANCHES = {"master"}


def _is_legacy(pr: PullRequest) -> bool:
    """Return True for old Lerna/Yarn/Jest PRs (master branch)."""
    return getattr(pr.base, "ref", "") in _MASTER_BRANCHES


class TypescriptEslintImageDefault(Image):
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
        return "node:22"

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
                "prepare.sh",
                (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "cd /home/{repo}\n"
                    "git reset --hard\n"
                    "git checkout {sha}\n"
                ).format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                ("#!/bin/bash\ncd /home/{repo}\necho run.sh placeholder\n").format(
                    repo=self.pr.repo
                ),
            ),
            File(
                ".",
                "test-run.sh",
                ("#!/bin/bash\ncd /home/{repo}\necho test-run.sh placeholder\n").format(
                    repo=self.pr.repo
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                ("#!/bin/bash\ncd /home/{repo}\necho fix-run.sh placeholder\n").format(
                    repo=self.pr.repo
                ),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = (
            "FROM node:22\n"
            "\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "\n"
            "RUN apt-get update && apt-get install -y git python3 make g++\n"
            "\n"
            "RUN npm install -g corepack 2>/dev/null || true && "
            "corepack enable 2>/dev/null || true\n"
            "\n"
            "WORKDIR /home/\n"
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}\n"
            "\n"
            "WORKDIR /home/{pr.repo}\n"
            "RUN git reset --hard\n"
            "RUN git checkout {pr.base.sha}\n"
            "\n"
            "{copy_commands}\n"
        )
        return dockerfile_content.format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("typescript-eslint", "typescript-eslint")
class TypescriptEslint(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TypescriptEslintImageDefault(self.pr, self._config)

    # ---------- constants ----------

    _APPLY_OPTS = (
        '--whitespace=nowarn --exclude="**/yarn.lock" --exclude="**/pnpm-lock.yaml"'
    )

    # Legacy: Yarn + Lerna + Jest (master, v1-v5)
    _LEGACY_INSTALL = (
        "npm install -g yarn lerna 2>/dev/null || true ; "
        "yarn install --ignore-engines --frozen-lockfile 2>/dev/null || "
        "yarn install --ignore-engines 2>/dev/null || "
        "npm install --legacy-peer-deps 2>/dev/null || true ; "
        "npx lerna bootstrap --no-ci 2>/dev/null || true ; "
    )
    _LEGACY_BUILD = (
        "npx lerna run build --stream 2>/dev/null || yarn build 2>/dev/null || true ; "
    )
    _LEGACY_TEST = "npx lerna run test --stream -- --verbose 2>&1 || true"

    # Modern: detect packageManager field → use yarn or pnpm (main, v6+)
    _MODERN_INSTALL = (
        "PM=$(node -e \"try{console.log(require('./package.json').packageManager||'')}catch(e){console.log('')}\" 2>/dev/null) ; "
        "if echo $PM | grep -q pnpm ; then "
        "corepack disable 2>/dev/null || true ; "
        "npm install -g pnpm 2>/dev/null || true ; "
        "pnpm install --no-frozen-lockfile 2>/dev/null || pnpm install 2>/dev/null || true ; "
        "pnpm add -w @swc/core @swc/jest 2>/dev/null || true ; "
        "elif echo $PM | grep -q yarn ; then "
        "corepack enable 2>/dev/null || true ; "
        "yarn install 2>/dev/null || yarn install --no-immutable 2>/dev/null || true ; "
        "yarn add --dev @swc/core @swc/jest 2>/dev/null || true ; "
        "else "
        "npm install -g pnpm yarn 2>/dev/null || true ; "
        "yarn install --ignore-engines 2>/dev/null || npm install --legacy-peer-deps 2>/dev/null || true ; "
        "npm install --force @swc/core @swc/jest 2>/dev/null || true ; "
        "fi ; "
    )
    _MODERN_BUILD = (
        "PM=$(node -e \"try{console.log(require('./package.json').packageManager||'')}catch(e){console.log('')}\" 2>/dev/null) ; "
        "if echo $PM | grep -q pnpm ; then "
        "pnpm exec nx run-many -t build --parallel 2>/dev/null || pnpm build 2>/dev/null || true ; "
        "elif echo $PM | grep -q yarn ; then "
        "yarn nx run-many -t build --parallel 2>/dev/null || yarn build 2>/dev/null || true ; "
        "else "
        "npx nx run-many -t build --parallel 2>/dev/null || yarn build 2>/dev/null || true ; "
        "fi ; "
    )
    _MODERN_TEST = (
        "PM=$(node -e \"try{console.log(require('./package.json').packageManager||'')}catch(e){console.log('')}\" 2>/dev/null) ; "
        "if echo $PM | grep -q pnpm ; then "
        "pnpm exec nx run-many -t test --parallel -- --verbose --reporter=verbose 2>&1 || pnpm test 2>&1 || true ; "
        "elif echo $PM | grep -q yarn ; then "
        "yarn nx run-many -t test --parallel -- --verbose --reporter=verbose 2>&1 || yarn test 2>&1 || true ; "
        "else "
        "npx nx run-many -t test --parallel -- --verbose --reporter=verbose 2>&1 || yarn test 2>&1 || true ; "
        "fi"
    )

    # ---------- methods ----------

    def _install(self) -> str:
        if _is_legacy(self.pr):
            return self._LEGACY_INSTALL
        return self._MODERN_INSTALL

    def _build(self) -> str:
        if _is_legacy(self.pr):
            return self._LEGACY_BUILD
        return self._MODERN_BUILD

    def _test(self) -> str:
        if _is_legacy(self.pr):
            return self._LEGACY_TEST
        return self._MODERN_TEST

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        repo = self.pr.repo
        return (
            f"bash -c '"
            f"cd /home/{repo} ; "
            f"{self._install()}"
            f"{self._build()}"
            f"{self._test()}"
            f"'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        repo = self.pr.repo
        return (
            f"bash -c '"
            f"cd /home/{repo} ; "
            f"git checkout -- . 2>/dev/null ; "
            f"git apply {self._APPLY_OPTS} /home/test.patch || "
            f"git apply {self._APPLY_OPTS} --3way /home/test.patch || true ; "
            f"{self._install()}"
            f"{self._build()}"
            f"{self._test()}"
            f"'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        repo = self.pr.repo
        return (
            f"bash -c '"
            f"cd /home/{repo} ; "
            f"git checkout -- . 2>/dev/null ; "
            f"git apply {self._APPLY_OPTS} /home/test.patch || "
            f"git apply {self._APPLY_OPTS} --3way /home/test.patch || true ; "
            f"git apply {self._APPLY_OPTS} /home/fix.patch || "
            f"git apply {self._APPLY_OPTS} --3way /home/fix.patch || true ; "
            f"{self._install()}"
            f"{self._build()}"
            f"{self._test()}"
            f"'"
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes
        log = re.sub(r"\x1b\[[0-9;]*m", "", log)

        # --- Jest file-level patterns ---
        jest_pass_file = re.compile(
            r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\d.]*\s*s\))?$", re.MULTILINE
        )
        jest_fail_file = re.compile(
            r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\d.]*\s*s\))?$", re.MULTILINE
        )

        for m in jest_pass_file.finditer(log):
            passed_tests.add(m.group(1).strip())
        for m in jest_fail_file.finditer(log):
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)

        # --- Jest/Vitest individual test patterns ---
        # ✓ test name (Xms)  or  ✔ test name
        ind_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", re.MULTILINE)
        # ✕ test name  or  × test name  or  ✗ test name
        ind_fail = re.compile(r"^\s*[×✗✕]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", re.MULTILINE)
        # ○ test name  or  - test name (skipped)
        ind_skip = re.compile(r"^\s*[○-]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", re.MULTILINE)

        for m in ind_pass.finditer(log):
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
        for m in ind_fail.finditer(log):
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
        for m in ind_skip.finditer(log):
            name = m.group(1).strip()
            skipped_tests.add(name)

        # --- Vitest verbose output: ✓ suite > test name Xms ---
        vitest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)\s+\d+m?s\s*$", re.MULTILINE)
        vitest_fail = re.compile(r"^\s*[×✗✕]\s+(.+?)\s+\d+m?s\s*$", re.MULTILINE)
        for m in vitest_pass.finditer(log):
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
        for m in vitest_fail.finditer(log):
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)

        # --- Lerna stream prefix stripping: e.g. "lerna info @typescript-eslint/parser: ..." ---
        # Also handle nx output: "  @typescript-eslint/parser > test: ..."
        if not passed_tests and not failed_tests:
            stripped = re.sub(
                r"^(?:lerna\s+\w+\s+)?(?:@[\w\-/]+(?:\s*>\s*\w+)?:\s*)",
                "",
                log,
                flags=re.MULTILINE,
            )
            for m in jest_pass_file.finditer(stripped):
                passed_tests.add(m.group(1).strip())
            for m in jest_fail_file.finditer(stripped):
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
            for m in ind_pass.finditer(stripped):
                name = m.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
            for m in ind_fail.finditer(stripped):
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
