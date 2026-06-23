import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class nestImageBase(Image):
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
        return "node:20"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
{code}

{self.clear_env}

"""


class nestImageDefault(Image):
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
        return nestImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || true
npm run build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
npm install --legacy-peer-deps || true
npm run build || true
npm run test || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install --legacy-peer-deps || true
npm run build || true
npm run test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --legacy-peer-deps || true
npm run build || true
npm run test || true

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
        # No proxy/cert/MITM injection (removed per build-env policy).
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("nestjs", "nest")
class Nest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    _APPLY_OPTS = '--whitespace=nowarn --exclude="**/package-lock.json"'

    _ENV_FIX = (
        "export TS_NODE_TRANSPILE_ONLY=true ; "
        "npm install --legacy-peer-deps tsconfig-paths 2>/dev/null || true ; "
        "grep -q baseUrl tsconfig.json 2>/dev/null || "
        r"""node -e "var f=require(\"fs\"),p=\"tsconfig.json\",t=f.readFileSync(p,\"utf8\").replace(/\/\/.*$/gm,\"\").replace(/,\s*([}\]])/g,\"\$1\"),d=JSON.parse(t);d.compilerOptions=d.compilerOptions||{};d.compilerOptions.baseUrl=\".\";f.writeFileSync(p,JSON.stringify(d,null,2))" 2>/dev/null || true ; """
    )

    _BUILD_CMD = "npm run build 2>&1 || npx tsc -b 2>&1 || true"

    _MOCHA_REQUIRES = (
        "--require ts-node/register "
        "--require tsconfig-paths/register "
        "--require node_modules/reflect-metadata/Reflect.js "
    )

    _MOCHA_OPTS = (
        "--exit --timeout 30000 "
        '--ignore "packages/microservices/test/json-socket/message-parsing.spec.ts" '
    )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return nestImageDefault(self.pr, self._config)

    def _get_integration_files(self, *patches: str) -> str:
        files = set()
        for patch in patches:
            for line in patch.splitlines():
                if line.startswith("+++ b/"):
                    fpath = line[6:]
                    if fpath.endswith(".spec.ts") and "integration/" in fpath:
                        files.add(fpath)
        return " ".join(f'"{f}"' for f in sorted(files)) if files else ""

    def _build_mocha_cmd(self, extra_files: str = "") -> str:
        globs = '"packages/**/*.spec.ts"'
        if extra_files:
            globs += " " + extra_files
        return "npx mocha {requires}{opts}{globs} 2>&1 || true".format(
            requires=self._MOCHA_REQUIRES,
            opts=self._MOCHA_OPTS,
            globs=globs,
        )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        mocha = self._build_mocha_cmd()
        return "bash -c 'cd /home/{repo} ; {env}{build} ; {test}'".format(
            repo=self.pr.repo,
            env=self._ENV_FIX,
            build=self._BUILD_CMD,
            test=mocha,
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        integ_str = self._get_integration_files(self.pr.test_patch)
        mocha = self._build_mocha_cmd(integ_str)

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "{env}"
            "{build} ; "
            "{test}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                env=self._ENV_FIX,
                build=self._BUILD_CMD,
                test=mocha,
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        integ_str = self._get_integration_files(self.pr.test_patch, self.pr.fix_patch)
        mocha = self._build_mocha_cmd(integ_str)

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "git apply {opts} /home/fix.patch 2>/dev/null || "
            "git apply {opts} --3way /home/fix.patch 2>/dev/null || true ; "
            "{env}"
            "{build} ; "
            "{test}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                env=self._ENV_FIX,
                build=self._BUILD_CMD,
                test=mocha,
            )
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Mocha output patterns:
        #   ✓ test name (Xms)
        #   N) test name
        #   - test name (pending/skipped)
        # Also matches spec reporter suite-level lines:
        #   N passing (Xs)
        #   N failing
        #   N pending

        passed_res = [
            # ✓ test name (123ms) or ✓ test name
            re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+ms\))?$"),
            # passing (suite level): "  123 passing (1m)"
            re.compile(r"^\s*(\d+)\s+passing\b"),
        ]

        failed_res = [
            # N) test name (numbered failure)
            re.compile(r"^\s*\d+\)\s+(.+)$"),
            # ✗ or × test name
            re.compile(r"^\s*[×✗✕]\s+(.+)$"),
        ]

        skipped_res = [
            # - test name (pending)
            re.compile(r"^\s*-\s+(.+)$"),
        ]

        # Track the summary line counts for validation
        summary_passing = 0
        summary_failing = 0

        for line in test_log.splitlines():
            stripped = line.strip()

            # Parse summary lines
            m_summary_pass = re.match(r"^(\d+)\s+passing\b", stripped)
            if m_summary_pass:
                summary_passing = int(m_summary_pass.group(1))
                continue

            m_summary_fail = re.match(r"^(\d+)\s+failing\b", stripped)
            if m_summary_fail:
                summary_failing = int(m_summary_fail.group(1))
                continue

            m_summary_pend = re.match(r"^(\d+)\s+pending\b", stripped)
            if m_summary_pend:
                continue

            # Match individual test lines
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    test_name = m.group(1).strip()
                    if test_name and not re.match(r"^\d+$", test_name):
                        passed_tests.add(test_name)

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name:
                        failed_tests.add(test_name)
                        if test_name in passed_tests:
                            passed_tests.remove(test_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name:
                        skipped_tests.add(test_name)

        # If individual test matching found nothing but summary exists,
        # use summary counts
        if not passed_tests and not failed_tests and summary_passing > 0:
            for i in range(summary_passing):
                passed_tests.add(f"test_{i + 1}")
            for i in range(summary_failing):
                failed_tests.add(f"failed_test_{i + 1}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
