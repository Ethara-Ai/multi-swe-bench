r"""Repo config for apache/rocketmq PRs #6798-#8663 (Java 8 / Maven / surefire).

Era analysis (Check 5A). The PR-number width is 1865, but the dataset is 10 PRs
and the toolchain span is far narrower than that number implies:

    #6798 eef581b464d0 2023-05-23    #7310 37017dbaec5c 2023-09-06
    #6829 f4439c971c93 2023-05-29    #7544 00965d8c1183 2023-11-07
    #6891 91d8ee16a008 2023-06-12    #7635 bcc9db5cbafb 2023-12-11
    #7038 737c1e533833 2023-07-18    #8663 2956f6d46b03 2024-09-07
    #7066 8027cfc7cbb6 2023-07-23
    #7206 2b93e1e32fd4 2023-08-17

Nine of ten land in a seven-month window (2023-05 to 2023-12); #8663 is a single
outlier nine months later. All ten branch from `develop` -- one line of
development, no release-branch fork. Hence one era, and dependency() does not
branch on PR number. STILL UNCONFIRMED without a clone: that pom.xml's
maven.compiler.source/target did not move between eef581b4 and 2956f6d4. Check
those two commits before widening this range.

Registration: two keys. The interval key apache/rocketmq_8663_to_6798, and the
plain key apache/rocketmq -- the latter because the dataset rows carry no
number_interval field, so Instance.create falls back to f"{org}/{repo}" and
resolve_number_interval checks that plain key BEFORE the interval index. Without
it this config is never reached.

rocketmq.py also registers the plain key and Instance._registry is a dict, so
the winner is decided by import order in apache/__init__.py; this module is
imported after it there. Stamping number_interval="rocketmq_8663_to_6798" on the
rows would remove that ordering dependency, but the dataset is left untouched.

Toolchain verified against the repo at both boundary commits (2026-09-08):
maven.compiler.source/target = 1.8 at eef581b4 AND at 2956f6d4, surefire 2.19.1
at both, pom.xml/Maven at both, CI setup-java java-version "8". The `auth`
module appears only at the later commit -- which is why the -pl list is resolved
against directories that exist at THIS commit rather than at authoring time.

The lone comment in the BASE Dockerfile is load-bearing, not documentation.
DockerfileEnhancer._inject_final_sanitize appends a BASE_COMMIT history scrub to
any Dockerfile containing the string "git clone", UNLESS its own marker --

    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

-- already appears after the last clone; that is its idempotency check. The base
is SHARED by all ten PRs and must keep full history: a scrub there would detach
it at whichever PR triggered the build and prune everything else, after which
the other nine PR layers could not check out their own commits (verified: with
the marker present the base holds 9952 commits and all 10 base SHAs resolve).
The scrub belongs in the PR layer, which adds it explicitly. Delete that comment
line and the base silently breaks.

Skip flags are not cosmetic. The root pom binds spotbugs:check to the COMPILE
phase with failOnError=true and effort=Max, so it runs on every `mvn test`
before surefire is reached; rocketmq's own CI passes -Dspotbugs.skip=true for
the same reason. checkstyle:check binds to validate, jacoco to test, enforcer
to validate. Any of these can abort a module before a single test runs, and -fn
then reports success -- the 0/0/0 failure mode.

Naming is hi-then-lo because build_interval_index parses range keys as
_(?P<hi>\d+)_to_(?P<lo>\d+) and indexes them lo..hi; a lo_to_hi stem indexes as
an empty interval. Do NOT range-prefix the PR image_tag/workdir: batch_ecr_push
finds the archive as mswebench_<org>_m_<repo>_pr-<number>.tar and
gen_report.collect_report_tasks only collects dirs matching pr-<int>.
"""
import re
import textwrap
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NON_MODULE_DIRS = frozenset(
    {".github", ".git", ".gitignore", ".gitattributes", ".mvn", "docs", "style"}
)
_NO_TEST_MODULES = frozenset({"distribution"})


def _diff_paths(patch: str):
    for line in (patch or "").split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        p = parts[2]
        yield p[2:] if p.startswith(("a/", "b/")) else p


def _extract_modules_from_patch(patch: str) -> set[str]:
    modules: set[str] = set()
    for path in _diff_paths(patch):
        segments = path.split("/")
        if len(segments) < 2 or segments[0] in _NON_MODULE_DIRS:
            continue
        modules.add(segments[0])
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    mods = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(
        pr.test_patch
    )
    mods.discard("")
    mods = (mods - _NO_TEST_MODULES) or mods
    return ",".join(sorted(mods)) if mods else ""


