import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "38553-38888-39575-41276-41445-41781-41958-42075-42138-42142-42227-42734-42943-43240-43246-44757-44793-44794-45580-45993-45995-46127-47287-47712-48139-49789-49890-50095-50129-50431-50646-51569-51964-52504-53389-53567-53980-55722-56372-56399-56574-56593-56607"


class ReactNativeImageBase(Image):
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
        return "node:20-bookworm"

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
            code = f"RUN git clone --no-single-branch https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

{code}

CMD ["/bin/bash"]
"""


class ReactNativeImageDefault(Image):
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
        return ReactNativeImageBase(self.pr, self.config)

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
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn install --frozen-lockfile || yarn install || true
yarn test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
yarn install --frozen-lockfile || yarn install || true
yarn test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
git apply /home/fix.patch
yarn install --frozen-lockfile || yarn install || true
yarn test

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

ARG BASE_COMMIT="{self.pr.base.sha}"

{Image._HARDENING_BLOCK}

"""


@Instance.register("facebook", _NUMBER_INTERVAL)
class ReactNative(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReactNativeImageDefault(self.pr, self._config)

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

        current_suite = ""
        current_suite_status = ""
        has_checkmark_tests = False
        passed_suites = set()
        failed_suites = set()

        for line in test_log.splitlines():
            stripped = line.strip()

            suite_match = re.match(
                r"^(PASS|FAIL)\s+(.+?)(?:\s+\([\d.]+\s*m?s\))?$", stripped
            )
            if suite_match:
                current_suite_status = suite_match.group(1)
                current_suite = suite_match.group(2)
                if current_suite_status == "PASS":
                    passed_suites.add(current_suite)
                else:
                    failed_suites.add(current_suite)
                continue

            pass_match = re.match(
                r"^[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped
            )
            if pass_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {pass_match.group(1)}"
                    if current_suite
                    else pass_match.group(1)
                )
                passed_tests.add(test_name)
                continue

            fail_match = re.match(
                r"^[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped
            )
            if fail_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {fail_match.group(1)}"
                    if current_suite
                    else fail_match.group(1)
                )
                failed_tests.add(test_name)
                continue

            skip_match = re.match(r"^[○⊘]\s+(.+)", stripped)
            if skip_match:
                has_checkmark_tests = True
                test_name = (
                    f"{current_suite} > {skip_match.group(1)}"
                    if current_suite
                    else skip_match.group(1)
                )
                skipped_tests.add(test_name)
                continue

            bullet_match = re.match(r"^●\s+(.+?)\s+›\s+(.+)", stripped)
            if bullet_match and current_suite_status == "FAIL":
                test_name = (
                    f"{current_suite} > {bullet_match.group(1)} › "
                    f"{bullet_match.group(2)}"
                )
                failed_tests.add(test_name)
                continue

        if not has_checkmark_tests:
            for suite in passed_suites:
                passed_tests.add(suite)
            if not failed_tests:
                for suite in failed_suites:
                    failed_tests.add(suite)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
