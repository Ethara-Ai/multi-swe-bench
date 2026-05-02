import re
from json import JSONDecoder
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# TAP format regexes (tape test runner output)
RE_TAP_TEST = re.compile(
    r"^\s*(ok|not ok)\s+\d+\s+(.+?)(?:\s+#\s*(SKIP|TODO)\b.*)?\s*$",
    re.IGNORECASE,
)
RE_TAP_GROUP = re.compile(r"^# (.+)$")
RE_TAP_SUMMARY = re.compile(r"^# (tests|pass|fail|ok|not ok)\b")
RE_TAP_PLAN = re.compile(r"^1\.\.\d+$")


class EslintPluginJsxA11yImageBase(Image):
    def __init__(
        self,
        pr: PullRequest,
        config: Config,
    ):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        dep = self.dependency()
        lines = [
            f"FROM {dep}",
            "",
            "RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*",
            "",
        ]
        if self.config.need_clone:
            lines.extend(
                [
                    "RUN git clone https://github.com/jsx-eslint/eslint-plugin-jsx-a11y.git /repo",
                    "WORKDIR /repo",
                ]
            )
        else:
            lines.extend(
                [
                    "COPY . /repo",
                    "WORKDIR /repo",
                ]
            )
        lines.extend(
            [
                "",
                'CMD ["bash"]',
            ]
        )
        return "\n".join(lines)


class EslintPluginJsxA11yImageDefault(Image):
    def __init__(
        self,
        pr: PullRequest,
        config: Config,
    ):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return EslintPluginJsxA11yImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        fix_patch = File(
            dir=".",
            name="fix.patch",
            content=self.pr.fix_patch,
        )
        test_patch = File(
            dir=".",
            name="test.patch",
            content=self.pr.test_patch,
        )

        prepare_sh = File(
            dir=".",
            name="prepare.sh",
            content="\n".join(
                [
                    "#!/bin/bash",
                    "set -e",
                    "git reset --hard",
                    f"git checkout {self.pr.base.sha}",
                    "rm -rf node_modules",
                    "npm install || true",
                    "npm install --no-save string.prototype.includes || true",
                ]
            ),
        )

        test_command = "\n".join(
            [
                'if grep -q \'"jest"\' package.json 2>/dev/null; then',
                '    npx jest --json --forceExit --outputFile=/tmp/jest-results.json "__tests__/**/*" 2>&1 || true',
                "    cat /tmp/jest-results.json 2>/dev/null || true",
                "else",
                "    npx tape --require=@babel/register '__tests__/**/*.js' 2>&1",
                "fi",
            ]
        )

        run_sh = File(
            dir=".",
            name="run.sh",
            content="\n".join(
                [
                    "#!/bin/bash",
                    "set -eo pipefail",
                    test_command,
                ]
            ),
        )

        test_run_sh = File(
            dir=".",
            name="test-run.sh",
            content="\n".join(
                [
                    "#!/bin/bash",
                    "set -eo pipefail",
                    "git apply --whitespace=nowarn test.patch",
                    test_command,
                ]
            ),
        )

        fix_run_sh = File(
            dir=".",
            name="fix-run.sh",
            content="\n".join(
                [
                    "#!/bin/bash",
                    "set -eo pipefail",
                    "git apply --whitespace=nowarn test.patch",
                    "git apply --whitespace=nowarn fix.patch",
                    test_command,
                ]
            ),
        )

        return [fix_patch, test_patch, prepare_sh, run_sh, test_run_sh, fix_run_sh]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, Image):
            dep_name = f"{dep.image_name()}:{dep.image_tag()}"
        else:
            dep_name = dep

        lines = [f"FROM {dep_name}"]

        if self.config.global_env:
            for key, value in self.config.global_env.items():
                lines.append(f"ENV {key}={value}")

        lines.append("")

        for f in self.files():
            lines.append(f"COPY {f.name} /repo/{f.name}")

        lines.extend(
            [
                "",
                "RUN chmod +x /repo/prepare.sh /repo/run.sh /repo/test-run.sh /repo/fix-run.sh",
                "RUN /repo/prepare.sh",
                "",
            ]
        )

        if self.config.clear_env and self.config.global_env:
            for key in self.config.global_env:
                lines.append(f"ENV {key}=")

        lines.append('CMD ["bash"]')
        return "\n".join(lines)


@Instance.register("jsx-eslint", "eslint-plugin-jsx-a11y")
class EslintPluginJsxA11y(Instance):
    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        *args,
        **kwargs,
    ):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return EslintPluginJsxA11yImageDefault(self.pr, self.config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /repo/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /repo/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /repo/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        cleaned = ANSI_ESCAPE.sub("", test_log)

        result = self._parse_jest_json(cleaned)
        if result is not None:
            return result
        return self._parse_tap(cleaned)

    def _parse_jest_json(self, log: str) -> Optional[TestResult]:
        """Parse Jest JSON output (--json / --outputFile flag)."""
        passed = set()
        failed = set()
        skipped = set()

        # Locate the Jest JSON blob by its known starting key
        match = re.search(r'\{"numFailedTestSuites"', log)
        if not match:
            return None

        # Use strict=False to tolerate control characters in test names
        decoder = JSONDecoder(strict=False)
        try:
            obj, _ = decoder.raw_decode(log, match.start())
        except ValueError:
            return None

        if not isinstance(obj, dict) or "testResults" not in obj:
            return None

        for suite in obj.get("testResults", []):
            for assertion in suite.get("assertionResults", []):
                full_name = assertion.get("fullName", "").strip()
                if not full_name:
                    continue
                status = assertion.get("status", "")
                if status == "passed":
                    if full_name not in failed:
                        passed.add(full_name)
                elif status == "failed":
                    passed.discard(full_name)
                    skipped.discard(full_name)
                    failed.add(full_name)
                elif status == "pending":
                    if full_name not in failed and full_name not in passed:
                        skipped.add(full_name)

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

    def _parse_tap(self, log: str) -> TestResult:
        """Parse TAP (Test Anything Protocol) output from tape runner."""
        passed = set()
        failed = set()
        skipped = set()
        current_group = ""

        for line in log.split("\n"):
            line = line.rstrip()

            if RE_TAP_PLAN.match(line):
                continue
            if RE_TAP_SUMMARY.match(line):
                continue

            group_match = RE_TAP_GROUP.match(line)
            if group_match:
                current_group = group_match.group(1).strip()
                continue

            test_match = RE_TAP_TEST.match(line)
            if test_match:
                status = test_match.group(1).lower()
                test_name = test_match.group(2).strip()
                directive = test_match.group(3)

                if current_group:
                    full_name = f"{current_group}: {test_name}"
                else:
                    full_name = test_name

                if directive and directive.upper() in ("SKIP", "TODO"):
                    if full_name not in failed and full_name not in passed:
                        skipped.add(full_name)
                    continue

                if status == "ok":
                    if full_name not in failed:
                        passed.add(full_name)
                else:
                    passed.discard(full_name)
                    skipped.discard(full_name)
                    failed.add(full_name)

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