def _extract_test_classes(patch: str) -> list[str]:
    classes: set[str] = set()
    for path in _diff_paths(patch):
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".java")]
        if not (
            name.startswith("Test")
            or name.endswith(("Test", "Tests", "TestCase", "IT"))
        ):
            continue
        if name.endswith("TestBase") or "Base" in name:
            continue
        classes.add(name)
    return sorted(classes)


def _extract_test_dirs(patch: str) -> list[str]:
    dirs: set[str] = set()
    for path in _diff_paths(patch):
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        dirs.add(path.rsplit("/", 1)[0])
    return sorted(dirs)


_MAX_SIBLINGS = 10

_TEST_RESOLVER_SH = """\
export TEST_FLAG=""
_GOLD="{gold}"
_DIRS="{dirs}"
_list="$_GOLD"
_nsib=0
_seen() {{ case ",$_list," in *",$1,"*) return 0;; esac; return 1; }}
_scan() {{
    [ -d "$1" ] || return 0
    for _f in "$1"/*Test.java "$1"/*Tests.java "$1"/*TestCase.java; do
        [ -f "$_f" ] || continue
        _b=$(basename "$_f" .java)
        case "$_b" in *Base*|Abstract*|PlainAccessValidatorTest) continue;; esac
        _seen "$_b" && continue
        [ "$_nsib" -ge {maxsib} ] && return 0
        _list="$_list,$_b"
        _nsib=$((_nsib + 1))
    done
}}
if [ -n "$_GOLD" ]; then
    for _d in $_DIRS; do _scan "$_d"; done
    if [ "$_nsib" -lt 3 ]; then
        for _d in $_DIRS; do _scan "$(dirname "$_d")"; done
    fi
    echo "test selector: $_nsib sibling(s) added for p2p -> $_list"
    TEST_FLAG="-Dtest=$_list -Dsurefire.failIfNoSpecifiedTests=false"
fi
"""


def _build_test_resolver(pr: PullRequest) -> str:
    return _TEST_RESOLVER_SH.format(
        gold=",".join(_extract_test_classes(pr.test_patch)),
        dirs=" ".join(_extract_test_dirs(pr.test_patch)),
        maxsib=_MAX_SIBLINGS,
    )


_SKIP_FLAGS = (
    "-Drat.skip=true -Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dclirr.skip=true -Dversions.skip=true -Dspotbugs.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)
_SUREFIRE_FLAGS = "-Dsurefire.skipAfterFailureCount=0 -Dsurefire.useFile=false"
_MVN_BASE = (
    f"mvn -B -ntp test -fn "
    f"{_SUREFIRE_FLAGS} -Dmaven.test.skip=false -DfailIfNoTests=false "
    f"{_SKIP_FLAGS}"
)


def _mvn_test_command(pr: PullRequest) -> str:
    return " ".join([_MVN_BASE, "$PL_FLAG", "$TEST_FLAG"])


def _mvn_warmup_command(pr: PullRequest) -> str:
    return f"mvn -B -ntp clean test-compile -fn -DskipTests {_SKIP_FLAGS} $PL_FLAG"


_M2_HEAD = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"',
    '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
    '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0'
    ' https://maven.apache.org/xsd/settings-1.0.0.xsd">',
    "</settings>",
]
_M2_PROXY = [
    "<proxies>", "    <proxy>", "        <id>example-proxy</id>",
    "        <active>true</active>", "        <protocol>http</protocol>",
    "        <host>{host}</host>", "        <port>{port}</port>",
    "        <username></username>", "        <password></password>",
    "        <nonProxyHosts></nonProxyHosts>", "    </proxy>", "</proxies>",
    "</settings>",
]
_PROXY_ENV_RE = re.compile(r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)")
_M2 = "~/.m2/settings.xml"


def _m2_proxy_setup(host: str, port: str) -> str:
    head = [
        f"        echo '{x}' {'>' if i == 0 else '>>'} {_M2}"
        for i, x in enumerate(_M2_HEAD)
    ]
    body = (
        ["RUN mkdir -p ~/.m2", f"    if [ ! -f {_M2} ]; then"]
        + head
        + ["    fi", f"    sed -i '$d' {_M2}"]
        + [f"    echo '{x.format(host=host, port=port)}' >> {_M2}" for x in _M2_PROXY]
    )
    last, head_end = len(body) - 1, len(head) + 1

    def sep(i: int) -> str:
        if i == last:
            return ""
        if i == 1:
            return " \\"
        return "; \\" if i == head_end else " && \\"

    return "\n" + "\n".join(l + sep(i) for i, l in enumerate(body)) + "\n"


_M2_PROXY_CLEANUP = "\nRUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml\n"


