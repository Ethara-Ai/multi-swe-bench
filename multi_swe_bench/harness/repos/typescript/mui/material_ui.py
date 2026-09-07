import re
from dataclasses import asdict, dataclass
from json import JSONDecoder
from typing import Generator, Optional, Union

from dataclasses_json import dataclass_json

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MaterialUiImageBase(Image):
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
        return "base_fullhist"

    def workdir(self) -> str:
        return "base_fullhist"

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

        # The leading syntax directive makes DockerfileEnhancer.enhance() return
        # this text untouched, so the standard repo-fetch rewrite - which pins the
        # clone to ONE ${{BASE_COMMIT}} and gc-prunes everything unreachable from
        # it - does not run here. That rewrite made this shared base usable by
        # exactly one PR: any PR whose base.sha was not an ancestor of the pinned
        # commit died at `git checkout <sha>` with "reference is not a tree", the
        # image never built, and no report was produced.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git jq && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@9

{code}

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class MaterialUiImageBase40180(Image):
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
        return "base40180_fullhist"

    def workdir(self) -> str:
        return "base40180_fullhist"

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

        # The leading syntax directive makes DockerfileEnhancer.enhance() return
        # this text untouched, so the standard repo-fetch rewrite - which pins the
        # clone to ONE ${{BASE_COMMIT}} and gc-prunes everything unreachable from
        # it - does not run here. That rewrite made this shared base usable by
        # exactly one PR: any PR whose base.sha was not an ancestor of the pinned
        # commit died at `git checkout <sha>` with "reference is not a tree", the
        # image never built, and no report was produced.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git jq && rm -rf /var/lib/apt/lists/*

{code}

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class MaterialUiImageDefault(Image):
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
        return MaterialUiImageBase(self.pr, self._config)

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

sed -i 's/packageManager": ".*"/packageManager": "pnpm@^9"/' package.json
jq '.packageManager = "pnpm@^9" | del(.engines)' package.json > temp.json && mv temp.json package.json
pnpm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm test:unit -- --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
pnpm test:unit -- --reporter json
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
pnpm test:unit -- --reporter json
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

        # The shared base keeps full history so every PR can reach its own
        # base.sha; the strict single-commit strip therefore happens here, with
        # this PR's sha carried by the BASE_COMMIT ARG, so the finished image
        # still holds exactly one commit and no remotes.
        hardening = Image._HARDENING_BLOCK

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
{prepare_commands}

{hardening}
{self.clear_env}
"""


class MaterialUiImageDefault40180(Image):
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
        return MaterialUiImageBase40180(self.pr, self._config)

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

# --ignore-engines: this era's lockfile pins packages that cap Node at 16
# (eslint-import-resolver-webpack@0.13.1), and yarn 1 aborts the ENTIRE install
# on an engine mismatch. That left no node_modules at all, so every later stage
# died with "cross-env: not found" and the report came back 0/0/0.
yarn install --ignore-engines || true

# Fail the build here instead of shipping an image whose test command cannot
# run: without this, a broken install only shows up three stages later as an
# unexplained (0, 0, 0).
test -x node_modules/.bin/mocha
test -x node_modules/.bin/cross-env

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
cd /home/__REPO__
set +e

LOGDIR=/tmp/mswb-run
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Report goes to a file, not straight to the container's stdout pipe: mocha
# exits via process.exit(), and Node truncates asynchronous pipe writes, which
# silently costs the tail of a multi-megabyte JSON report.
yarn run test:unit --reporter json > "$LOGDIR/report.json" 2> "$LOGDIR/stderr.log"

cat "$LOGDIR/report.json"
cat "$LOGDIR/stderr.log"

exit 0

""".replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
cd /home/__REPO__

set -e
git apply /home/test.patch
set +e

LOGDIR=/tmp/mswb-test-stage
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Every mocha run writes its report to a FILE and the files are concatenated at
# the very end, once every writer has exited. Mocha runs with --exit, which
# calls process.exit() the moment the run finishes; Node's writes to a PIPE are
# asynchronous, so a multi-megabyte JSON report loses its tail and parse_log
# discards the whole unparseable payload (that is how a stage with 4224 passing
# tests still reported 0/0/0). Writes to a file are synchronous, so the report
# survives intact.
run_mocha() {
  local out="$LOGDIR/$1"
  shift
  NODE_ENV=test npx mocha "$@" --reporter json --exit > "$out.json" 2> "$out.err"
}

