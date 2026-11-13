import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# openrewrite/rewrite is a Gradle multi-module Java repo that also carries the
# C# language server under rewrite-csharp/csharp (an MSBuild solution with an
# xUnit test project, OpenRewrite.Tests). This config grades PRs whose
# test.patch changes tests of that .NET solution: every OpenRewrite*.csproj at
# the bases targets net10.0, so the official multi-arch .NET 10 SDK image
# (Ubuntu noble, amd64 + arm64, git included) is the toolchain. There is no
# global.json, so the SDK is pinned here.
#
# The Gradle half of the repo is not covered: at current bases its build needs
# io.moderne:jsonrpc >= 1.3.x and org.openrewrite build-plugin dependencies
# (rewrite 8.91+) that are published only to the credentialed Code Genome
# repository (artifacts.codegenomeproject.org), not to Maven Central.
_DOTNET_IMAGE = "mcr.microsoft.com/dotnet/sdk:10.0.403-noble"

_ENV = """export CI=true
export NO_COLOR=1
export TERM=dumb
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
export DOTNET_CLI_USE_MSBUILD_SERVER=0
export MSBUILDDISABLENODEREUSE=1
export NUGET_XMLDOC_MODE=skip
# .NET W^X double-mapping segfaults under QEMU amd64 emulation
export DOTNET_EnableWriteXorExecute=0"""

