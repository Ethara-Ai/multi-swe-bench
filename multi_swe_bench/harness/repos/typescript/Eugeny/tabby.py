from __future__ import annotations

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
        return "node:18-bookworm"

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

        # SHARED base (tag `base`, reused by every tabby PR). The `# syntax`
        # directive makes DockerfileEnhancer.enhance() return this verbatim, so the
        # enhancer cannot rewrite the clone into `checkout ${BASE_COMMIT}` +
        # history-strip — that would pin the shared base to a single PR's commit and
        # prune the objects every other PR needs. Per-PR hardening lives in
        # ImageDefault (Image._HARDENING_BLOCK with the literal base.sha) instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git jq python3 build-essential libfontconfig1-dev ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g --force yarn@1.22.22

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

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

# Root install without scripts so we control the heavy postinstall.
# Retry once on transient yarn cache corruption (intermittent under emulation).
# --ignore-engines is REQUIRED: the modern lockfile pulls minimatch@10, whose
# "engines" demands node >=20 while this base image is node:18. Yarn Classic
# treats an engine mismatch as FATAL ("Found incompatible module"), so without
# the flag the install aborts and the image ships with no node_modules --
# eslint/shelljs missing, so lint and build:typings fail in every stage and the
# instance yields no signal at all.
yarn install --ignore-engines --ignore-scripts --network-timeout 600000 \
    || {{ yarn cache clean; yarn install --ignore-engines --ignore-scripts --network-timeout 600000; }} \
    || true

# Record the nearest tag BEFORE the image-level _HARDENING_BLOCK strips every
# ref. tabby derives its version with `git describe`; with zero tags that exits
# 128 ("No names found, cannot describe anything") and both build:typings and
# scripts/install-deps.mjs die in EVERY grading stage. The tag is recreated at
# HEAD after hardening, which leaks nothing (HEAD is already reachable).
git describe --tags --abbrev=0 > /home/.base_tag 2>/dev/null || echo v1.0.0 > /home/.base_tag

# patch-package needs node_modules; tolerate missing patches/ directory
yarn patch-package || true

# Per-plugin yarn installs (handle both era variants) — required for eslint/tsc
# to resolve plugin-local deps. install-deps.mjs (>= v1.0.196) or install-deps.js (older).
if [ -f scripts/install-deps.mjs ]; then
    node scripts/install-deps.mjs || true
elif [ -f scripts/install-deps.js ]; then
    node scripts/install-deps.js || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}

echo '##### TABBY_STEP_START build_typings #####'
yarn build:typings
build_typings_rc=$?
if [ $build_typings_rc -eq 0 ]; then
    echo '##### TABBY_PASS build_typings #####'
else
    echo '##### TABBY_FAIL build_typings #####'
fi

echo '##### TABBY_STEP_START lint #####'
yarn lint
lint_rc=$?
if [ $lint_rc -eq 0 ]; then
    echo '##### TABBY_PASS lint #####'
else
    echo '##### TABBY_FAIL lint #####'
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --whitespace=nowarn \
    --exclude='extras/clink/*' \
    --exclude='*.dll' --exclude='*.exe' --exclude='*.ico' \
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' \
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.otf' \
    /home/test.patch
apply_rc=$?
if [ $apply_rc -ne 0 ]; then
    echo '##### TABBY_FAIL apply_patch #####'
    exit 0
fi
echo '##### TABBY_PASS apply_patch #####'

echo '##### TABBY_STEP_START build_typings #####'
yarn build:typings
build_typings_rc=$?
if [ $build_typings_rc -eq 0 ]; then
    echo '##### TABBY_PASS build_typings #####'
else
    echo '##### TABBY_FAIL build_typings #####'
fi

echo '##### TABBY_STEP_START lint #####'
yarn lint
lint_rc=$?
if [ $lint_rc -eq 0 ]; then
    echo '##### TABBY_PASS lint #####'
else
    echo '##### TABBY_FAIL lint #####'
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --whitespace=nowarn \
    --exclude='extras/clink/*' \
    --exclude='*.dll' --exclude='*.exe' --exclude='*.ico' \
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' \
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.otf' \
    /home/test.patch /home/fix.patch
apply_rc=$?
if [ $apply_rc -ne 0 ]; then
    echo '##### TABBY_FAIL apply_patch #####'
    exit 0
fi
echo '##### TABBY_PASS apply_patch #####'

echo '##### TABBY_STEP_START build_typings #####'
yarn build:typings
build_typings_rc=$?
if [ $build_typings_rc -eq 0 ]; then
    echo '##### TABBY_PASS build_typings #####'
else
    echo '##### TABBY_FAIL build_typings #####'
fi

echo '##### TABBY_STEP_START lint #####'
yarn lint
lint_rc=$?
if [ $lint_rc -eq 0 ]; then
    echo '##### TABBY_PASS lint #####'
else
    echo '##### TABBY_FAIL lint #####'
fi
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

        # Per-PR anti-cheat hardening. This image depends on an Image (the shared
        # base), so DockerfileEnhancer emits its Dockerfile verbatim — it only
        # auto-injects hardening into str-dependency images. Bake the canonical
        # block from image.py with the LITERAL base.sha (BASE_COMMIT is not passed
        # as a build arg for FROM-an-image builds), so the fix commit cannot be read
        # back out of git history via git log/show/fetch inside the container.
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

# Restore a single tag at HEAD so `git describe` resolves (see prepare.sh).
# Placed AFTER the hardening block so it survives; it points at the base commit,
# which is already reachable, so no pruned history becomes visible again.
RUN cd /home/{self.pr.repo} && git tag -f "$(cat /home/.base_tag)" HEAD

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("Eugeny", "tabby")
class Tabby(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        re_pass = re.compile(r"^#####\s+TABBY_PASS\s+(\S+)\s+#####$")
        re_fail = re.compile(r"^#####\s+TABBY_FAIL\s+(\S+)\s+#####$")

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).strip()
            if not line:
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))
                continue

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