# Same glob set as the package.json test:unit script, so the baseline test set
# matches run.sh / fix-run.sh exactly. Globs stay quoted: mocha expands them.
run_mocha main 'packages/**/*.test.{js,ts,tsx}' 'docs/**/*.test.{js,ts,tsx}' 'scripts/**/*.test.{js,ts,tsx}' 'test/utils/**/*.test.{js,ts,tsx}' --exclude '**/node_modules/**'

# Mocha loads every file matched by the glob before running any test. A test
# patch may reference a source module that only lands with the FIX patch, so
# that load throws MODULE_NOT_FOUND and the whole suite aborts before the
# reporter emits anything. If no report was produced, re-run with the test
# files touched by the patch excluded, so the rest of the suite is still
# measured, then run each touched file on its own.
if ! grep -q '"stats"' "$LOGDIR/main.json"; then
  echo "test-run.sh: suite produced no JSON report; retrying without the patched test files"

  # git apply leaves added files untracked and modified ones tracked, so read
  # the porcelain status and keep the path column whatever the status code is.
  PATCHED_TESTS=$(git status --porcelain -uall | cut -c4- | grep -E '\.test\.(js|ts|tsx)$')

  EXCLUDE_ARGS=()
  while IFS= read -r f; do
    [ -n "$f" ] && EXCLUDE_ARGS+=(--exclude "$f")
  done <<< "$PATCHED_TESTS"

  run_mocha retry 'packages/**/*.test.{js,ts,tsx}' 'docs/**/*.test.{js,ts,tsx}' 'scripts/**/*.test.{js,ts,tsx}' 'test/utils/**/*.test.{js,ts,tsx}' --exclude '**/node_modules/**' "${EXCLUDE_ARGS[@]}"

  i=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    i=$((i + 1))
    run_mocha "patched$i" "$f"
  done <<< "$PATCHED_TESTS"
fi

