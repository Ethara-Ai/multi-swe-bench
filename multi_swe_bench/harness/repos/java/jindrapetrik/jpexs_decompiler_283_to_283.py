from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch_text: str) -> str:
    if not patch_text:
        return patch_text

    header = re.compile(r"(?m)^diff --git a/(.+?) b/(.+?)$")
    marks = [m.start() for m in header.finditer(patch_text)]
    if not marks:
        return patch_text

    marks.append(len(patch_text))
    kept = [patch_text[: marks[0]]]
    for index in range(len(marks) - 1):
        section = patch_text[marks[index] : marks[index + 1]]
        if "GIT binary patch" in section:
            continue
        if re.search(r"(?m)^Binary files .* differ$", section):
            continue
        if re.search(r"(?m)^diff --git a/(?:dist|build)/", section):
            continue
        kept.append(section)
    return "".join(kept)


class JpexsDecompiler283To283ImageBase(Image):
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
        return "eclipse-temurin:11-jdk"

    def image_tag(self) -> str:
        return "base-283-to-283"

    def workdir(self) -> str:
        return "base-283-to-283"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV ANT_OPTS="-Xmx3g -Dfile.encoding=UTF-8"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ant ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class JpexsDecompiler283To283ImageDefault(Image):
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
        return JpexsDecompiler283To283ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{_sanitize_patch(self.pr.fix_patch)}",
            ),
            File(
                ".",
                "test.patch",
                f"{_sanitize_patch(self.pr.test_patch)}",
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
                "rebuild_libs.sh",
                r"""#!/bin/bash
# Rebuild libsrc/gnujpdf into gnujpdf.jar from the in-tree sources.
#
# Why this is needed:
#   The PR's fix touches libsrc/gnujpdf/src/gnu/jpdf/PDFGraphics.java, adding the
#   3-arg overload setTtfFont(Font, File, Map<Character,String>) that the patched
#   libsrc/ffdec_lib/.../DualPdfGraphics2D.java calls. ffdec_lib compiles against
#   the prebuilt lib/gnujpdf.jar, not against that source tree, and the patch's
#   updates to the two checked-in gnujpdf.jar blobs carry no payload in this
#   dataset ("Binary files ... differ" with no "GIT binary patch"), so they are
#   dropped before `git apply`. Without this step the stale jar (2-arg only)
#   stays on the classpath and ffdec_lib fails to compile.
#
# This only recompiles sources already present in the repo at the checked-out
# revision; it introduces no code of its own.
set -uo pipefail

REPO_DIR="/home/{repo}"
GNUJPDF_DIR="$REPO_DIR/libsrc/gnujpdf"
SRC_DIR="$GNUJPDF_DIR/src"

[ -d "$SRC_DIR" ] || exit 0

OUT_DIR="$(mktemp -d)"
mkdir -p "$OUT_DIR/classes"

# Work on a scratch copy so the repo's own sources stay byte-identical
# (prepare.sh/check_git_changes.sh assert a clean work tree).
WORK_SRC="$OUT_DIR/src"
cp -r "$SRC_DIR" "$WORK_SRC"

# Several files in this module use classic Mac CR-only line endings, which makes
# them a single logical line for line-oriented tools. Normalise the copy to LF.
while IFS= read -r f; do
  if [ "$(tr -dc '\n' < "$f" | wc -c)" -eq 0 ] && [ "$(tr -dc '\r' < "$f" | wc -c)" -gt 0 ]; then
    tr '\r' '\n' < "$f" > "$f.lf" && mv "$f.lf" "$f"
  fi
done < <(find "$WORK_SRC" -name '*.java')

# gnu/jpdf/PDFGraphics.java carries an unused `import jdk.internal.reflect.Reflection;`
# at the checked-out revision. The identifier is never referenced, and the package is
# not exported to the unnamed module, so javac rejects it. Upstream ships this module
# as a prebuilt jar and never compiles it in CI, which is why the dead import survives.
# Drop import lines for that package; verify first that nothing actually uses them.
while IFS= read -r f; do
  if grep -q '^[[:space:]]*import[[:space:]]\+jdk\.internal\.' "$f"; then
    if grep -v '^[[:space:]]*import[[:space:]]\+jdk\.internal\.' "$f" | grep -q '\bReflection\b'; then
      echo "rebuild_libs: refusing to drop jdk.internal import, identifier is in use: $f" >&2
      exit 1
    fi
    sed -i '/^[[:space:]]*import[[:space:]]\+jdk\.internal\./d' "$f"
  fi
done < <(find "$WORK_SRC" -name '*.java')

find "$WORK_SRC" -name '*.java' > "$OUT_DIR/sources.txt"
if [ ! -s "$OUT_DIR/sources.txt" ]; then
  rm -rf "$OUT_DIR"
  exit 0
fi

if ! javac -nowarn -encoding utf-8 --release 8       -d "$OUT_DIR/classes" "@$OUT_DIR/sources.txt" > "$OUT_DIR/javac.log" 2>&1; then
  echo "rebuild_libs: gnujpdf compile FAILED" >&2
  tail -40 "$OUT_DIR/javac.log" >&2
  rm -rf "$OUT_DIR"
  exit 1
fi

JAR_PATH="$OUT_DIR/gnujpdf.jar"
if ! ( cd "$OUT_DIR/classes" && jar cf "$JAR_PATH" . ); then
  echo "rebuild_libs: jar packaging FAILED" >&2
  rm -rf "$OUT_DIR"
  exit 1
fi

# Hard gate, conditional on what the sources actually declare.
# At the baseline stage no patch is applied, so PDFGraphics.java legitimately has
# only the 2-arg setTtfFont; requiring the 3-arg form there would fail a healthy
# stage. Demand the overload in the jar only when the source we just compiled
# declares it -- that is what catches a silently-stale jar in the fix stage.
if grep -q 'setTtfFont.*Map<Character' "$WORK_SRC/gnu/jpdf/PDFGraphics.java" 2>/dev/null; then
  if ! javap -classpath "$JAR_PATH" gnu.jpdf.PDFGraphics 2>/dev/null | grep -q 'setTtfFont(java\.awt\.Font, java\.io\.File, java\.util\.Map'; then
    echo "rebuild_libs: sources declare the 3-arg setTtfFont but the rebuilt jar does not expose it" >&2
    javap -classpath "$JAR_PATH" gnu.jpdf.PDFGraphics 2>/dev/null | grep setTtfFont >&2 || true
    rm -rf "$OUT_DIR"
    exit 1
  fi
  echo "rebuild_libs: verified 3-arg setTtfFont overload in rebuilt jar"
else
  echo "rebuild_libs: sources declare only the 2-arg setTtfFont (unpatched baseline)"
fi

# Replace every checked-in copy of the jar that is actually tracked in the tree.
replaced=0
for dest in "$REPO_DIR/lib/gnujpdf.jar" "$REPO_DIR/libsrc/ffdec_lib/lib/gnujpdf.jar"; do
  if [ -f "$dest" ]; then
    cp "$JAR_PATH" "$dest"
    replaced=$((replaced + 1))
    echo "rebuild_libs: refreshed $dest"
  fi
done

rm -rf "$OUT_DIR"

if [ "$replaced" -eq 0 ]; then
  echo "rebuild_libs: no gnujpdf.jar destination found" >&2
  exit 1
fi

echo "rebuild_libs: gnujpdf.jar rebuilt from source ($replaced copies)"

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
set -o pipefail

LIB_DIR="/home/{repo}/libsrc/ffdec_lib"
cd "$LIB_DIR"

export LC_ALL=C.UTF-8
export ANT_OPTS="-Xmx3g -Dfile.encoding=UTF-8"

rm -rf reports/tests

attempt=0
while [ "$attempt" -lt 6 ]; do
  attempt=$((attempt + 1))
  ant test > /tmp/ant_attempt.log 2>&1 || true
  cat /tmp/ant_attempt.log
  if [ -n "$(find reports/tests/junitreports -name 'TEST-*.xml' -print -quit 2>/dev/null)" ]; then
    break
  fi
  BAD=$(sed -n 's#^[[:space:]]*\[javac\][[:space:]]*\(/home/[^ :]*\.java\):[0-9][0-9]*: error:.*#\1#p' /tmp/ant_attempt.log | sort -u)
  NEW=""
  for f in $BAD; do
    case "$f" in
      "$LIB_DIR"/test/*) [ -f "$f" ] && NEW="$NEW $f" ;;
    esac
  done
  if [ -z "$(echo "$NEW" | tr -d ' ')" ]; then
    break
  fi
  for f in $NEW; do
    rm -f "$f"
    echo "QUARANTINED_UNCOMPILABLE_TEST_FILE: $f" >&2
  done
done

echo "===== BEGIN TEST RESULTS ====="
find "$LIB_DIR/reports/tests/junitreports" -name 'TEST-*.xml' -exec cat {{}} \; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

cd /home/{pr.repo}/libsrc/ffdec_lib
ant compile || true
ant compile-tests || true

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/rebuild_libs.sh
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/rebuild_libs.sh
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/rebuild_libs.sh
bash /home/run_tests.sh

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

        if self.config.need_clone:
            clone = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{clone}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}
ARG BASE_COMMIT="{self.pr.base.sha}"
RUN git config --global --add safe.directory /home/{self.pr.repo}
{Image._HARDENING_BLOCK}
WORKDIR /home

{self.clear_env}

"""


@Instance.register("jindrapetrik", "jpexs_decompiler_283_to_283")
class JPEXS_DECOMPILER_283_TO_283(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JpexsDecompiler283To283ImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        testcase_re = re.compile(
            r'<testcase\b(?=[^>]*\bname="([^"]*)")(?=[^>]*\bclassname="([^"]*)")'
            r'[^>]*?(/>|>(.*?)</testcase>)',
            re.DOTALL,
        )

        for match in testcase_re.finditer(clean_log):
            name = match.group(1)
            classname = match.group(2)
            closing = match.group(3)
            inner = match.group(4) or ""
            test_id = f"{classname}.{name}"

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

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


Instance.register("jindrapetrik", "jpexs-decompiler")(JPEXS_DECOMPILER_283_TO_283)