# Helper: finds the test projects and classes test.patch touches, the C#
# sources a failed build names, the tests test.patch adds or rewrites in a
# source that no longer compiles, and turns the JUnit XML that
# JunitXml.TestLogger writes into "ORW_TEST <STATUS> <project>:<class>#<test>".
TESTS_PY = r'''import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO]]"
ERROR = re.compile(r"^\s*(/\S+?\.cs)\(\d+,\d+\): error [A-Z]+\d+:")
TEST_ATTR = re.compile(
    r"\[\s*(?:[\w.]+\s*(?:\([^\]]*?\))?\s*,\s*)*(?:Xunit\.)?(?:Fact|Theory)(?:Attribute)?\b[^\]]*\]"
)
TYPE_DECL = re.compile(r"(?<![\w.])(?:class|struct|record|interface)\s+(\w+)")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_blocks(patch_file):
    """{path: (status, [lines of the block])} keyed by the b/ path (a/ for deletions)."""
    blocks = {}
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for block in re.split(r"^(?=diff --git )", text, flags=re.M):
        head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if not head:
            continue
        a, b = head.groups()
        if re.search(r"^deleted file mode", block, re.M):
            blocks[a] = ("deleted", block.splitlines())
        elif re.search(r"^new file mode", block, re.M):
            blocks[b] = ("new", block.splitlines())
        else:
            blocks[b] = ("modified", block.splitlines())
            if a != b:
                blocks[a] = ("deleted", block.splitlines())
    return blocks


def body_lines(lines, sign):
    out = []
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line[:1] in (sign, " "):
            out.append(line[1:])
    return "\n".join(out) + "\n"


def added_line_numbers(lines):
    added = set()
    new_no = 0
    for line in lines:
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            new_no = int(m.group(1))
            continue
        if not new_no:
            continue
        if line.startswith("+"):
            added.add(new_no)
            new_no += 1
        elif line.startswith(" "):
            new_no += 1
    return added


def project_of(rel):
    """The test project (dir with a *.csproj that references the test SDK) holding rel."""
    if not rel.endswith(".cs"):
        return None
    d = os.path.dirname(rel)
    while d:
        projs = sorted(glob.glob(os.path.join(REPO, d, "*.csproj")))
        if projs:
            with open(projs[0], encoding="utf-8", errors="replace") as fh:
                if "Microsoft.NET.Test.Sdk" not in fh.read():
                    return None
            return os.path.relpath(projs[0], REPO)
        d = os.path.dirname(d)
    return None


def mask(text):
    """Blank comments and string/char literals (keeping newlines and offsets)."""
    out = list(text)
    n = len(text)
    i = 0

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif text[i] == '"':
            k = i
            while k < n and text[k] == '"':
                k += 1
            q = k - i
            if q >= 3:
                end = text.find('"' * q, k)
                j = n if end < 0 else end + q
            elif q == 2:
                j = i + 2
            else:
                verbatim = text[max(0, i - 2):i].endswith("@") or text[max(0, i - 2):i] == "@$"
                j = i + 1
                while j < n:
                    if verbatim:
                        if text[j] == '"':
                            if j + 1 < n and text[j + 1] == '"':
                                j += 2
                                continue
                            break
                    else:
                        if text[j] == "\\":
                            j += 2
                            continue
                        if text[j] in '"\n':
                            break
                    j += 1
                j += 1
            blank(i, j)
            i = j
        elif text[i] == "'":
            j = i + 1
            while j < n and text[j] not in "'\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            j += 1
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def braces(masked):
    match = {}
    stack = []
    for i, c in enumerate(masked):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            match[stack.pop()] = i
    return match


def analyse(text):
    """[(class_fqn, method, first_line, last_line)] for each xUnit test method."""
    masked = mask(text)
    match = braces(masked)
    ns = re.search(r"^\s*namespace\s+([\w.]+)", masked, re.M)
    prefix = ns.group(1) + "." if ns else ""
    types = []
    for m in TYPE_DECL.finditer(masked):
        rest = masked[m.end():]
        b = rest.find("{")
        s = rest.find(";")
        if b < 0 or (0 <= s < b):
            continue
        open_at = m.end() + b
        if open_at in match:
            types.append((m.group(1), open_at, match[open_at]))

    def line(pos):
        return masked.count("\n", 0, pos) + 1

    tests = []
    for m in TEST_ATTR.finditer(masked):
        rest = masked[m.end():]
        stop = min([p for p in (rest.find("{"), rest.find("=>"), rest.find(";")) if p >= 0] or [-1])
        if stop < 0:
            continue
        sig = re.sub(r"\[[^\[\]]*\]", " ", rest[:stop])
        name = re.search(r"(\w+)\s*(?:<[^<>()]*>)?\s*\(", sig)
        if not name:
            continue
        start = m.end() + stop
        if masked.startswith("{", start) and start in match:
            end = match[start]
        else:
            end = masked.find(";", start)
            end = len(masked) - 1 if end < 0 else end
        chain = [t[0] for t in sorted(types, key=lambda t: t[1]) if t[1] < m.start() < t[2]]
        if not chain:
            continue
        tests.append((prefix + "+".join(chain), name.group(1), line(m.start()), line(end)))
    return tests


def source_of(rel, status, lines):
    path = os.path.join(REPO, rel)
    if status == "new":
        return body_lines(lines, "+")
    if status == "deleted":
        return body_lines(lines, "-")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return ""


def targets(patch_file):
    """'<project.csproj>\t<class fqn>' for each test class of a touched test source."""
    out = set()
    for rel, (status, lines) in patch_blocks(patch_file).items():
        proj = project_of(rel)
        if not proj:
            continue
        for cls, _, _, _ in analyse(source_of(rel, status, lines)):
            out.add(proj + "\t" + cls)
    for row in sorted(out):
        print(row)


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = ERROR.match(line)
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def in_project(src_file, proj):
    """exit 0 when src_file lies in the directory of the test project proj."""
    pdir = os.path.dirname(os.path.join(REPO, proj)) + os.sep
    sys.exit(0 if os.path.abspath(src_file).startswith(pdir) else 1)


def in_patch(src_file, patch_file="/home/test.patch"):
    """exit 0 when src_file is a file test.patch adds or changes."""
    rel = os.path.relpath(os.path.abspath(src_file), REPO)
    sys.exit(0 if rel in patch_blocks(patch_file) else 1)


def methods(src_file, patch_file="/home/test.patch"):
    """Tests that test.patch adds or rewrites in src_file, whose project no longer
    builds with it -> FAILED (they cannot run). Untouched tests of that file
    are left unreported rather than marked failed."""
    rel = os.path.relpath(os.path.abspath(src_file), REPO)
    proj = project_of(rel)
    block = patch_blocks(patch_file).get(rel)
    if not proj or not block or not os.path.isfile(src_file):
        return
    added = added_line_numbers(block[1])
    with open(src_file, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    name = os.path.splitext(os.path.basename(proj))[0]
    for cls, meth, first, last in analyse(text):
        if any(first <= n <= last for n in added):
            print("ORW_TEST FAILED " + name + ":" + cls + "#" + meth + " (test source does not compile)")


def patch_sources(proj, patch_file="/home/test.patch"):
    """Existing C# sources of proj that test.patch adds or changes."""
    for rel, (status, _) in sorted(patch_blocks(patch_file).items()):
        if status != "deleted" and project_of(rel) == proj and os.path.isfile(os.path.join(REPO, rel)):
            print(os.path.join(REPO, rel))


def junit(rdir):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for f in sorted(glob.glob(os.path.join(rdir, "*.xml"))):
        mod = os.path.splitext(os.path.basename(f))[0]
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter():
            if local(tc.tag) != "testcase":
                continue
            cls = (tc.get("classname") or "").strip()
            name = (tc.get("name") or "").strip()
            if not cls or not name:
                continue
            tid = re.sub(r"\s+", " ", mod + ":" + cls + "#" + name).strip()
            tags = {local(c.tag) for c in tc}
            if tags & {"failure", "error"}:
                status = "FAILED"
            elif "skipped" in tags:
                status = "SKIPPED"
            else:
                status = "PASSED"
            if tid not in results or rank[status] > rank[results[tid]]:
                results[tid] = status
    for tid in sorted(results):
        print("ORW_TEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {
        "targets": targets,
        "broken": broken,
        "in_project": in_project,
        "in_patch": in_patch,
        "methods": methods,
        "patch_sources": patch_sources,
        "junit": junit,
    }[cmd](*args)
'''

