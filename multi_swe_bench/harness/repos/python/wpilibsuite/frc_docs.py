import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# apply_patch.sh — the dataset's binary diffs carry only an index hash and no
# payload. Every binary hunk in this dataset is a pure deletion (post-image sha
# is all zeros), so `rm -f` is enough; the object-DB fallback is kept for any
# future dataset whose binary hunks carry content.
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/frc-docs
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


# docs_test_report.py — wpilibsuite/frc-docs ships no unit tests. Its CI gate is
# the documentation build (`make html` -> sphinx-build -W, warnings-as-errors),
# so the docs build IS the test suite and each document Sphinx reads is one test
# case: it PASSES when the build attributes no warning to its source file.
_DOCS_TEST_REPORT_PY = '''"""Turn one Sphinx build into per-document test results.

usage: docs_test_report.py <sphinx-stdout> <sphinx-stderr> <source-root>

Emits one line per document, in the trailing-keyword form parse_log reads:

    sphinx:docs/software/basic-programming/index PASSED
    sphinx:docs/software/basic-programming/index FAILED

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one
document failed, 2 = the build produced no documents at all (the runner never
started -- NOT "zero tests passed").
"""
import re
import sys

ANSI = re.compile(r"\\x1b\\[[0-9;]*[a-zA-Z]")
READING = re.compile(r"^reading sources\\.\\.\\.\\s*\\[\\s*\\d+%\\]\\s+(\\S+)\\s*$", re.M)


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return ANSI.sub("", fh.read())


def main():
    stdout, stderr, src = read(sys.argv[1]), read(sys.argv[2]), sys.argv[3]
    if not src.endswith("/"):
        src += "/"

    docs, seen = [], set()
    for m in READING.finditer(stdout):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            docs.append(m.group(1))

    attributed = re.compile(
        r"^(" + re.escape(src) + r".+?\\.(?:rst|md))(?::[^:]*)?:\\s*(?:WARNING|ERROR):"
    )
    failed, unattributed = set(), []
    for line in stderr.splitlines():
        if "WARNING:" not in line and "ERROR:" not in line:
            continue
        m = attributed.match(line)
        if m:
            failed.add(m.group(1)[len(src):].rsplit(".", 1)[0])
        else:
            unattributed.append(line)

    if not docs:
        sys.stderr.write("docs_test_report: sphinx read no sources; the build never ran\\n")
        return 2

    for doc in docs:
        print("sphinx:%s %s" % (doc, "FAILED" if doc in failed else "PASSED"))
    for doc in sorted(failed - set(docs)):
        print("sphinx:%s FAILED" % doc)
    print("sphinx:__build__ %s" % ("FAILED" if unattributed else "PASSED"))
    for line in unattributed:
        sys.stderr.write("docs_test_report: unattributed -> %s\\n" % line)

    return 1 if (failed or unattributed) else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh — `-b dummy` still reads every source and resolves every
# reference, emitting the same warnings as the html builder, but skips writing
# output; with CI=true the html builder would make sphinxext-photofinish
# reprocess ~106 MB of images on every act for no additional signal.
# `-W` is deliberately NOT passed: the act must not abort on the first warning,
# because the per-document verdicts ARE the results.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/frc-docs
OUT=/tmp/sphinx.stdout
ERR=/tmp/sphinx.stderr
rm -rf /tmp/docsbuild "$OUT" "$ERR"
sphinx-build -b dummy -T source /tmp/docsbuild > "$OUT" 2> "$ERR"
sphinx_rc=$?
echo "sphinx-build exit=${sphinx_rc}"
echo "----- sphinx warnings -----"
cat "$ERR"
echo "----- per-document results -----"
python3 /home/docs_test_report.py "$OUT" "$ERR" /home/frc-docs/source
"""


class FrcDocsImageBase(Image):
    """Shared base image.

    dockerfile() is deliberately NOT overridden: the fork's Image.dockerfile()
    already emits the mandated order (FROM -> apt -> clone -> WORKDIR -> reset ->
    checkout ${BASE_COMMIT} -> extra_setup -> hardening/scrub + assertions ->
    CMD), and DockerfileEnhancer then prepends the syntax directive, ARGs
    (including an EMPTY ARG BASE_COMMIT), ENV, LABEL and the CA-cert links. The
    result is Main_Tasks/Base_Image_Ideal.Dockerfile. `BASE_COMMIT` and
    `REPO_URL` are supplied by build_dataset as docker build args, read from the
    dataset -- so nothing commit-specific is ever written into this file.
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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # librsvg2-bin : sphinxcontrib-svg2pdfconverter looks for rsvg-convert.
        # libtk8.6     : sphinxext-photofinish imports `turtle` -> `tkinter`;
        #                without it every build dies with "Could not import
        #                extension sphinxext.photofinish (libtk8.6.so: cannot
        #                open shared object file)".
        return ["librsvg2-bin", "libtk8.6"]

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so source/requirements.txt is
        # this PR's own file. Not `|| true`: this is the toolchain, not a cache,
        # so a failure must fail the build instead of surfacing later as an
        # empty image. The import line proves the libtk8.6 package took effect.
        return (
            "RUN pip install --no-cache-dir --upgrade pip && \\\n"
            "    pip install --no-cache-dir -r source/requirements.txt && \\\n"
            '    python -c "import sphinx, sphinxext.photofinish, '
            'sphinxext.rediraffe, furo, frccontrol" && \\\n'
            "    sphinx-build --version"
        )


class FrcDocsImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and act scripts, run
    prepare.sh. Nothing else -- the shape of
    Main_Tasks/pr_specific dockerfile.dockerfile. The repo is already cloned,
    checked out and scrubbed in the base."""

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
        return FrcDocsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "docs_test_report.py", _DOCS_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
pip install --no-cache-dir -r source/requirements.txt || true
python -c "import sphinx, sphinxext.photofinish, sphinxext.rediraffe, furo, frccontrol"
sphinx-build --version
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/frc-docs
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/frc-docs
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/frc-docs
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("wpilibsuite", "frc-docs")
class FrcDocs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FrcDocsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what docs_test_report.py prints. The name
        # is captured non-greedily BEFORE the keyword, so no duration or count can
        # leak into it and manufacture a false transition between acts.
        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
