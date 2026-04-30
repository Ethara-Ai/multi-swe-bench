import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- Smart install helper used by prepare.sh, run.sh, test-run.sh, fix-run.sh ---
# Detects whether SKIP_SUBMODULE_DEPS is available (introduced ~PR #5275, commit c51ff3bc).
#   Post-#5275: export SKIP_SUBMODULE_DEPS=1 && yarn install
#   Pre-#5275:  yarn install --ignore-scripts && rebuild argon2 && build test-plugin
_INSTALL_SNIPPET = r"""
# ---------- smart install ----------
export PATH="$PWD/node_modules/.bin:$PATH"
if grep -q "SKIP_SUBMODULE_DEPS" ci/dev/postinstall.sh 2>/dev/null; then
    export SKIP_SUBMODULE_DEPS=1
    export CI=true
    yarn install --ignore-engines 2>&1 || npm install --ignore-scripts 2>&1 || true
    # Manually install test deps and build test-plugin (postinstall may have been skipped)
    if [ -f "test/package.json" ]; then
        (cd test && yarn install 2>&1 || npm install 2>&1) || true
    fi
    if [ -d "test/e2e/extensions/test-extension" ]; then
        (cd test/e2e/extensions/test-extension && yarn install 2>&1 || npm install 2>&1) || true
    fi
    if [ -d "test/unit/node/test-plugin" ] && [ -f "test/unit/node/test-plugin/Makefile" ]; then
        (cd test/unit/node/test-plugin && yarn install 2>&1 && make -s out/index.js 2>&1) || true
    fi
    # Create dummy lib/vscode/out so test-unit.sh doesn't bail with
    # "Code must be built before running unit tests"
    mkdir -p lib/vscode/out 2>/dev/null
else
    yarn install --ignore-scripts --ignore-engines 2>&1 || true
    npm rebuild argon2 2>/dev/null || true
    if [ -d "test/unit/node/test-plugin" ] && [ -f "test/unit/node/test-plugin/Makefile" ]; then
        (cd test/unit/node/test-plugin && yarn install 2>&1 && make -s out/index.js 2>&1) || true
    fi
    if [ -f "test/package.json" ]; then
        (cd test && yarn install 2>&1) || true
    fi
    mkdir -p vendor/modules/code-oss-dev 2>/dev/null
    [ -f vendor/modules/code-oss-dev/package.json ] || echo '{"version":"0.0.0"}' > vendor/modules/code-oss-dev/package.json
    mkdir -p /tmp/code-server/tests/workspaces 2>/dev/null
    grep -rh 'const testName' test/unit/ test/helpers.test.ts 2>/dev/null \
        | grep -oP '"[^"]+"' | tr -d '"' \
        | while read -r d; do mkdir -p "/tmp/code-server/tests/$d" 2>/dev/null; done
fi
# ---------- end smart install ----------
"""

_TEST_CMD = "./ci/dev/test-unit.sh --forceExit --detectOpenHandles --verbose 2>&1 || true"


class CodeServerImageBase(Image):
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
            "jq",
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
    jq pkg-config libsecret-1-dev libxkbfile-dev libx11-dev \\
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable && corepack prepare yarn@1.22.22 --activate
{code}

{self.clear_env}

"""


class CodeServerImageDefault(Image):
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
        return CodeServerImageBase(self.pr, self._config)

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
                    install=_INSTALL_SNIPPET.strip(),
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
                    install=_INSTALL_SNIPPET.strip(),
                    test=_TEST_CMD,
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
                    install=_INSTALL_SNIPPET.strip(),
                    test=_TEST_CMD,
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
                    install=_INSTALL_SNIPPET.strip(),
                    test=_TEST_CMD,
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


@Instance.register("coder", "code-server")
class CodeServer(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CodeServerImageDefault(self.pr, self._config)

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

        pass_re = re.compile(r"^(\s*)[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        fail_re = re.compile(r"^(\s*)[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        skip_re = re.compile(r"^(\s*)[○]\s+(?:skipped\s+)?(.+)$")
        describe_re = re.compile(r"^(\s{2,})(\S.*)$")

        summary_re = re.compile(
            r"^Tests:\s+"
            r"(?:(\d+)\s+failed,\s*)?"
            r"(?:(\d+)\s+skipped,\s*)?"
            r"(?:(\d+)\s+passed,\s*)?"
            r"(\d+)\s+total$"
        )

        summary_passed = 0
        summary_failed = 0
        summary_skipped = 0
        found_summary = False

        describe_stack: list[tuple[int, str]] = []

        def _get_context(indent_len: int) -> str:
            parts = []
            for lvl, name in describe_stack:
                if lvl < indent_len:
                    parts.append(name)
                else:
                    break
            return " › ".join(parts)

        def _update_stack(indent_len: int, name: str) -> None:
            while describe_stack and describe_stack[-1][0] >= indent_len:
                describe_stack.pop()
            describe_stack.append((indent_len, name))

        def _full_name(indent_str: str, test_name: str) -> str:
            indent_len = len(indent_str)
            ctx = _get_context(indent_len)
            if ctx:
                return f"{ctx} › {test_name}"
            return test_name

        for line in test_log.splitlines():
            stripped = line.strip()

            m_summary = summary_re.match(stripped)
            if m_summary:
                found_summary = True
                summary_failed = int(m_summary.group(1) or 0)
                summary_skipped = int(m_summary.group(2) or 0)
                summary_passed = int(m_summary.group(3) or 0)
                continue

            m_pass = pass_re.match(line)
            if m_pass:
                test_name = _full_name(m_pass.group(1), m_pass.group(2).strip())
                if test_name:
                    passed_tests.add(test_name)
                continue

            m_fail = fail_re.match(line)
            if m_fail:
                test_name = _full_name(m_fail.group(1), m_fail.group(2).strip())
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            m_skip = skip_re.match(line)
            if m_skip:
                test_name = _full_name(m_skip.group(1), m_skip.group(2).strip())
                if test_name:
                    skipped_tests.add(test_name)
                continue

            m_desc = describe_re.match(line)
            if m_desc:
                indent_str = m_desc.group(1)
                desc_text = m_desc.group(2).strip()
                if not any(c in desc_text for c in "✓✔✕✗×○●"):
                    _update_stack(len(indent_str), desc_text)

        if not passed_tests and not failed_tests and found_summary:
            for i in range(summary_passed):
                passed_tests.add(f"test_{i + 1}")
            for i in range(summary_failed):
                failed_tests.add(f"failed_test_{i + 1}")
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
