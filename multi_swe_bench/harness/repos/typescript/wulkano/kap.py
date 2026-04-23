import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Merge commit SHAs for fetching binary fixtures that git apply cannot handle.
# These are needed because the JSONL test patches contain non-binary diffs
# (e.g. "Binary files /dev/null and b/file differ") without actual binary content.
_MERGE_COMMIT_SHAS = {
    878: "a3ca2a9c870422c5049318a7eb4b93421f2e15d9",
    899: "e11b3dfb92c9749f907e83e41cc7f87199417cd5",
    1021: "bc60c679b267fd28baae5d7c9484dc9f28553dca",
}


class KapImageBase(Image):
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
        return "node:14-bullseye"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
RUN apt-get update && apt-get install -y git make python3 ffmpeg
{code}

{self.clear_env}

"""


class KapImageDefault(Image):
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
        return KapImageBase(self.pr, self._config)

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

ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform --frozen-lockfile || ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "apply_patch.sh",
                """#!/bin/bash
set -e

PATCH_FILES="$@"
REPO_DIR="/home/{pr.repo}"
ORG="{pr.org}"
REPO="{pr.repo}"
MERGE_SHA="{merge_sha}"

cd "$REPO_DIR"

for PATCH in $PATCH_FILES; do
    NEW_BINARIES=""
    DEL_BINARIES=""
    EXCLUDE_ARGS=""

    while IFS= read -r bline; do
        if echo "$bline" | grep -q '^Binary files /dev/null and b/'; then
            FPATH=$(echo "$bline" | sed 's|Binary files /dev/null and b/\\(.*\\) differ|\\1|')
            NEW_BINARIES="$NEW_BINARIES $FPATH"
            EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$FPATH"
        elif echo "$bline" | grep -q 'and /dev/null differ$'; then
            FPATH=$(echo "$bline" | sed 's|Binary files a/\\(.*\\) and /dev/null differ|\\1|')
            DEL_BINARIES="$DEL_BINARIES $FPATH"
            EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$FPATH"
        fi
    done < <(grep '^Binary files' "$PATCH" || true)

    git apply --whitespace=nowarn $EXCLUDE_ARGS "$PATCH" || git apply --whitespace=nowarn --3way $EXCLUDE_ARGS "$PATCH" || true

    for BP in $NEW_BINARIES; do
        mkdir -p "$(dirname "$BP")"
        curl -sL "https://raw.githubusercontent.com/$ORG/$REPO/$MERGE_SHA/$BP" -o "$BP"
    done

    for BP in $DEL_BINARIES; do
        rm -f "$BP"
    done
done

""".format(pr=self.pr, merge_sha=_MERGE_COMMIT_SHAS.get(self.pr.number, "")),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform --frozen-lockfile || ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform || true

yarn test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch
ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform --frozen-lockfile || ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform || true

yarn test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform --frozen-lockfile || ELECTRON_SKIP_BINARY_DOWNLOAD=1 yarn install --ignore-platform || true

yarn test || true

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


@Instance.register("wulkano", "Kap")
class Kap(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KapImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # === AVA verbose pass: ✔ test name ===
            # Exclude expected-fail markers: ✔ [expected fail] ...
            m = re.match(r"[✔✓]\s+(?!\[expected fail\])(.+?)$", stripped)
            if m:
                name = m.group(1).strip()
                passed_tests.add(name)
                continue

            # === AVA verbose fail: ✘ test name / ✘ [fail]: test name ===
            m = re.match(r"[✘✗]\s+(?:\[fail\]:\s*)?(.+?)$", stripped)
            if m:
                name = m.group(1).strip()
                failed_tests.add(name)
                continue

            # === AVA verbose skip: - [skip] test name ===
            m = re.match(r"-\s+\[skip\]\s+(.+?)$", stripped)
            if m:
                name = m.group(1).strip()
                skipped_tests.add(name)
                continue

            # === AVA verbose todo: - [todo] test name ===
            # Count todos as skipped for completeness
            m = re.match(r"-\s+\[todo\]\s+(.+?)$", stripped)
            if m:
                name = m.group(1).strip()
                skipped_tests.add(name)
                continue

            # === xo linter errors (treat as failures) ===
            # xo outputs lines like: filename.js:line:col: error message (rule)
            # Only match if we see xo-style lint output
            m = re.match(
                r"(.+?\.[jt]sx?):(\d+):(\d+):\s+(error)\s+(.+)$", stripped
            )
            if m:
                filepath = m.group(1)
                rule_msg = m.group(5).strip()
                name = f"xo:{filepath}:{rule_msg}"
                failed_tests.add(name)
                continue

        # If a test appears in both pass and fail, it failed
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
