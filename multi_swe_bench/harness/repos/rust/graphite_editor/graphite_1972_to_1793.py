import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.rust.graphite_editor.patch_utils import (
    filter_binary_diffs,
    get_binary_new_files,
)

EXCLUDES = (
    "--exclude graphite-desktop "
    "--exclude graphite-wasm "
    "--exclude graphite-wasm-svelte "
    "--exclude bezier-rs-wasm "
    "--exclude vulkan-executor "
    "--exclude gpu-compiler-bin-wrapper "
    "--exclude compilation-server "
    "--exclude compilation-client"
)


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

    def dependency(self) -> str:
        return "rust:1.72.0"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _binary_placeholder_cmds(self) -> str:
        bins = set(
            get_binary_new_files(self.pr.fix_patch)
            + get_binary_new_files(self.pr.test_patch)
        )
        if not bins:
            return ""
        lines = []
        for f in sorted(bins):
            lines.append(f'[ ! -f "{f}" ] && mkdir -p "$(dirname "{f}")" && touch "{f}"')
        return "\n".join(lines) + "\n"

    def files(self) -> list[File]:
        placeholder_cmds = self._binary_placeholder_cmds()
        return [
            File(
                ".",
                "fix.patch",
                filter_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                filter_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git fetch origin {pr.base.sha} --depth=1
git checkout {pr.base.sha}

cargo test --workspace {excludes} || true

""".format(pr=self.pr, excludes=EXCLUDES),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
cargo test --workspace {excludes}

""".format(pr=self.pr, excludes=EXCLUDES),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn --reject /home/test.patch || true
find . -name "*.rej" -delete
{placeholder_cmds}cargo test --workspace {excludes}

""".format(pr=self.pr, excludes=EXCLUDES, placeholder_cmds=placeholder_cmds),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
find . -name "*.rej" -delete
{placeholder_cmds}cargo test --workspace {excludes}

""".format(pr=self.pr, excludes=EXCLUDES, placeholder_cmds=placeholder_cmds),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM rust:1.72.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git cmake pkg-config

RUN if [ ! -f /bin/bash ]; then \\
        if command -v apk >/dev/null 2>&1; then \\
            apk add --no-cache bash; \\
        elif command -v apt-get >/dev/null 2>&1; then \\
            apt-get update && apt-get install -y bash; \\
        elif command -v yum >/dev/null 2>&1; then \\
            yum install -y bash; \\
        else \\
            exit 1; \\
        fi \\
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git fetch origin {pr.base.sha} --depth=1 && git checkout {pr.base.sha}
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("GraphiteEditor", "Graphite_1972_to_1793")
class GRAPHITE_1972_TO_1793(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in log.splitlines():
            line = line.strip()
            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))
            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))
            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
