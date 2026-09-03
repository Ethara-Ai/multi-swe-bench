from __future__ import annotations

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

EXAMPLE_DIR = "examples/rectOfInterest"

NODE_MODULES_STORE_DIR = "/home/example_deps"
NODE_MODULES_STORE = f"{NODE_MODULES_STORE_DIR}/node_modules"


def _binary_patch_paths(patch: str) -> list[str]:
    """Paths `git apply` cannot handle, taken from the patch text.

    A diff records a binary file either as a `GIT binary patch` block (which
    carries the payload and applies fine) or as a bare
    `Binary files a/x and b/y differ` marker (which does not).  Only the
    second kind is returned; those are the paths that must be excluded from
    every apply, and because a patch applies atomically, missing even one of
    them fails the whole thing.

    Deriving the list here rather than hardcoding it keeps the config correct
    across dataset regeneration.  Every path is returned verbatim, including
    any containing spaces -- _exclude_flags() shell-quotes them.  Dropping such
    a path instead would be the worst option available: the unexcluded binary
    hunk fails the atomic apply, and the whole graded stage dies on an error
    that names the patch rather than the filter that caused it.
    """
    prefix = "Binary files "
    suffix = " differ"
    paths: set[str] = set()

    for line in patch.splitlines():
        line = line.rstrip("\r")
        if not (line.startswith(prefix) and line.endswith(suffix)):
            continue
        body = line[len(prefix) : -len(suffix)]
        left, sep, right = body.partition(" and ")
        if not sep:
            continue
        target = right if right != "/dev/null" else left
        if target.startswith(("a/", "b/")):
            target = target[2:]
        if target and target != "/dev/null":
            paths.add(target)

    return sorted(paths)


def _exclude_flags(patch: str) -> str:
    """`git apply` --exclude flags for every unappliable binary path.

    shlex.quote() is applied per path because these flags are interpolated
    into a shell command line.  It is a no-op for the twelve paths in this
    dataset and correct for anything a regenerated one might carry.
    """
    return " ".join(
        f"--exclude={shlex.quote(path)}" for path in _binary_patch_paths(patch)
    )


class ImageBase(Image):
    """Toolchain + cloned source. Built before the PR image."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:12-buster"

    def image_prefix(self) -> str:
        return "envagent"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class ImageDefault(Image):
    """Per-PR layer: patches, graded scripts, installed dependencies."""

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

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "pr"

    def workdir(self) -> str:
        return "pr"

    def files(self) -> list[File]:
        example_path = f"/home/{self.pr.repo}/{EXAMPLE_DIR}"
        fix_excludes = _exclude_flags(self.pr.fix_patch)
        test_excludes = _exclude_flags(self.pr.test_patch)

        test_cmd = """export CI=true

mkdir -p {example_path}
ln -sfn {store} {example_path}/node_modules

JEST_LOG="$(mktemp)"

cd {example_path}
npx --no-install jest --config /home/jest.config.js --verbose --runInBand > "$JEST_LOG" 2>&1 && JEST_RC=0 || JEST_RC=$?
cat "$JEST_LOG"
if ! grep -qE '^(PASS|FAIL) |^Test Suites:|^Tests: |No tests found' "$JEST_LOG"; then
    echo "harness: jest produced no reporter output (exit $JEST_RC)" >&2
    exit 1
fi

