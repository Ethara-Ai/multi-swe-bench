import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Early-era code-server PRs (#2563, #2674).
# Both use ci/dev/test.sh which exists at their base commits:
#   PR #2563 (base c17f3ffc): Mocha — `mocha -r ts-node/register ./test/*.test.ts`
#   PR #2674 (base 4bace1ae): Jest  — `./test/node_modules/.bin/jest` via test/package.json
# Neither has ci/dev/test-unit.sh (introduced in PR #2852).
# Install: yarn install --ignore-scripts (skip lib/vscode/ postinstall).
# test-plugin: must build before running ci/dev/test.sh.
# test/package.json: may or may not exist (absent at #2563, present at #2674).

_EARLY_INSTALL = r"""
# ---------- early-era install ----------
yarn install --ignore-scripts --ignore-engines 2>&1 || true
if [ -d "test/test-plugin" ] && [ -f "test/test-plugin/Makefile" ]; then
    (cd test/test-plugin && yarn install 2>&1 && make -s out/index.js 2>&1) || true
fi
if [ -f "test/package.json" ]; then
    (cd test && yarn install 2>&1) || true
fi
# Ensure node_modules/.bin is in PATH (mocha at #2563 base is called bare)
export PATH="$PWD/node_modules/.bin:$PATH"
# ---------- end early-era install ----------
"""

_EARLY_TEST_CMD = "./ci/dev/test.sh 2>&1 || true"


class CodeServerEarlyImageBase(Image):
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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "pkg-config",
            "libsecret-1-dev",
            "libxkbfile-dev",
            "libx11-dev",
        ]

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
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    pkg-config libsecret-1-dev libxkbfile-dev libx11-dev \\
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable && corepack prepare yarn@1.22.22 --activate
{code}

{self.clear_env}

"""


class CodeServerEarlyImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return CodeServerEarlyImageBase(self.pr, self._config)

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
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{install}

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    install=_EARLY_INSTALL.strip(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}

{install}

{test}
""".format(
                    repo=self.pr.repo,
                    install=_EARLY_INSTALL.strip(),
                    test=_EARLY_TEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{install}

{test}

""".format(
                    repo=self.pr.repo,
                    install=_EARLY_INSTALL.strip(),
                    test=_EARLY_TEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{install}

{test}

""".format(
                    repo=self.pr.repo,
                    install=_EARLY_INSTALL.strip(),
                    test=_EARLY_TEST_CMD,
                ),
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
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
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


@Instance.register("coder", "code_server_2674_to_2563")
class CodeServerEarly(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CodeServerEarlyImageDefault(self.pr, self._config)

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

        # --- Individual test line patterns ---
        # Mocha: ✓ test name (Nms)  |  Jest verbose: ✓ name (Nms)
        pass_re = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        # Mocha failures are numbered:  1) test name  (after the summary)
        # Jest verbose: ✕ name (Nms)
        fail_re = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        # Mocha pending: - test name
        mocha_pending_re = re.compile(r"^\s+-\s+(.+)$")
        # Jest skip: ○ skipped name
        jest_skip_re = re.compile(r"^\s*[○]\s+(?:skipped\s+)?(.+)$")

        # --- Summary patterns ---
        # Mocha: "N passing (Nms)", "N pending", "N failing"
        mocha_passing_re = re.compile(r"^\s*(\d+)\s+passing\b")
        mocha_failing_re = re.compile(r"^\s*(\d+)\s+failing\b")
        mocha_pending_summary_re = re.compile(r"^\s*(\d+)\s+pending\b")

        # Jest summary: "Tests: N failed, N skipped, N passed, N total"
        jest_summary_re = re.compile(
            r"^Tests:\s+"
            r"(?:(\d+)\s+failed,\s*)?"
            r"(?:(\d+)\s+skipped,\s*)?"
            r"(?:(\d+)\s+passed,\s*)?"
            r"(\d+)\s+total$"
        )

        # Mocha numbered failure: "  1) suite name test name"
        mocha_numbered_fail_re = re.compile(r"^\s+(\d+)\)\s+(.+)$")

        summary_passed = 0
        summary_failed = 0
        summary_skipped = 0
        found_summary = False
        in_failures_section = False

        for line in test_log.splitlines():
            stripped = line.strip()

            # --- Jest summary ---
            m_jest = jest_summary_re.match(stripped)
            if m_jest:
                found_summary = True
                summary_failed = int(m_jest.group(1) or 0)
                summary_skipped = int(m_jest.group(2) or 0)
                summary_passed = int(m_jest.group(3) or 0)
                continue

            # --- Mocha summary lines ---
            m_mp = mocha_passing_re.match(stripped)
            if m_mp:
                found_summary = True
                summary_passed = int(m_mp.group(1))
                continue

            m_mf = mocha_failing_re.match(stripped)
            if m_mf:
                found_summary = True
                summary_failed = int(m_mf.group(1))
                in_failures_section = True
                continue

            m_ms = mocha_pending_summary_re.match(stripped)
            if m_ms:
                found_summary = True
                summary_skipped = int(m_ms.group(1))
                continue

            # --- Individual pass ---
            m_pass = pass_re.match(line)
            if m_pass:
                test_name = m_pass.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            # --- Individual fail (Jest ✕ style) ---
            m_fail = fail_re.match(line)
            if m_fail:
                test_name = m_fail.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            # --- Mocha numbered failure ---
            if in_failures_section:
                m_nf = mocha_numbered_fail_re.match(line)
                if m_nf:
                    test_name = m_nf.group(2).strip()
                    if test_name:
                        failed_tests.add(test_name)
                        passed_tests.discard(test_name)
                    continue

            # --- Mocha pending / Jest skip ---
            m_mp2 = mocha_pending_re.match(line)
            if m_mp2:
                test_name = m_mp2.group(1).strip()
                if test_name and test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

            m_js = jest_skip_re.match(line)
            if m_js:
                test_name = m_js.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)
                continue

        if not passed_tests and not failed_tests and not skipped_tests and found_summary:
            for i in range(summary_passed):
                passed_tests.add(f"test_{i + 1}")
            for i in range(summary_failed):
                failed_tests.add(f"failed_test_{i + 1}")
            for i in range(summary_skipped):
                skipped_tests.add(f"skipped_test_{i + 1}")
        elif not skipped_tests and summary_skipped > 0 and found_summary:
            for i in range(summary_skipped):
                skipped_tests.add(f"skipped_test_{i + 1}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