# Driver: build each touched test project offline (dropping test sources that
# test.patch left uncompilable, and reporting the tests it adds or rewrites in
# them as FAILED), then run only the touched test classes. The exit code is
# dotnet's, never swallowed.
TESTS_SH = r'''#!/bin/bash
set -eo pipefail
REPO=/home/[[REPO]]
HELPER=/home/orw_tests.py
RESULTS=/tmp/orw_results
BUILD="dotnet build --no-restore -nologo -m:3 -nodeReuse:false -p:UseSharedCompilation=false"
cd "$REPO"

python3 "$HELPER" targets "$1" > /tmp/orw_targets
cat /tmp/orw_targets | sed 's/^/ORW_TARGET /'
test -s /tmp/orw_targets
PROJS=$(cut -f1 /tmp/orw_targets | sort -u)
rm -rf "$RESULTS"
mkdir -p "$RESULTS"
: > /tmp/orw_removed

# 0 = built, 2 = blocked by a test.patch source outside the test project
build_project() {
    local proj=$1 i f rc
    for i in $(seq 1 10); do
        rc=0
        $BUILD "$proj" > /tmp/orw_build.log 2>&1 || rc=$?
        grep -E ': error |Build succeeded|Build FAILED|error\(s\)' /tmp/orw_build.log | sort -u | head -n 60 || true
        [ "$rc" = 0 ] && return 0
        python3 "$HELPER" broken /tmp/orw_build.log > /tmp/orw_broken
        if [ ! -s /tmp/orw_broken ]; then
            tail -n 60 /tmp/orw_build.log
            echo "ORW_BUILD_FAILED $proj (not a source error)"
            return 1
        fi
        local own=0 other=0
        : > /tmp/orw_other
        while IFS= read -r f; do
            if python3 "$HELPER" in_project "$f" "$proj"; then
                own=1
                echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
                python3 "$HELPER" methods "$f" >> /tmp/orw_removed
                rm -f "$f"
            else
                echo "$f" >> /tmp/orw_other
            fi
        done < /tmp/orw_broken
        [ "$own" = 1 ] && continue
        # Only sources outside the test project fail. That is reportable only
        # when test.patch itself added or changed every one of them.
        while IFS= read -r f; do
            if ! python3 "$HELPER" in_patch "$f"; then
                echo "ORW_BUILD_FAILED $proj (${f#$REPO/} is not from test.patch)"
                return 1
            fi
        done < /tmp/orw_other
        echo "ORW_PROJECT_BLOCKED $proj"
        python3 "$HELPER" patch_sources "$proj" > /tmp/orw_srcs
        while IFS= read -r f; do
            python3 "$HELPER" methods "$f" >> /tmp/orw_removed
        done < /tmp/orw_srcs
        return 2
    done
    echo "ORW_BUILD_FAILED $proj (too many rounds)"
    return 1
}

rc=0
for proj in $PROJS; do
    brc=0
    build_project "$proj" || brc=$?
    if [ "$brc" = 1 ]; then
        exit 1
    fi
    if [ "$brc" = 2 ]; then
        rc=1
        continue
    fi
    filter=$(awk -F '\t' -v p="$proj" '$1 == p {printf "%sFullyQualifiedName~%s.", sep, $2; sep="|"}' /tmp/orw_targets)
    name=$(basename "$proj" .csproj)
    trc=0
    dotnet test --no-build --no-restore -nologo "$proj" --filter "$filter" \
        --logger "junit;LogFilePath=$RESULTS/$name.xml" > /tmp/orw_test.log 2>&1 || trc=$?
    grep -E '^\s+Failed |Passed!|Failed!|No test matches|Total tests' /tmp/orw_test.log | tail -n 200 || true
    [ "$trc" = 0 ] || rc=$trc
done
cat /tmp/orw_removed
python3 "$HELPER" junit "$RESULTS"
echo "ORW_DOTNET_EXIT=$rc"
exit "$rc"
'''


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