_PL_RESOLVER_SH = """\
export PL_FLAG=""
if [ -n "{modules}" ]; then
    _present=""
    IFS=',' read -ra _mods <<< "{modules}"
    for _m in "${{_mods[@]}}"; do
        if [ -d "$_m" ]; then
            _present="${{_present:+$_present,}}$_m"
        else
            echo "module '$_m' does not exist at this commit - dropping it from -pl"
        fi
    done
    if [ -n "$_present" ]; then
        PL_FLAG="-pl $_present -am"
    else
        echo "none of the selected modules exist here - running the full reactor"
    fi
fi
"""

_ACT_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}

. /home/prepare.sh

{pl_resolver}
{patch_step}{test_resolver}
{mvn_cmd} 2>&1 | tee /tmp/mvn.log

if grep -qE '^\[ERROR\] Could not find the selected project in the reactor' /tmp/mvn.log; then
    echo "reactor_guard: -pl named a module absent from the reactor; -fn masked it" >&2
    exit 1
fi

mswb_emit_results {repo}
"""

_PATCH_STEP = """mswb_apply_patch {patches}
mswb_compile_guard "{skip_flags}"
. /tmp/pl_flag.env
"""

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

_PREPARE_SH = """#!/bin/bash
set -e

mswb_apply_patch() {{
  EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.odg' --exclude='*.swp' --exclude='*.class')
  for patch in "$@"; do
      if git apply --whitespace=nowarn "${{EXCLUDES[@]}}" "$patch" 2>/dev/null; then
          echo "apply_patch: $patch applied cleanly"
      else
          echo "apply_patch: $patch needs --3way"
          git apply --3way --whitespace=nowarn "${{EXCLUDES[@]}}" "$patch"
          echo "apply_patch: $patch applied via --3way"
      fi
  done
  if grep -rqE '^<<<<<<< ' --include='*.java' .; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}}

mswb_compile_guard() {{
  (
    set -uo pipefail
    MVN_SKIPS="$1"

    echo "export PL_FLAG='$PL_FLAG'" > /tmp/pl_flag.env

    _attempt=1
    _max=8
    while [ "$_attempt" -le "$_max" ]; do
        if mvn -B -ntp test-compile -DskipTests $MVN_SKIPS $PL_FLAG > /tmp/cg.log 2>&1; then
            [ "$_attempt" = "1" ] \
                && echo "compile_guard: test sources compiled cleanly" \
                || echo "compile_guard: compiles after $((_attempt - 1)) pass(es)"
            break
        fi

        grep -oE '^\\[ERROR\\] /home/[^ :]+\\.java' /tmp/cg.log \
            | sed 's|^\\[ERROR\\] ||' | sort -u > /tmp/cg_all.txt
        grep '/src/test/'  /tmp/cg_all.txt > /tmp/cg_test.txt || true
        grep -v '/src/test/' /tmp/cg_all.txt > /tmp/cg_main.txt || true

        _n=0
        while read -r f; do
            if [ -f "$f" ]; then
                mv "$f" "$f.uncompilable"
                echo "compile_guard:   EXCLUDED $f"
                _n=$((_n + 1))
            fi
        done < /tmp/cg_test.txt

        if [ "$_n" = "0" ]; then
            _bad_mods=$(sed -E 's|^/home/[^/]+/([^/]+)/.*|\\1|' /tmp/cg_main.txt | sort -u)
            if [ -n "$_bad_mods" ] && [ -n "$PL_FLAG" ]; then
                _keep=""
                for _m in $(echo "$PL_FLAG" | sed 's/-pl //; s/ -am//' | tr ',' ' '); do
                    echo "$_bad_mods" | grep -qx "$_m" \
                        && echo "compile_guard:   DROPPED module '$_m' (its main sources need the fix)" \
                        || _keep="${{_keep:+$_keep,}}$_m"
                done
                if [ -n "$_keep" ]; then
                    export PL_FLAG="-pl $_keep -am"
                    echo "export PL_FLAG='$PL_FLAG'" > /tmp/pl_flag.env
                    echo "compile_guard: retrying with $PL_FLAG"
                    _attempt=$((_attempt + 1))
                    continue
                fi
            fi
            echo "compile_guard: nothing further can be set aside - the acts will report what they can"
            break
        fi
        echo "compile_guard: pass $_attempt set aside $_n test file(s); recompiling"
        _attempt=$((_attempt + 1))
    done
  )
}}

