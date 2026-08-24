import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections that cannot affect a test outcome but do break `git apply`.

    R19: `git apply` is atomic -- one unusable section rejects the whole patch, the
    stage produces no output at all, and the run grades as "test stage crashed before
    collection". This repo's fix patch carries a binary hunk for `docs/demo.gif` with
    no full index line, which fails with:

        error: cannot apply binary patch to 'docs/demo.gif' without full index line

    The header is parsed non-greedily (`(.+?) b/(.+)$`) because a `\\S+` pattern does
    not match paths containing spaces, which would let a binary payload leak into the
    previous section and survive the filter.

    Cost: `docs/demo.gif` is a README asset -- no test reads it, so dropping it loses
    nothing. The helper is retained even where the current patches need no filtering,
    so behaviour stays identical the day a patch arrives that does.
    """
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")

    kept: list[str] = []
    section: list[str] = []
    drop = False

    for line in patch.splitlines(keepends=True):
        if header.match(line):
            if section and not drop:
                kept.extend(section)
            section = [line]
            drop = False
            continue

        section.append(line)
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            drop = True

    if section and not drop:
        kept.extend(section)

    return "".join(kept)


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
        return "node:20"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>` rather than a shared `base`: the Dockerfile QC
        # contract requires the PR layer to inherit
        # `mswebench/<org>_m_<repo>:base-pr-<N>`, and a shared tag hides a real
        # hazard -- DockerfileEnhancer bakes one BASE_COMMIT into this image, so a
        # reused `base` stays pinned to whichever PR built it first and any later
        # PR whose base commit is unreachable from that sha dies in prepare.sh.
        # Costs one base image per PR instead of one per repo; deliberate.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        # Deliberately minimal. Because dependency() returns a *string*,
        # DockerfileEnhancer.enhance() owns this file: it injects the syntax
        # directive, TARGETARCH/REPO_URL/BASE_COMMIT ARGs, the proxy ARG+ENV
        # blocks, the OCI labels, the CA-cert symlink farm, and rewrites the
        # clone below into clone + checkout ${BASE_COMMIT} + history scrub.
        # Anything added here that the enhancer also emits would be duplicated,
        # so the only repo-specific line is the apt install: xvfb plus the X11 /
        # Chromium shared libraries @vscode/test-electron needs to boot a real
        # VS Code instance headlessly. `git` and `ca-certificates` already ship
        # in node:20.
        #
        # `xauth` is listed explicitly and is NOT optional: `xvfb-run` shells out
        # to it to build the X authority file, and under --no-install-recommends
        # it is not pulled in with xvfb. Without it every stage dies instantly
        # with "xvfb-run: error: xauth command not found" and collects 0 tests.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    xvfb xauth libnss3 libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

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
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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
npm ci || true
npm run compile
npm run webpack
# Warm the @vscode/test-electron cache at build time so each of the three stages
# does not re-download ~317 MB of VS Code over the network at run time.
xvfb-run -a node ./out/test/runTest.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# `npm test` is NOT used here. Its `pretest` hook runs `tsc -p ./`, which
# type-checks the WHOLE project, so one unresolvable import in the gold test
# aborts the run before mocha starts and the stage collects 0 tests. That is
# exactly what happens at the test stage: the gold test imports
# getJsoncFromYaml/getYamlFromJsonc, which only exist after fix.patch.
# tsc still EMITS the .js for every other file (verified: 11 outputs, errors
# confined to helpers.test.ts), so tolerating its exit code lets the suite run
# and the gold tests fail honestly -- a real FAIL->PASS signal instead of
# NONE->PASS. Identical in all three scripts so the stages stay comparable.
npx tsc -p ./ || true
npx webpack --mode production || true
xvfb-run -a node ./out/test/runTest.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# The patches may add dependencies (this PR adds jsonc-parser) that the build-time
# `npm ci` could not know about, so reinstall against the patched lockfile.
npm ci || true
# `npm test` is NOT used here. Its `pretest` hook runs `tsc -p ./`, which
# type-checks the WHOLE project, so one unresolvable import in the gold test
# aborts the run before mocha starts and the stage collects 0 tests. That is
# exactly what happens at the test stage: the gold test imports
# getJsoncFromYaml/getYamlFromJsonc, which only exist after fix.patch.
# tsc still EMITS the .js for every other file (verified: 11 outputs, errors
# confined to helpers.test.ts), so tolerating its exit code lets the suite run
# and the gold tests fail honestly -- a real FAIL->PASS signal instead of
# NONE->PASS. Identical in all three scripts so the stages stay comparable.
npx tsc -p ./ || true
npx webpack --mode production || true
xvfb-run -a node ./out/test/runTest.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The patches may add dependencies (this PR adds jsonc-parser) that the build-time
# `npm ci` could not know about, so reinstall against the patched lockfile.
npm ci || true
# `npm test` is NOT used here. Its `pretest` hook runs `tsc -p ./`, which
# type-checks the WHOLE project, so one unresolvable import in the gold test
# aborts the run before mocha starts and the stage collects 0 tests. That is
# exactly what happens at the test stage: the gold test imports
# getJsoncFromYaml/getYamlFromJsonc, which only exist after fix.patch.
# tsc still EMITS the .js for every other file (verified: 11 outputs, errors
# confined to helpers.test.ts), so tolerating its exit code lets the suite run
# and the gold tests fail honestly -- a real FAIL->PASS signal instead of
# NONE->PASS. Identical in all three scripts so the stages stay comparable.
npx tsc -p ./ || true
npx webpack --mode production || true
xvfb-run -a node ./out/test/runTest.js

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("hilleer", "vscode-yaml-plus-json")
class VscodeYamlPlusJson(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        # Mocha spec reporter: indentation encodes suite hierarchy (2-space per level).
        # Tests are suite-qualified as "Suite > SubSuite > test name" to disambiguate
        # duplicate leaf names across different suites (e.g. "should return expected yaml"
        # appears under six different suites in this repo).
        # `\s+` not `\s*`: @vscode/test-electron prints its own progress lines with the
        # same tick glyph at column 0 ("✔ Validated version: 1.134.0", "✔ Downloaded
        # VS Code into ..."). Mocha's real test ticks are always indented under a suite,
        # so requiring indentation drops the downloader's chatter without touching tests.
        re_pass = re.compile(r"^(\s+)[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_inline_fail = re.compile(r"^(\s+)\d+\)\s+(.+)$")
        re_suite = re.compile(r"^(\s+)(\S.*)$")
        re_summary_pass = re.compile(r"^\s*\d+\s+passing")
        re_summary_fail = re.compile(r"^\s*\d+\s+failing")
        re_summary_pending = re.compile(r"^\s*\d+\s+pending")
        re_fail_entry = re.compile(r"^\s+\d+\)\s+(.+)$")

        # This suite deliberately exercises error paths, so Node/Mocha print real stack
        # traces and error headers to stdout *while the test still passes*. Those lines
        # are indented, so a bare `^(\s+)(\S.*)$` suite regex captures them as fake
        # `describe()` context and prepends them to every following test name -- which
        # both inflates the count and makes names unstable across stages (breaking the
        # f2p comparison). Reject anything that looks like trace/diagnostic output
        # before it can reach the suite stack.
        re_stack_frame = re.compile(r"^\s*at\s")
        re_source_ref = re.compile(r"\(?[^\s()]+:\d+:\d+\)?\s*$")
        re_error_header = re.compile(r"^\s*\w*(?:Error|Exception)\b")
        re_diff_marker = re.compile(r"^\s*[+-]\s")
        re_caret = re.compile(r"^\s*\^+\s*$")

        def _is_noise(text: str) -> bool:
            return bool(
                re_stack_frame.match(text)
                or re_source_ref.search(text)
                or re_error_header.match(text)
                or re_diff_marker.match(text)
                or re_caret.match(text)
            )

        suite_stack: list[tuple[int, str]] = []
        in_failure_section = False

        def _qualified_name(stack: list[tuple[int, str]], test: str) -> str:
            parts = [s for _, s in stack]
            parts.append(test)
            return " > ".join(parts)

        lines = clean_log.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            if re_summary_fail.match(stripped):
                in_failure_section = True
                i += 1
                continue

            if re_summary_pass.match(stripped) or re_summary_pending.match(stripped):
                i += 1
                continue

            if not in_failure_section:
                pass_match = re_pass.match(line)
                if pass_match:
                    indent = len(pass_match.group(1))
                    test_name = pass_match.group(2).strip()
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    passed_tests.add(_qualified_name(suite_stack, test_name))
                    i += 1
                    continue

                inline_fail = re_inline_fail.match(line)
                if inline_fail:
                    indent = len(inline_fail.group(1))
                    test_name = inline_fail.group(2).strip()
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    failed_tests.add(_qualified_name(suite_stack, test_name))
                    i += 1
                    continue

                suite_match = re_suite.match(line)
                if suite_match:
                    indent = len(suite_match.group(1))
                    name = suite_match.group(2).strip()
                    # Mocha indents suites on a strict 2-space grid. Test bodies here
                    # echo raw fixture text ("{ bad json }", "--- {unclosed") and caret
                    # pointers at arbitrary columns; anything off-grid, tab-indented, or
                    # noise-shaped is output, not structure.
                    if (
                        _is_noise(name)
                        or indent % 2 != 0
                        or "\t" in suite_match.group(1)
                        or name.startswith("(")
                        or name.startswith(">")
                    ):
                        i += 1
                        continue
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    suite_stack.append((indent, name))
                    i += 1
                    continue

            else:
                # Failure summary: "  1) Suite\n       SubSuite\n         test name:"
                fail_entry = re_fail_entry.match(line)
                if fail_entry:
                    parts = [fail_entry.group(1).strip()]
                    i += 1
                    while i < len(lines):
                        cont_line = lines[i]
                        cont_stripped = cont_line.strip()
                        if not cont_stripped:
                            break
                        if cont_stripped.endswith(":"):
                            parts.append(cont_stripped.rstrip(":").strip())
                            i += 1
                            break
                        if re_fail_entry.match(cont_line):
                            break
                        if _is_noise(cont_stripped):
                            break
                        parts.append(cont_stripped)
                        i += 1
                    failed_tests.add(" > ".join(parts))
                    continue

            i += 1

        # R2: the sets MUST be disjoint or TestResult raises. Failure wins.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
