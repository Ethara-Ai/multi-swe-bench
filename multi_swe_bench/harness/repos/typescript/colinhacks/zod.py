import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ZodImageBase(Image):
    """Level 1: toolchain-only base image (shared by all PRs of the era).

    ``dependency()`` returns a *string* (the Node toolchain image) and this
    Dockerfile carries NO ``# syntax`` directive, so the pipeline's
    DockerfileEnhancer engages and prepends the ``# syntax``/ARG/ENV/LABEL infra
    block. IMPORTANT: this image must NOT clone the repository -- a shared
    string-dependency image that performs a ``git clone`` is force-pinned to a
    single ``${BASE_COMMIT}`` and history-stripped by the enhancer, which would
    break ``git checkout`` for every other PR sharing the base. So the clone
    lives in ZodImageDefault (whose dependency() is an Image, left verbatim by
    the enhancer), done per-PR. This image only provides the Node toolchain and
    apt deps.
    """

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {image_name}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git make python3 \\
    && rm -rf /var/lib/apt/lists/*

CMD ["/bin/bash"]
"""


class ZodImageDefault(Image):
    """Level 2: per-PR image (built on the shared toolchain base).

    ``dependency()`` returns ZodImageBase (an Image, not a string), so the
    DockerfileEnhancer returns this Dockerfile verbatim -- no pin, no history
    strip injected by the pipeline. The clone therefore lives here, per-PR: the
    image clones full history, checks out ``${BASE_COMMIT}`` inline, COPYs the
    scripts, warms the yarn cache (install.sh), then the verbatim
    ``Image._HARDENING_BLOCK`` strips origin/refs/future history (with the four
    post-condition asserts + submodule pass) while keeping base.sha reachable.
    """

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
        return ZodImageBase(self.pr, self._config)

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
                "install.sh",
                # Checkout is done inline in the Dockerfile (RUN git checkout
                # ${BASE_COMMIT}) and hardening is the inline Image._HARDENING_BLOCK;
                # install.sh only warms the yarn cache while the full clone is
                # still present (it runs BEFORE the hardening strip).
                """#!/bin/bash
set -e
cd /home/{pr.repo}
yarn install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
yarn install || true
yarn build || true
yarn test --verbose --ci || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
yarn install || true
yarn build || true
yarn test --verbose --ci || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
yarn install || true
yarn build || true
yarn test --verbose --ci || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("colinhacks", "zod")
class Zod(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ZodImageDefault(self.pr, self._config)

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

        current_suite = None

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
