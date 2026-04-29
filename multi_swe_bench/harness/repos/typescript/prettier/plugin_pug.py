import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PluginPugImageBase(Image):
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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt update && apt install -y python3
RUN npm install -g pnpm@9
{code}

WORKDIR /home/{self.pr.repo}
RUN echo "*.png binary" >> .git/info/attributes && git checkout -- .
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

CMD ["/bin/bash"]

{self.clear_env}

"""


class PluginPugImageDefault(Image):
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
        return PluginPugImageBase(self.pr, self._config)

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

# Detect package manager from lockfile
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile || yarn install
elif [ -f "pnpm-lock.yaml" ]; then
    pnpm install --no-frozen-lockfile
else
    npm install
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
cd /home/{pr.repo}

# Detect test runner: vitest vs jest
if [ -f "vitest.config.ts" ] || [ -f "vitest.config.js" ] || [ -f "vitest.config.mts" ]; then
    echo "=== Detected Vitest ==="
    npx vitest run --reporter=verbose || true
else
    echo "=== Detected Jest ==="
    ./node_modules/.bin/jest --verbose --no-coverage || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

# Install dependencies
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile || yarn install
elif [ -f "pnpm-lock.yaml" ]; then
    pnpm install --no-frozen-lockfile
else
    npm install
fi

bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch

# Install dependencies (test patch may add new deps)
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile || yarn install
elif [ -f "pnpm-lock.yaml" ]; then
    pnpm install --no-frozen-lockfile
else
    npm install
fi

bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true

# Install dependencies (patches may add new deps)
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile || yarn install
elif [ -f "pnpm-lock.yaml" ]; then
    pnpm install --no-frozen-lockfile
else
    npm install
fi

bash /home/run_tests.sh

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


@Instance.register("prettier", "plugin-pug")
class PluginPug(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PluginPugImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_re.sub("", test_log)

        # Vitest verbose format (check first — more specific patterns):
        #   ✓ tests/options/emptyAttributes/empty-attributes.test.ts > Options > emptyAttributes > should remove... 19ms
        #   × tests/some/test.ts > Suite > should fail 5ms
        vitest_pass_re = re.compile(
            r"^\s*[\u2713\u2714]\s+(.+?)\s+\d+ms$"
        )
        vitest_fail_re = re.compile(
            r"^\s*[\u00d7\u2717\u2715]\s+(.+?)\s+\d+ms$"
        )

        # Jest verbose format:
        #   PASS tests/embed/vue/embed.test.ts
        #   FAIL tests/some/test.ts
        #     ✓ should format when embedded in vue (95 ms)
        #     ✕ failing test (5 ms)
        jest_file_pass_re = re.compile(
            r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"
        )
        jest_file_fail_re = re.compile(
            r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"
        )
        jest_test_pass_re = re.compile(
            r"^\s*[\u2713\u2714]\s+(.+?)(?:\s+\(\d+\s*ms\))?$"
        )
        jest_test_fail_re = re.compile(
            r"^\s*[\u00d7\u2717\u2715]\s+(.+?)(?:\s+\(\d+\s*ms\))?$"
        )

        skipped_re = re.compile(r"SKIP:?\s?(.+?)\s")

        # Detect format: vitest verbose lines end with bare "NNNms" (no parens),
        # jest individual lines have "(NN ms)" with parens.
        # Check first 200 lines for vitest signature.
        is_vitest = False
        for line in test_log.splitlines()[:200]:
            if re.match(r"^\s*[\u2713\u2714]\s+.+\s+\d+ms$", line):
                is_vitest = True
                break

        for line in test_log.splitlines():
            if is_vitest:
                m = vitest_pass_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    if test_name not in failed_tests:
                        passed_tests.add(test_name)
                    continue

                m = vitest_fail_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                    continue
            else:
                # Jest file-level
                m = jest_file_pass_re.match(line)
                if m and m.group(1).strip() not in failed_tests:
                    passed_tests.add(m.group(1).strip())
                    continue

                m = jest_file_fail_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                    continue

                # Jest individual test
                m = jest_test_pass_re.match(line)
                if m and m.group(1).strip() not in failed_tests:
                    passed_tests.add(m.group(1).strip())
                    continue

                m = jest_test_fail_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                    continue

            m = skipped_re.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
