import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TextlintImageBase(Image):
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
        return "node:18"

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


class TextlintImageDefault(Image):
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
        return TextlintImageBase(self.pr, self._config)

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

yarn install --frozen-lockfile || yarn install || true
npx lerna run build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
yarn install --frozen-lockfile || yarn install || true
npx lerna run build || true
npx lerna run test --ignore integration-test --ignore textlint-example-* --ignore textlint-script-* --ignore textlint-website || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git checkout -- . 2>/dev/null
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test.patch 2>/dev/null || true
yarn install --frozen-lockfile || yarn install || true
npx lerna run build || true
npx lerna run test --ignore integration-test --ignore textlint-example-* --ignore textlint-script-* --ignore textlint-website || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git checkout -- . 2>/dev/null
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn /home/fix.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/fix.patch 2>/dev/null || true
yarn install --frozen-lockfile || yarn install || true
npx lerna run build || true
npx lerna run test --ignore integration-test --ignore textlint-example-* --ignore textlint-script-* --ignore textlint-website || true

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.yarnrc && \\
                        echo 'proxy "{proxy_host}:{proxy_port}"' >> $HOME/.yarnrc && \\
                        echo 'https-proxy "{proxy_host}:{proxy_port}"' >> $HOME/.yarnrc && \\
                        echo 'strict-ssl false' >> $HOME/.yarnrc && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.yarnrc $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("textlint", "textlint")
class Textlint(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TextlintImageDefault(self.pr, self._config)

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
        test_log = ansi_re.sub("", test_log)

        summary_passing = 0
        summary_failing = 0

        for line in test_log.splitlines():
            stripped = line.strip()

            m_summary_pass = re.match(r"^(\d+)\s+passing\b", stripped)
            if m_summary_pass:
                summary_passing += int(m_summary_pass.group(1))
                continue

            m_summary_fail = re.match(r"^(\d+)\s+failing\b", stripped)
            if m_summary_fail:
                summary_failing += int(m_summary_fail.group(1))
                continue

            m_summary_pend = re.match(r"^(\d+)\s+pending\b", stripped)
            if m_summary_pend:
                continue

            # Mocha individual test pass: ✓ name (Nms)
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(\d+ms\))?$", stripped)
            if m and m.group(1) not in failed_tests:
                test_name = m.group(1).strip()
                if test_name and not re.match(r"^\d+$", test_name):
                    passed_tests.add(test_name)
                continue

            # Mocha individual test fail: N) test name
            m = re.match(r"\d+\)\s+(.+)$", stripped)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            # Mocha fail markers: ×/✗/✕ test name
            m = re.match(r"[×✗✕]\s+(.+)$", stripped)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            # Mocha pending/skipped: - name
            m = re.match(r"-\s+(.+)$", stripped)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)
                continue

        if not passed_tests and not failed_tests and summary_passing > 0:
            for i in range(summary_passing):
                passed_tests.add(f"test_{i + 1}")
            for i in range(summary_failing):
                failed_tests.add(f"failed_test_{i + 1}")

        # If a test appears in both pass and fail, it failed
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