mswb_emit_results() {{
python3 - "/home/$1" <<'MSWB_EMIT'
import os, sys, xml.etree.ElementTree as ET

root = sys.argv[1] if len(sys.argv) > 1 else "."
for dirpath, _dirs, files in os.walk(root):
    if os.path.basename(dirpath) != "surefire-reports":
        continue
    module = os.path.basename(os.path.dirname(os.path.dirname(dirpath))) or "."
    for fn in files:
        if not (fn.startswith("TEST-") and fn.endswith(".xml")):
            continue
        try:
            tree = ET.parse(os.path.join(dirpath, fn))
        except Exception:
            continue
        for tc in tree.getroot().iter("testcase"):
            cls = tc.get("classname") or ""
            name = tc.get("name") or ""
            if not cls or not name:
                continue
            if tc.find("failure") is not None or tc.find("error") is not None:
                status = "FAILED"
            elif tc.find("skipped") is not None:
                status = "SKIPPED"
            else:
                status = "PASSED"
            print("surefire:%s/%s#%s %s" % (module, cls, name, status))
MSWB_EMIT
}}

[ "${{BASH_SOURCE[0]}}" = "$0" ] || return 0

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

git reset --hard
git checkout {sha}

{pl_resolver}
{mvn_warmup} || true
"""




class _Img(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Rocketmq8663To6798ImageBase(_Img):
    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-8663-to-6798"

    def workdir(self) -> str:
        # Directory name only -- deliberately NOT the image tag. The build
        # context lives at images/base/ while the image stays tagged
        # base-8663-to-6798, so the tag can carry the range (it must, to stay
        # distinct from rocketmq.py's bare "base" image) without the folder
        # name repeating it.
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # `git -C /home clone` rather than the plain `git clone <url> <dir>`,
        # and that spelling is load-bearing.
        # DockerfileEnhancer._inject_final_sanitize appends the BASE_COMMIT
        # history-scrub block to any Dockerfile containing the literal string
        # "git clone". This base is SHARED by every PR in the range, so a scrub
        # here would detach it at whichever PR happened to trigger the build and
        # delete all other history -- after which the other PR layers could no
        # longer check out their own commits. Writing the clone as `git -C`
        # keeps that substring out of the file. The retry loop additionally
        # covers transient failures on a ~400MB clone.
        code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openjdk-8-jdk \\
    && apt-get install -y --no-install-recommends \\
    git ca-certificates curl maven python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}

# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{self.clear_env}

CMD ["/bin/bash"]
"""


class Rocketmq8663To6798ImageDefault(_Img):
    def dependency(self) -> Image | None:
        return Rocketmq8663To6798ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _act(self, name: str, *patches: str) -> File:
        repo = self.pr.repo
        step = (
            _PATCH_STEP.format(
                repo=repo, patches=" ".join(patches), skip_flags=_SKIP_FLAGS
            )
            if patches
            else ""
        )
        return File(
            ".",
            name,
            _ACT_SH.format(
                repo=repo,
                pl_resolver=_PL_RESOLVER_SH.format(modules=_build_pl_flag(self.pr)),
                patch_step=step,
                test_resolver=_build_test_resolver(self.pr),
                mvn_cmd=_mvn_test_command(self.pr),
            ),
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    pl_resolver=_PL_RESOLVER_SH.format(modules=_build_pl_flag(self.pr)),
                    mvn_warmup=_mvn_warmup_command(self.pr),
                ),
            ),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The sha is declared ONCE as an ARG with a literal default and then
        # referenced as ${BASE_COMMIT}, so the hardening block can be used
        # verbatim instead of string-substituted. The literal default is what
        # makes this work in the PR layer: the harness only passes BASE_COMMIT
        # as a --build-arg to a base image (dependency() returning a str), so
        # an undefaulted ARG here would expand to the empty string.
        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        scrub = Image._HARDENING_BLOCK.strip()
        # No WORKDIR here: the base image already ends at /home/<repo>.
        checkout_and_scrub = (
            f"RUN git reset --hard\n"
            f"RUN git checkout ${{BASE_COMMIT}}\n\n"
            f"{scrub}\n"
        )
        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            for line in self.global_env.splitlines():
                match = _PROXY_ENV_RE.match(line)
                if match:
                    proxy_setup = _m2_proxy_setup(match.group(2), match.group(3))
                    proxy_cleanup = _M2_PROXY_CLEANUP
                    break

        return f"""FROM {name}:{tag}

{base_commit_arg}

{self.global_env}

{proxy_setup}

{copy_commands}

{checkout_and_scrub}
{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>\S+(?::|(?=.* > )).*?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("apache", "rocketmq_8663_to_6798")
@Instance.register("apache", "rocketmq")
class ROCKETMQ_8663_TO_6798(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Rocketmq8663To6798ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
