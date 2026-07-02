"""Generic validatorjs/validator.js config (node 20, npm + mocha era).

Used for jsonl records that carry no number_interval, so Instance.create
resolves them to the plain "validatorjs/validator.js" key (the bundle-specific
configs register under validator_js_<range> keys instead). Modeled on
validator_js_931_to_99999 (node:20-bookworm + npm + mocha).
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ValidatorJsImageBase(Image):
    """Level 1: shared node:20 toolchain base (built once, reused by every PR).

    dependency() returns a *string* (node:20-bookworm) and this Dockerfile has
    NO ``# syntax`` directive, so the DockerfileEnhancer engages and prepends the
    infra block (ARG REPO_URL/BASE_COMMIT/TARGETARCH + ENV/cert/label).
    IMPORTANT: this image must NOT clone the repo -- a shared string-dependency
    image that clones is force-pinned to a single ``${BASE_COMMIT}`` and
    history-stripped by the enhancer, breaking ``git checkout`` for every other
    PR that shares the base. So the clone lives per-PR in the ImageDefault
    classes (an Image dependency, left verbatim by the enhancer). This image
    only provides node + the apt build deps, shared across all PR/era configs.
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
        base_img = self.dependency()

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

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

CMD ["/bin/bash"]
"""


class ValidatorJsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        # Level 2: per-PR image FROM the shared ValidatorJsImageBase toolchain.
        # dependency() is an *Image* (not a string), so the DockerfileEnhancer
        # returns dockerfile() verbatim -- the clone/checkout + verbatim
        # Image._HARDENING_BLOCK below are kept exactly as written (and pinning
        # BASE_COMMIT here is correct: it is per-PR, not the shared base).
        return ValidatorJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # Two-level per-PR Dockerfile (mirrors FastfetchImageDefault). The shared
        # toolchain base does NOT clone, so this image clones full history then
        # checks out ${BASE_COMMIT} inline. Because dependency() is an Image, the
        # DockerfileEnhancer returns this Dockerfile verbatim -- the clone +
        # hardening below are kept as written. Image._HARDENING_BLOCK is
        # concatenated raw (not via the f-string) so its ${BASE_COMMIT} /
        # %(refname) tokens stay literal. prepare.sh installs node_modules +
        # builds (network is available at build time, before the hardening
        # strip); node_modules is untracked so the history rewrite leaves it in
        # place for the offline eval runs.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/{file.name}\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail

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
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# installs dependencies and builds so the eval runs don't need network.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

npm install --legacy-peer-deps >/dev/null 2>&1 || true

# devDependencies introduced by the fix patch must be available to the test-only
# stage too (the test patch may import a devDep whose package.json entry lives in
# fix.patch). Apply just package.json from the fix, install, then revert the
# source -- node_modules is untracked so it survives the hardening pass.
if [ -f /home/fix.patch ]; then
    git apply --include=package.json --whitespace=nowarn --ignore-whitespace /home/fix.patch 2>/dev/null || true
    npm install --legacy-peer-deps >/dev/null 2>&1 || true
    git checkout -- package.json 2>/dev/null || true
fi

npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch /home/fix.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --require @babel/register --reporter spec --recursive

""".format(pr=self.pr),
            ),
        ]


@Instance.register("validatorjs", "validator.js")
class ValidatorJs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ValidatorJsImageDefault(self.pr, self._config)

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

        lines = test_log.splitlines()
        current_path = []
        indentation_to_level = {}

        for line in lines:
            line = ansi_escape.sub("", line)

            match = re.match(
                r"^(\s*)(?:([✓✔]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            if indent not in indentation_to_level:
                if not indentation_to_level:
                    indentation_to_level[indent] = 0
                else:
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]
            current_path = current_path[:level]
            current_path.append(name)

            if status:
                full_path = ":".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
