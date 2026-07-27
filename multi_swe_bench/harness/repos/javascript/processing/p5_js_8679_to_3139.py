import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "base-era1b"

    def workdir(self) -> str:
        return "base-era1b"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Shared per-era base: installs the toolchain, clones the repo (default
        # branch, via ${REPO_URL}) and warms the COMMON npm dependencies once, so
        # every per-PR image in this era reuses them. The base pins NO commit and
        # does NOT harden -- it is shared across all era PRs; the per-PR
        # ImageDefault checks out its own ${BASE_COMMIT} in this inherited clone
        # and applies the history scrub. `# syntax` keeps DockerfileEnhancer from
        # rewriting/pinning the base clone.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y chromium

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN npm install || true

{self.clear_env}

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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

ln -sf /usr/bin/chromium /usr/bin/chromium-browser
PUPPETEER_SKIP_DOWNLOAD=true npm install

# Generate parameterData.json required by p5.js build
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}, preprocessor: './docs/preprocessor.js'}}).run(); }} catch(e) {{ console.log('yuidoc skipped:', e.message); }}" || true
mkdir -p docs
[ -f docs/parameterData.json ] || echo '{{}}' > docs/parameterData.json
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
npx grunt browserify:test || true
timeout 1200 npx grunt connect:server mochaChrome:test mochaTest || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch || true
PUPPETEER_SKIP_DOWNLOAD=true npm install
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Regenerate parameterData.json in case test patch changes src/
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}, preprocessor: './docs/preprocessor.js'}}).run(); }} catch(e) {{ console.log('yuidoc skipped:', e.message); }}" || true
mkdir -p docs
[ -f docs/parameterData.json ] || echo '{{}}' > docs/parameterData.json

npx grunt browserify:test || true
timeout 1200 npx grunt connect:server mochaChrome:test mochaTest || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch /home/fix.patch || true
PUPPETEER_SKIP_DOWNLOAD=true npm install
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Regenerate parameterData.json in case patches change src/
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}, preprocessor: './docs/preprocessor.js'}}).run(); }} catch(e) {{ console.log('yuidoc skipped:', e.message); }}" || true
mkdir -p docs
[ -f docs/parameterData.json ] || echo '{{}}' > docs/parameterData.json

npx grunt browserify:test || true
timeout 1200 npx grunt connect:server mochaChrome:test mochaTest || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_files = " ".join(file.name for file in self.files())

        # The shared base already cloned the repo and warmed the common deps, so
        # this per-PR image REUSES that clone: it checks out its own ${BASE_COMMIT}
        # in /home/{repo}, COPYs the scripts, runs prepare.sh (installs the PR's
        # required deps on top of the reused node_modules + builds), then the
        # canonical Image._HARDENING_BLOCK strips origin/all refs/future history
        # (HEAD==BASE_COMMIT asserts + submodule pass). dependency() is an Image,
        # so DockerfileEnhancer returns this Dockerfile verbatim -- the checkout +
        # hardening stay as written (pinning here is correct: per-PR, not the base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


class P5JS_8679_to_3139(Instance):
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
                r"^(\s*)(?:([✓✔✅]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
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

                # Skip garbage captured as test names (console output, not real tests)
                if (
                    full_path.startswith("[ '")
                    or full_path.startswith("🌸")
                    or full_path.startswith('Running "')
                ):
                    continue

                if status in ("✓", "✔", "✅"):
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
