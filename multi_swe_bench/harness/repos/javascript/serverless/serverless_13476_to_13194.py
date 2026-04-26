import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ServerlessJestImageBase(Image):
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
        return "base-jest"

    def workdir(self) -> str:
        return "base-jest"

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

RUN apt update && apt install -y build-essential python3

{code}

{self.clear_env}

"""


class ServerlessJestImageDefault(Image):
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
        return ServerlessJestImageBase(self.pr, self._config)

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

rm -f package-lock.json
npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
cd /home/{pr.repo}

for pkg_dir in packages/sf-core packages/serverless packages/engine; do
    if [ -d "$pkg_dir" ] && [ -f "$pkg_dir/package.json" ]; then
        pkg_name=$(python3 -c "import json; print(json.load(open('$pkg_dir/package.json')).get('name',''))" 2>/dev/null)
        if [ -n "$pkg_name" ]; then
            NODE_OPTIONS="--experimental-vm-modules" NODE_NO_WARNINGS=1 npm run test --workspace="$pkg_name" || true
        fi
    fi
done
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


@Instance.register("serverless", "serverless_13476_to_13194")
class SERVERLESS_13476_TO_13194(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServerlessJestImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            # Jest verbose: ✓ <test name> (<time>) for pass
            m = re.match(r"[✓✔]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$", line)
            if m:
                test_name = m.group(1).strip()
                if test_name and test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            # Jest verbose: ✕ <test name> for fail
            m = re.match(r"[✕✗×]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$", line)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            # Jest FAIL header: "FAIL path/to/test.js"
            m = re.match(r"FAIL\s+(\S+)", line)
            if m:
                failed_tests.add(m.group(1))
                continue

            # Jest skipped: ○ <test name>
            m = re.match(r"○\s+(.*)", line)
            if m:
                test_name = m.group(1).strip()
                if test_name:
                    skipped_tests.add(test_name)

        # Jest summary: "Tests: N failed, N passed, N total"
        test_summary = re.search(r"Tests:\s+(.+)", clean_log)
        if test_summary:
            summary_text = test_summary.group(1)
            passed_match = re.search(r"(\d+)\s+passed", summary_text)
            failed_match = re.search(r"(\d+)\s+failed", summary_text)
            skipped_match = re.search(r"(\d+)\s+skipped", summary_text)

            if passed_match and int(passed_match.group(1)) > 0 and not passed_tests:
                passed_tests.add("ToTal_Test")
            if failed_match and int(failed_match.group(1)) > 0 and not failed_tests:
                failed_tests.add("ToTal_Test")
            if skipped_match and int(skipped_match.group(1)) > 0 and not skipped_tests:
                skipped_tests.add("ToTal_Pending")

        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