exit 0
""".format(example_path=example_path, store=NODE_MODULES_STORE)

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
                "jest.config.js",
                """module.exports = {{
  preset: 'react-native',
  rootDir: '{example_path}',
  testEnvironment: 'node',
}};
""".format(example_path=example_path),
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

git apply --whitespace=nowarn {fix_excludes} /home/fix.patch

cd {example_path}
npm ci --ignore-scripts --no-audit --no-fund \\
  || npm install --ignore-scripts --no-audit --no-fund \\
  || true

if [ ! -x "{example_path}/node_modules/.bin/jest" ]; then
    echo "prepare: npm install did not produce node_modules/.bin/jest in {example_path}" >&2
    echo "prepare: the graded runs cannot work without it; failing the build here" >&2
    exit 1
fi

mkdir -p {store_dir}
rm -rf {store}
mv {example_path}/node_modules {store}

ln -sfn {store} {example_path}/node_modules
if ! (cd {example_path} && npx --no-install jest --version > /dev/null 2>&1); then
    echo "prepare: jest was installed but is not runnable through the staged" >&2
    echo "prepare: node_modules symlink -- module resolution is broken." >&2
    (cd {example_path} && npx --no-install jest --version) >&2 || true
    exit 1
fi
rm -f {example_path}/node_modules

cd /home/{pr.repo}
git reset --hard
git clean -fd

bash /home/check_git_changes.sh
""".format(
                    pr=self.pr,
                    fix_excludes=fix_excludes,
                    example_path=example_path,
                    store=NODE_MODULES_STORE,
                    store_dir=NODE_MODULES_STORE_DIR,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

"""
                + test_cmd,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

if ! git -C /home/{pr.repo} apply --whitespace=nowarn {test_excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

""".format(pr=self.pr, test_excludes=test_excludes)
                + test_cmd,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

if ! git -C /home/{pr.repo} apply --whitespace=nowarn {test_excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
if ! git -C /home/{pr.repo} apply --whitespace=nowarn {fix_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

""".format(
                    pr=self.pr,
                    test_excludes=test_excludes,
                    fix_excludes=fix_excludes,
                )
                + test_cmd,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("react-native-camera", "react-native-camera")
class ReactNativeCamera(Instance):
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
        """Parse `jest --verbose` output.

        jest prints an indentation tree per suite:

            PASS __tests__/App-test.js (17.4s)      <- suite header
              App                                   <- describe(), indent 2
                * renders correctly (3353ms)        <- test(), indent 4

        Tests are keyed as pytest-style node IDs rooted at the REPOSITORY, not
        at jest's rootDir:

            examples/rectOfInterest/__tests__/App-test.js::renders correctly

        The repo-relative head is deliberate.  Report._test_name_matches_files()
        compares the segment before the first "::" against the patch file
        lists verbatim, so a bare "__tests__/App-test.js" head would match
        neither test_patch_files nor fix_patch_files and the credited test
        would fall through to the ambiguous branch of the classifier.  With the
        prefix it matches test_patch_files exactly (which is what the gold test
        patch adds) and matches nothing in fix_patch_files (which is what keeps
        rule 5's cheating guard from tripping).

        jest's describe() occupies the same slot as pytest's class segment, so
        nested describes simply add further "::" segments.  Keying on the leaf
        test name alone would be wrong even here: "renders correctly" is the
        default name react-native init gives every generated app's spec, and a
        regenerated dataset covering two example apps would silently merge them.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        duration = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")
        status_marks = {
            "✓": passed_tests,
            "✔": passed_tests,
            "√": passed_tests,
            "✕": failed_tests,
            "✗": failed_tests,
            "×": failed_tests,
            "○": skipped_tests,
            "◯": skipped_tests,
        }

        suite_file: Optional[str] = None
        describe_stack: list[tuple[int, str]] = []
        in_failure_detail = False

        summary_prefixes = (
            "Test Suites:",
            "Tests:",
            "Snapshots:",
            "Time:",
            "Ran all test suites",
        )

        for raw_line in log.split("\n"):
            line = ansi.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith(("PASS ", "FAIL ")):
                suite_status = stripped[:4]
                path = duration.sub("", stripped[5:].strip())
                suite_file = f"{EXAMPLE_DIR}/{path}" if path else None
                if suite_file:
                    if suite_status == "PASS":
                        passed_tests.add(suite_file)
                    else:
                        failed_tests.add(suite_file)
                describe_stack.clear()
                in_failure_detail = False
                continue

            if stripped.startswith(summary_prefixes):
                suite_file = None
                describe_stack.clear()
                in_failure_detail = False
                continue

            if stripped.startswith("●"):
                in_failure_detail = True
                continue

            if suite_file is None:
                continue

            indent = len(line) - len(line.lstrip())
            mark = stripped[0]

            if mark in status_marks:
                name = duration.sub("", stripped[1:].strip())
                segments = [n for i, n in describe_stack if i < indent]
                segments.append(name)
                segments.insert(0, suite_file)
                status_marks[mark].add("::".join(segments))
                continue

            if in_failure_detail:
                continue

            while describe_stack and describe_stack[-1][0] >= indent:
                describe_stack.pop()
            describe_stack.append((indent, stripped))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