class RewriteImageBase(Image):
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
        return _DOTNET_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        return (
            "ENV CI=true\n"
            "ENV DOTNET_CLI_TELEMETRY_OPTOUT=1\n"
            "ENV DOTNET_NOLOGO=1\n"
            "RUN git config --global --add safe.directory '*' && dotnet --version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class RewriteImageDefault(Image):
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
        return RewriteImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
        tests_py = TESTS_PY.replace("[[REPO]]", repo)
        tests_sh = TESTS_SH.replace("[[REPO]]", repo)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            File(".", "orw_tests.py", tests_py),
            File(".", "orw_tests.sh", tests_sh),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}
python3 /home/orw_tests.py targets /home/test.patch > /tmp/orw_targets
cat /tmp/orw_targets
test -s /tmp/orw_targets
PROJS=$(cut -f1 /tmp/orw_targets | sort -u)

# Restore every NuGet package of the touched test projects into
# ~/.nuget/packages (retrying transient network failures), then build them.
for proj in $PROJS; do
  ok=0
  for attempt in 1 2 3; do
    if dotnet restore -nologo "$proj"; then
      ok=1
      break
    fi
    sleep 20
  done
  test "$ok" = 1
  dotnet build --no-restore -nologo -m:3 -nodeReuse:false -p:UseSharedCompilation=false "$proj"
done

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
for proj in $PROJS; do
  test -f "$(dirname "$proj")/obj/project.assets.json"
  dotnet build --no-restore -nologo -m:3 -nodeReuse:false -p:UseSharedCompilation=false "$proj" > /dev/null
  dotnet test --no-build --no-restore -nologo "$proj" --list-tests | grep -q "The following Tests are available"
done
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
bash /home/orw_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch
bash /home/orw_tests.sh /home/test.patch
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
bash /home/orw_tests.sh /home/test.patch
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("openrewrite", "rewrite")
class REWRITE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RewriteImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        result_re = re.compile(
            r"^ORW_TEST (PASSED|FAILED|SKIPPED) (\S+?:\S.*?#.+?)"
            r"(?: \(test source does not compile\))?\s*$"
        )
        buckets = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }
        for raw in log.splitlines():
            found = result_re.match(raw.strip())
            if found:
                buckets[found.group(1)].add(found.group(2))

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