# parse_log scans for multiple JSON objects, so several reports in one log are
# all counted.
cat "$LOGDIR"/*.json
cat "$LOGDIR"/*.err

exit 0

""".replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
cd /home/__REPO__
set +e

set -e
git apply /home/test.patch /home/fix.patch
set +e

LOGDIR=/tmp/mswb-fix
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Report goes to a file, not straight to the container's stdout pipe: mocha
# exits via process.exit(), and Node truncates asynchronous pipe writes, which
# silently costs the tail of a multi-megabyte JSON report.
yarn run test:unit --reporter json > "$LOGDIR/report.json" 2> "$LOGDIR/stderr.log"

cat "$LOGDIR/report.json"
cat "$LOGDIR/stderr.log"

exit 0

""".replace("__REPO__", self.pr.repo),
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

        # The shared base keeps full history so every PR can reach its own
        # base.sha; the strict single-commit strip therefore happens here, with
        # this PR's sha carried by the BASE_COMMIT ARG, so the finished image
        # still holds exactly one commit and no remotes.
        hardening = Image._HARDENING_BLOCK

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
{prepare_commands}

{hardening}
{self.clear_env}
"""


class MaterialUiImageDefault33415(Image):
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
        return MaterialUiImageBase40180(self.pr, self._config)

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

# --ignore-engines: this era's lockfile pins packages that cap Node at 16
# (eslint-import-resolver-webpack@0.13.1), and yarn 1 aborts the ENTIRE install
# on an engine mismatch. That left no node_modules at all, so every later stage
# died with "cross-env: not found" and the report came back 0/0/0.
yarn install --ignore-engines || true

# Fail the build here instead of shipping an image whose test command cannot
# run: without this, a broken install only shows up three stages later as an
# unexplained (0, 0, 0).
test -x node_modules/.bin/mocha
test -x node_modules/.bin/cross-env

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
cd /home/__REPO__
set +e

LOGDIR=/tmp/mswb-run
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Report goes to a file, not straight to the container's stdout pipe: mocha
# exits via process.exit(), and Node truncates asynchronous pipe writes, which
# silently costs the tail of a multi-megabyte JSON report.
yarn run test:unit --reporter json --exit > "$LOGDIR/report.json" 2> "$LOGDIR/stderr.log"

cat "$LOGDIR/report.json"
cat "$LOGDIR/stderr.log"

exit 0

""".replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
cd /home/__REPO__

set -e
git apply /home/test.patch
set +e

LOGDIR=/tmp/mswb-test-stage
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Every mocha run writes its report to a FILE and the files are concatenated at
# the very end, once every writer has exited. Mocha runs with --exit, which
# calls process.exit() the moment the run finishes; Node's writes to a PIPE are
# asynchronous, so a multi-megabyte JSON report loses its tail and parse_log
# discards the whole unparseable payload (that is how a stage with 4224 passing
# tests still reported 0/0/0). Writes to a file are synchronous, so the report
# survives intact.
run_mocha() {
  local out="$LOGDIR/$1"
  shift
  NODE_ENV=test npx mocha "$@" --reporter json --exit > "$out.json" 2> "$out.err"
}

# Same glob set as the package.json test:unit script, so the baseline test set
# matches run.sh / fix-run.sh exactly. Globs stay quoted: mocha expands them.
run_mocha main 'packages/**/*.test.{js,ts,tsx}' 'docs/**/*.test.{js,ts,tsx}' 'scripts/**/*.test.{js,ts,tsx}' 'test/utils/**/*.test.{js,ts,tsx}' --exclude '**/node_modules/**'

# Mocha loads every file matched by the glob before running any test. A test
# patch may reference a source module that only lands with the FIX patch, so
# that load throws MODULE_NOT_FOUND and the whole suite aborts before the
# reporter emits anything. If no report was produced, re-run with the test
# files touched by the patch excluded, so the rest of the suite is still
# measured, then run each touched file on its own.
if ! grep -q '"stats"' "$LOGDIR/main.json"; then
  echo "test-run.sh: suite produced no JSON report; retrying without the patched test files"

  # git apply leaves added files untracked and modified ones tracked, so read
  # the porcelain status and keep the path column whatever the status code is.
  PATCHED_TESTS=$(git status --porcelain -uall | cut -c4- | grep -E '\.test\.(js|ts|tsx)$')

  EXCLUDE_ARGS=()
  while IFS= read -r f; do
    [ -n "$f" ] && EXCLUDE_ARGS+=(--exclude "$f")
  done <<< "$PATCHED_TESTS"

  run_mocha retry 'packages/**/*.test.{js,ts,tsx}' 'docs/**/*.test.{js,ts,tsx}' 'scripts/**/*.test.{js,ts,tsx}' 'test/utils/**/*.test.{js,ts,tsx}' --exclude '**/node_modules/**' "${EXCLUDE_ARGS[@]}"

  i=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    i=$((i + 1))
    run_mocha "patched$i" "$f"
  done <<< "$PATCHED_TESTS"
fi

# parse_log scans for multiple JSON objects, so several reports in one log are
# all counted.
cat "$LOGDIR"/*.json
cat "$LOGDIR"/*.err

exit 0

""".replace("__REPO__", self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
cd /home/__REPO__
set +e

set -e
git apply /home/test.patch /home/fix.patch
set +e

LOGDIR=/tmp/mswb-fix
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"

# Report goes to a file, not straight to the container's stdout pipe: mocha
# exits via process.exit(), and Node truncates asynchronous pipe writes, which
# silently costs the tail of a multi-megabyte JSON report.
yarn run test:unit --reporter json --exit > "$LOGDIR/report.json" 2> "$LOGDIR/stderr.log"

cat "$LOGDIR/report.json"
cat "$LOGDIR/stderr.log"

exit 0

""".replace("__REPO__", self.pr.repo),
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

        # The shared base keeps full history so every PR can reach its own
        # base.sha; the strict single-commit strip therefore happens here, with
        # this PR's sha carried by the BASE_COMMIT ARG, so the finished image
        # still holds exactly one commit and no remotes.
        hardening = Image._HARDENING_BLOCK

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
{prepare_commands}

{hardening}
{self.clear_env}
"""


@Instance.register("mui", "material-ui")
class MaterialUi(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 40180 and self.pr.number > 33415:
            return MaterialUiImageDefault40180(self.pr, self._config)
        elif self.pr.number <= 33415:
            return MaterialUiImageDefault33415(self.pr, self._config)

        return MaterialUiImageDefault(self.pr, self._config)

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

        @dataclass_json
        @dataclass
        class MaterialUiStats:
            suites: int
            tests: int
            passes: int
            pending: int
            failures: int
            start: str
            end: str
            duration: int

            @classmethod
            def from_dict(cls, d: dict) -> "TestResult":
                return cls(**d)

            @classmethod
            def from_json(cls, json_str: str) -> "TestResult":
                return cls.from_dict(cls.schema().loads(json_str))

            def dict(self) -> dict:
                return asdict(self)

            def json(self) -> str:
                return self.to_json(ensure_ascii=False)

        @dataclass_json
        @dataclass
        class MaterialUiTest:
            title: str
            fullTitle: str
            currentRetry: int
            err: dict
            file: Optional[str] = None
            duration: Optional[int] = None
            speed: Optional[str] = None

            @classmethod
            def from_dict(cls, d: dict) -> "TestResult":
                return cls(**d)

            @classmethod
            def from_json(cls, json_str: str) -> "TestResult":
                return cls.from_dict(cls.schema().loads(json_str))

            def dict(self) -> dict:
                return asdict(self)

            def json(self) -> str:
                return self.to_json(ensure_ascii=False)

        @dataclass_json
        @dataclass
        class MaterialUiInfo:
            stats: MaterialUiStats
            tests: list[MaterialUiTest]
            pending: list[MaterialUiTest]
            failures: list[MaterialUiTest]
            passes: list[MaterialUiTest]

            @classmethod
            def from_dict(cls, d: dict) -> "MaterialUiInfo":
                return cls(**d)

            @classmethod
            def from_json(cls, json_str: str) -> "MaterialUiInfo":
                return cls.from_dict(cls.schema().loads(json_str))

            def dict(self) -> dict:
                return asdict(self)

            def json(self) -> str:
                return self.to_json(ensure_ascii=False)

        def extract_json_objects(
            text: str, decoder=JSONDecoder()
        ) -> Generator[dict, None, None]:
            pos = 0
            while True:
                match = text.find("{", pos)
                if match == -1:
                    break
                try:
                    result, index = decoder.raw_decode(text[match:])
                    yield result
                    pos = match + index
                except ValueError:
                    pos = match + 1

        if "Building new" in test_log:
            test_log = test_log[test_log.find("Building new", 0) :]

        re_removes = [
            re.compile(r"error Command failed with exit code \d+\.", re.DOTALL),
        ]

        for re_remove in re_removes:
            test_log = re_remove.sub("", test_log)

        original_log = test_log
        test_log = test_log.replace("\r\n", "")
        test_log = test_log.replace("\n", "")

        for obj in extract_json_objects(test_log):
            try:
                info = MaterialUiInfo.from_dict(obj)
            except (KeyError, TypeError):
                continue
            for test in info.passes:
                test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle
                passed_tests.add(test_id)
            for test in info.failures:
                test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle
                failed_tests.add(test_id)
            for test in info.pending:
                test_id = f"{test.file}:{test.fullTitle}" if test.file else test.fullTitle
                skipped_tests.add(test_id)

        for test in failed_tests:
            if test in passed_tests:
                passed_tests.remove(test)
            if test in skipped_tests:
                skipped_tests.remove(test)

        for test in skipped_tests:
            if test in passed_tests:
                passed_tests.remove(test)

        if not passed_tests and not failed_tests and not skipped_tests:
            clean_log = re.sub(r'\x1b\[[0-9;]*m', '', original_log)

            vitest_match = re.search(
                r"Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                clean_log,
            )
            if not vitest_match:
                vitest_match = re.search(
                    r"Tests\s+(\d+)\s+passed(?:\s*\|\s*(\d+)\s+skipped)?",
                    clean_log,
                )
                if vitest_match:
                    vp = int(vitest_match.group(1) or 0)
                    vs = int(vitest_match.group(2) or 0)
                    vf = 0
                    for i in range(vp):
                        passed_tests.add(f"vitest_pass_{i}")
                    for i in range(vs):
                        skipped_tests.add(f"vitest_skip_{i}")
            else:
                vf = int(vitest_match.group(1) or 0)
                vp = int(vitest_match.group(2) or 0)
                vs = int(vitest_match.group(3) or 0)
                if vp > 0 or vf > 0:
                    for i in range(vp):
                        passed_tests.add(f"vitest_pass_{i}")
                    for i in range(vf):
                        failed_tests.add(f"vitest_fail_{i}")
                    for i in range(vs):
                        skipped_tests.add(f"vitest_skip_{i}")

            if not passed_tests and not failed_tests:
                dot_pass = re.search(r"(\d+)\s+passing", clean_log)
                dot_fail = re.search(r"(\d+)\s+failing", clean_log)
                dot_pend = re.search(r"(\d+)\s+pending", clean_log)
                dp = int(dot_pass.group(1)) if dot_pass else 0
                df = int(dot_fail.group(1)) if dot_fail else 0
                ds = int(dot_pend.group(1)) if dot_pend else 0
                if dp > 0 or df > 0:
                    for i in range(dp):
                        passed_tests.add(f"dot_pass_{i}")
                    for i in range(df):
                        failed_tests.add(f"dot_fail_{i}")
                    for i in range(ds):
                        skipped_tests.add(f"dot_skip_{i}")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
