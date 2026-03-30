import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SemanticReleaseImageDefault(Image):
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
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                (
                    "git config --global user.name github-actions\n"
                    "###ACTION_DELIMITER###\n"
                    "git config --global user.email github-actions@github.com\n"
                    "###ACTION_DELIMITER###\n"
                    "npm install --legacy-peer-deps 2>&1 || npm install 2>&1 || true\n"
                    "###ACTION_DELIMITER###\n"
                    "npm test 2>&1 || true\n"
                ),
            ),
            File(
                ".",
                "run.sh",
                ("#!/bin/bash\ncd /home/{repo}\nnpm test 2>&1\n").format(
                    repo=repo_name
                ),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    "#!/bin/bash\n"
                    "cd /home/{repo}\n"
                    "git apply --whitespace=nowarn /home/test.patch || "
                    "git apply --whitespace=nowarn --3way /home/test.patch || true\n"
                    "npm test 2>&1\n"
                ).format(repo=repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    "#!/bin/bash\n"
                    "cd /home/{repo}\n"
                    "git apply --whitespace=nowarn /home/test.patch || "
                    "git apply --whitespace=nowarn --3way /home/test.patch || true\n"
                    "git apply --whitespace=nowarn /home/fix.patch || "
                    "git apply --whitespace=nowarn --3way /home/fix.patch || true\n"
                    "npm test 2>&1\n"
                ).format(repo=repo_name),
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
            "RUN apt-get update && apt-get install -y git\n"
            "\n"
            "RUN git config --global user.name github-actions && "
            "git config --global user.email github-actions@github.com\n"
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


@Instance.register("semantic-release", "semantic-release")
class SemanticRelease(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SemanticReleaseImageDefault(self.pr, self._config)

    # ---------- inline command overrides ----------
    _APPLY_OPTS = "--whitespace=nowarn"

    _NPM_FIX = (
        "npm pkg delete scripts.pretest 2>/dev/null || true ; "
        "npm pkg delete scripts.prelint 2>/dev/null || true ; "
        "sed -i s/c8\\ ava/ava/g package.json 2>/dev/null || true ; "
        "sed -i s/nyc\\ ava/ava/g package.json 2>/dev/null || true ; "
        "grep -q npm-run-all package.json && "
        "npm pkg set scripts.test=ava\\ --verbose 2>/dev/null || true ; "
        "npm install --legacy-peer-deps 2>/dev/null || "
        "npm install 2>/dev/null || true ; "
    )

    _TEST_CMD = "npm test 2>&1 || true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        repo = self.pr.repo
        return f"bash -c 'cd /home/{repo} ; {self._NPM_FIX}{self._TEST_CMD}'"

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
            f"{self._NPM_FIX}"
            f"{self._TEST_CMD}"
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
            f"{self._NPM_FIX}"
            f"{self._TEST_CMD}"
            f"'"
        )

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        log = re.sub(r"^\[test:\w+\s*\]\s*", "", log, flags=re.MULTILINE)

        ava_pattern = re.compile(
            r"^\s*([✔✓✖✗×◌])\s+(.+?)(?:\s+\(\d+\.?\d*(?:ms|s)\))?$",
            re.MULTILINE,
        )
        for match in ava_pattern.finditer(log):
            symbol = match.group(1)
            test_name = match.group(2).strip()
            # Remove trailing ANSI escape codes
            test_name = re.sub(r"\x1b\[[0-9;]*m", "", test_name).strip()
            if symbol in ("✔", "✓"):
                passed_tests.add(test_name)
            elif symbol in ("✖", "✗", "×"):
                failed_tests.add(test_name)
            elif symbol == "◌":
                skipped_tests.add(test_name)

        # Fallback: TAP format for older PRs that may use tap instead of AVA
        if not passed_tests and not failed_tests:
            tap_passed = re.compile(r"^ok\s+\d+\s+-\s+(.*?)(?:\s+#.*)?$", re.MULTILINE)
            tap_failed = re.compile(
                r"^not ok\s+\d+\s+-\s+(.*?)(?:\s+#.*)?$", re.MULTILINE
            )
            tap_skip = re.compile(r"^ok\s+\d+\s+-\s+(.*?)\s+#\s+SKIP", re.MULTILINE)
            for m in tap_skip.findall(log):
                skipped_tests.add(m.strip())
            for m in tap_passed.findall(log):
                name = m.strip()
                if name not in skipped_tests:
                    passed_tests.add(name)
            for m in tap_failed.findall(log):
                failed_tests.add(m.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
