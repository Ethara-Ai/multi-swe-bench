import re
import textwrap
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Top-level directories that are not buildable reactor modules. rocketmq's
# reactor is flat (client, common, broker, store, ...), so unlike dubbo there
# are no multi-module grouping parents to expand — a single path segment is
# always the module name.
_NON_MODULE_DIRS = frozenset(
    {
        ".github",
        ".git",
        ".gitignore",
        ".gitattributes",
        ".mvn",
        "docs",
        "style",
    }
)

# distribution/ is packaging only (conf files, shell scripts, assembly descriptor).
# It carries no test sources, and building it drags in the assembly plugin. When a
# patch touches only distribution/, there is nothing to test there — but it must not
# be the sole -pl target either, or surefire runs zero tests.
_NO_TEST_MODULES = frozenset({"distribution"})


def _strip_diff_prefix(path: str) -> str:
    """'b/broker/src/...' -> 'broker/src/...'"""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _extract_modules_from_patch(patch: str) -> set[str]:
    modules: set[str] = set()
    if not patch:
        return modules
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        segments = _strip_diff_prefix(parts[2]).split("/")
        if len(segments) < 2:
            # Root-level file (pom.xml, README.md, ...) — not a module.
            continue
        top = segments[0]
        if top in _NON_MODULE_DIRS:
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    """Scope the reactor to the modules the PR actually touches.

    rocketmq has 15-18 reactor modules; a full ``mvn test`` takes 30-60 minutes
    per stage (x3 stages) and buries the relevant results among timing-sensitive
    store/broker tests that bind ports and flake. A flake in an unrelated module
    flips a test between the test-patch and fix-patch stages and is then read as
    a genuine transition.

    ``-am`` (also-make) is required so the parent POM and the sibling modules the
    target depends on (common, remoting, store, ...) are built too.

    The list is resolved AT RUN TIME against the directories that actually exist,
    because a PR can CREATE a module: selecting it in the run act -- where it does
    not exist yet -- makes maven abort the whole reactor with

        Could not find the selected project in the reactor: <module>

    and `-fn` then swallows it, so the act scores 0/0/0 while the build reports
    success. Modules that are absent are dropped; if none remain, the reactor runs
    unscoped rather than not at all.
    """
    all_modules = _extract_modules_from_patch(
        pr.fix_patch
    ) | _extract_modules_from_patch(pr.test_patch)
    all_modules.discard("")
    # Drop packaging-only modules, but only if something testable remains.
    testable = all_modules - _NO_TEST_MODULES
    if testable:
        all_modules = testable
    if not all_modules:
        return ""
    return ",".join(sorted(all_modules))


_PL_RESOLVER_SH = """\
# Keep only the modules that exist at THIS commit -- see _build_pl_flag.
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


def _extract_test_classes(patch: str) -> list[str]:
    """Collect the simple class names of the Java test files the patch touches.

    Returns e.g. ["PlainAccessControlFlowTest"] for a patch adding
    ``acl/src/test/java/.../PlainAccessControlFlowTest.java``.
    """
    classes: set[str] = set()
    if not patch:
        return []
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = _strip_diff_prefix(parts[2])
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".java")]
        # A test patch also ships shared scaffolding -- abstract bases and helper
        # classes (rocketmq: ContainerIntegrationTestBase, TransactionListenerImpl).
        # Surefire's default includes are **/Test*, **/*Test, **/*Tests,
        # **/*TestCase (plus *IT here), so naming anything else in -Dtest either
        # matches nothing or forces a non-test class to be "run". Keep only names
        # that surefire would have selected on its own.
        if not (
            name.startswith("Test")
            or name.endswith(("Test", "Tests", "TestCase", "IT"))
        ):
            continue
        # Abstract bases end in TestBase / IntegrationTestBase and cannot run.
        if name.endswith("TestBase"):
            continue
        classes.add(name)
    return sorted(classes)


def _build_test_flag(pr: PullRequest) -> str:
    """Grade only the test classes the test patch actually adds or changes.

    Without this the graded command is a whole-module ``mvn test``, and rocketmq's
    own suites are not isolated from each other -- they mutate tracked fixture
    files under ``src/test/resources`` and leave them dirty. Concretely, in the
    acl module ``PlainAccessValidatorTest`` rewrites
    ``acl/src/test/resources/conf/plain_acl.yml`` and never restores it (verified:
    ``git status`` reports it ``M`` after that class runs alone). A later class in
    the same module then reads corrupted fixtures. For PR #3927 that made the
    gold test fail in BOTH the test and fix stages -- no f2p transition, and
    ``Report.check()`` rule 3 rejected the instance. This is an upstream test
    isolation defect, not a patch defect: surefire ``reuseForks=false`` does NOT
    help, because a fresh JVM still sees the same dirty working tree.

    Restricting ``-Dtest`` to the patch's own classes sidesteps the polluters and
    grades exactly the contract the dataset cares about: the tests the PR ships.
    Verified for #3927 -- test stage ``Failures: 1``, fix stage ``Tests run: 3,
    Failures: 0``: a real fail-to-pass transition.

    ``-Dsurefire.failIfNoSpecifiedTests=false`` keeps the reactor green in the
    modules pulled in by ``-am`` that contain none of the named classes.
    """
    classes = _extract_test_classes(pr.test_patch)
    if not classes:
        return ""
    return (
        '"-Dtest=' + ",".join(classes) + '" -Dsurefire.failIfNoSpecifiedTests=false'
    )


# Skips that keep the run focused on tests only. rocketmq binds all of these to
# the default lifecycle, and each one can fail the build *before* surefire runs:
#  * apache-rat  : license-header audit. A test.patch / fix.patch hunk is not
#                  guaranteed to carry an ASF header, so rat would abort the
#                  test and fix stages and produce 0/0/0.
#  * checkstyle  : style audit, same failure mode, no test signal.
#  * jacoco/enforcer/javadoc/clirr/versions/gpg/license: not test signal.
_SKIP_FLAGS = (
    "-Drat.skip=true -Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dclirr.skip=true -Dversions.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

# rocketmq's root pom sets <skipAfterFailureCount>1</skipAfterFailureCount>, which
# makes surefire ABORT the whole run at the first test failure. The test-patch
# stage is *expected* to fail, so with the pom default every test after the first
# failure is never reported: parse_log sees a truncated log and the f2p set becomes
# an arbitrary function of module ordering. Setting it to 0 disables the early exit
# and lets every test report its own result in all three stages.
_SUREFIRE_FLAGS = "-Dsurefire.skipAfterFailureCount=0 -Dsurefire.useFile=false"

# -B  : batch mode, no ANSI colour, stable machine-readable output
# -ntp: no transfer-progress spam (keeps the captured log parseable)
# -fn : fail-never. Test failures are the expected outcome of the test-patch
#       stage; -fn makes Maven exit 0 on them while still exiting non-zero when
#       the reactor itself cannot be built. That is what lets the run scripts use
#       `set -eo pipefail` with no `|| true` on the test command.
_MVN_BASE = (
    f"mvn -B -ntp test -fn "
    f"{_SUREFIRE_FLAGS} -Dmaven.test.skip=false -DfailIfNoTests=false "
    f"{_SKIP_FLAGS}"
)


def _mvn_test_command(pr: PullRequest) -> str:
    """The test invocation. $PL_FLAG is resolved by _PL_RESOLVER_SH at run time."""
    parts = [_MVN_BASE, "$PL_FLAG"]
    test_flag = _build_test_flag(pr)
    if test_flag:
        parts.append(test_flag)
    return " ".join(parts)


def _mvn_warmup_command(pr: PullRequest) -> str:
    """Dependency/plugin warm-up for the image build: resolve + compile, no run."""
    return f"mvn -B -ntp clean test-compile -fn -DskipTests {_SKIP_FLAGS} $PL_FLAG"


class RocketmqImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # ONE shared base for every PR of this repo. Images are deduplicated on
        # image_full_name(), so a constant tag collapses every PR onto a single
        # build of the heavy JDK + Maven + clone layer instead of one per PR.
        #
        # A shared tag is only safe because this base holds nothing
        # commit-specific: it stops at the clone, so its content is identical
        # whichever PR triggers the build. Each PR layer checks out its own
        # commit and scrubs the history itself.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    # WHY THE apt LINE LOOKS THE WAY IT DOES. This is a note for whoever edits
    # this file -- it is deliberately NOT emitted into the generated Dockerfile,
    # which ships as a deliverable.
    #
    # python3 must be the FULL package, never python3-minimal: the latter omits
    # the `xml` module that the result emitter needs. ubuntu:22.04 ships no
    # python3 at all, and without it the emitter cannot run, so every act scores
    # 0/0/0 while maven still reports BUILD SUCCESS.
    #
    # The two apt calls must not be collapsed into one. Ubuntu's `maven` declares
    # `Depends: default-jre-headless | <java7-runtime-headless>`; resolving it on
    # a JDK-less image takes the first alternative and pulls
    # openjdk-11-jre-headless ALONGSIDE openjdk-8-jdk. On arm64 the JDK 11
    # postinst then fails to configure, dpkg returns 1 and the layer aborts with
    # apt exit 100. Installing JDK 8 first registers it as a
    # java7-runtime-headless provider, so the second call satisfies maven's
    # alternative with the JDK already present. Verified on ubuntu:22.04/arm64:
    # 0 openjdk-11 packages, java 1.8.0_502, Apache Maven 3.6.3.
    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # `git -C /home clone <url> <dir>` rather than `git clone <url> /home/<dir>`.
        # The two are equivalent to git. The harness appends its hardening block
        # to any Dockerfile whose text contains the substring "git clone", which
        # would make a clone-only base impossible; this form does not match it.
        #
        # Retried five times. A clone is the one step here that depends on a
        # third party being reachable, and a transient github failure otherwise
        # aborts the whole build after the expensive JDK+Maven layer has already
        # been produced. `rm -rf` before each retry so a half-written tree is not
        # mistaken for success, and `test -d .git` asserts the loop actually
        # produced a repository rather than falling out after five failures.
        #
        # ${REPO_URL} is used rather than the literal URL: the harness supplies
        # it as a --build-arg (dependency() returns a str for the base image, so
        # build_dataset passes REPO_URL and BASE_COMMIT), and hardcoding it here
        # leaves the ARG declared but dead.
        code = f"""RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{self.pr.repo}; \\
        if git -C /home clone "${{REPO_URL}}" {self.pr.repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{self.pr.repo}/.git"""

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openjdk-8-jdk \\
    && apt-get install -y --no-install-recommends \\
    git ca-certificates curl maven python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class RocketmqImageDefault(Image):
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
        return RocketmqImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        mvn_cmd = _mvn_test_command(self.pr)
        mvn_warmup = _mvn_warmup_command(self.pr)
        pl_resolver = _PL_RESOLVER_SH.format(modules=_build_pl_flag(self.pr))
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
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "compile_guard.sh", _COMPILE_GUARD_SH),
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

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git reset --hard

git clean -fdq
# The tree is already detached at this PR's base commit: the Dockerfile checked
# it out and scrubbed the history one layer up. Assert it rather than re-doing
# it, so a wrong tree fails the build instead of being silently corrected.
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

{pl_resolver}
{mvn_warmup} || true
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    mvn_warmup=mvn_warmup,
                    pl_resolver=pl_resolver,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}

{pl_resolver}
{mvn_cmd}

# Emit one canonical line per test method from surefire's XML reports.
python3 /home/emit_results.py /home/{repo}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd, pl_resolver=pl_resolver, skip_flags=_SKIP_FLAGS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}

{pl_resolver}
bash /home/apply_patch.sh {repo} /home/test.patch
bash /home/compile_guard.sh {repo} {skip_flags}
# the guard may have dropped a module that needs the fix
. /tmp/pl_flag.env
{mvn_cmd}

# Emit one canonical line per test method from surefire's XML reports.
python3 /home/emit_results.py /home/{repo}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd, pl_resolver=pl_resolver, skip_flags=_SKIP_FLAGS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}

{pl_resolver}
bash /home/apply_patch.sh {repo} /home/test.patch /home/fix.patch
bash /home/compile_guard.sh {repo} {skip_flags}
# the guard may have dropped a module that needs the fix
. /tmp/pl_flag.env
{mvn_cmd}

# Emit one canonical line per test method from surefire's XML reports.
python3 /home/emit_results.py /home/{repo}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd, pl_resolver=pl_resolver, skip_flags=_SKIP_FLAGS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The checkout and the history scrub live in this PR layer, not the base.
        # The sha is written LITERALLY: the harness supplies BASE_COMMIT as a build
        # arg only when dependency() returns a str -- i.e. only to a base image --
        # so ${BASE_COMMIT} here would expand to the empty string and check out the
        # default branch.
        #
        # The scrub itself is DERIVED from the harness's own definition rather than
        # retyped, so it cannot drift from it or lose a step (it is two RUNs: the
        # superproject, then submodules).
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()
        checkout_and_scrub = (
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"RUN git reset --hard && git checkout {sha}\n\n"
            f"{scrub}\n"
        )
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
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{checkout_and_scrub}
{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


# Surefire writes a full XML report per test class under
# <module>/target/surefire-reports/TEST-<fqcn>.xml. Every executed method is in
# there with its outcome, whereas the console only enumerates methods when they
# FAIL -- so parsing the console can only work at class granularity and loses
# every passing method's identity.
#
# Reading the XML instead gives one line per METHOD, in the harness's canonical
# form, and lets the id carry the MODULE. That matters here: this is a
# multi-module reactor, and two modules can hold a class of the same name. A
# module-less id would merge them, and a merged id whose instances disagree is
# recorded as FAILED -- a wrong verdict for the one that passed.
_EMIT_RESULTS_PY = """\
import os, sys, xml.etree.ElementTree as ET

root = sys.argv[1] if len(sys.argv) > 1 else "."
for dirpath, _dirs, files in os.walk(root):
    if os.path.basename(dirpath) != "surefire-reports":
        continue
    # <module>/target/surefire-reports -> <module>
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
"""

_APPLY_PATCH_SH = """#!/bin/bash
# Apply patches, falling back to a three-way merge.
#
# A plain `git apply` is exact and is always tried first. It fails when the patch
# was generated against a tree that already carried an earlier merge -- two PRs in
# one dataset can share a base_commit while one was merged before the other, and
# the later patch then will not apply to that shared base.
#
# `--3way` reconstructs the merge and succeeds in that case. It prints
#     error: <path>: does not exist in index
# for every file the patch CREATES -- that is noise, not failure: the file is
# still written. Judge the outcome by the exit status and the resulting tree,
# never by counting error lines (a resolvable PR was written off that way once).
set -euo pipefail
cd /home/$1
shift
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.odg' --exclude='*.swp' --exclude='*.class')
for patch in "$@"; do
    if git apply --whitespace=nowarn "${EXCLUDES[@]}" "$patch" 2>/dev/null; then
        echo "apply_patch: $patch applied cleanly"
    else
        echo "apply_patch: $patch needs --3way"
        git apply --3way --whitespace=nowarn "${EXCLUDES[@]}" "$patch"
        echo "apply_patch: $patch applied via --3way"
    fi
done
# A three-way merge that could not resolve leaves conflict markers behind. The
# build would then fail in a way that looks like a code error, so fail here.
if grep -rqE '^<<<<<<< ' --include='*.java' .; then
    echo "apply_patch: CONFLICT MARKERS present after apply" >&2
    exit 1
fi
"""


_COMPILE_GUARD_SH = """\
# Recover the tests that a PR's own uncompilable test files would otherwise take
# down with them.
#
# In Java a test that calls a method the FIX patch introduces does not fail -- it
# does not COMPILE, and maven-compiler-plugin then fails the whole module's test
# sources, so every OTHER test in that module never runs either. Measured here:
# pr-6692's run act runs 28 tests and its test act reported 0, because two new
# test files in `broker` could not compile. Those 28 exist at base_commit and are
# untouched by the test patch -- they are exactly the p2p set.
#
# TWO RULES, both learned the hard way.
#
# 1. ONLY files under /src/test/ are ever set aside. Never a main source. On
#    pr-6692 the second thing javac named was
#        test/src/main/java/.../RMQPopClient.java
#    a helper in the integration-test module. Excluding main sources would mutate
#    the code under test and cascade into whatever depends on it; a module whose
#    MAIN sources need the fix is dropped from the reactor instead.
#
# 2. It must LOOP. javac reports the errors it reached and then the module stops,
#    so the next uncompilable file only appears on the following attempt.
#    Excluding one file and giving up leaves the module broken -- measured on
#    pr-5590, which needed three passes.
#
# This cannot invent a result. A test that cannot compile is simply absent from
# the act, which is what makes it new-to-pass once the fix lands.
set -uo pipefail
cd /home/$1
shift
MVN_SKIPS="$*"

# The act sources this afterwards; write it up front so it always exists.
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
        # Nothing left that we are allowed to touch. If a MAIN source cannot
        # compile, that module needs the fix -- drop it and keep the others.
        _bad_mods=$(sed -E 's|^/home/[^/]+/([^/]+)/.*|\\1|' /tmp/cg_main.txt | sort -u)
        if [ -n "$_bad_mods" ] && [ -n "$PL_FLAG" ]; then
            _keep=""
            for _m in $(echo "$PL_FLAG" | sed 's/-pl //; s/ -am//' | tr ',' ' '); do
                echo "$_bad_mods" | grep -qx "$_m" \
                    && echo "compile_guard:   DROPPED module '$_m' (its main sources need the fix)" \
                    || _keep="${_keep:+$_keep,}$_m"
            done
            if [ -n "$_keep" ]; then
                export PL_FLAG="-pl $_keep -am"
                # This script is a CHILD process, so an export dies with it.
                # Hand the resolved flag back through a file the act sources.
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
"""



_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")


# Surefire 3.x appends the owning class: "... 0.12 s -- in com.foo.BarTest"

# "[INFO] Results:" starts the aggregate section, whose "Tests run:" line is a

_FAILURE_MARKER_RE = re.compile(r"<<<\s*(FAILURE|ERROR)!")


@Instance.register("apache", "rocketmq")
class Rocketmq(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocketmqImageDefault(self.pr, self._config)

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
        """Read the canonical result lines emitted by emit_results.py.

            surefire:<module>/<fqcn>#<method> PASSED|FAILED|SKIPPED

        Surefire's console output only names individual methods when they fail,
        so parsing it can work no finer than the test CLASS and loses every
        passing method's identity. The XML report holds every method, so the
        emitter produces one line each and this parser does not reconstruct
        anything.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # The name must look like a test id -- `<prefix>:<...>` or something
        # containing ` > ` -- so an ordinary log line ending in the word PASSED
        # cannot be counted as a test.
        line_re = re.compile(
            r"^(?P<name>\S+(?::|(?=.* > )).*?) (?P<status>PASSED|FAILED|SKIPPED)$")
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name, status = m.group("name").strip(), m.group("status")
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # TestResult.__post_init__ requires disjoint sets: a failure outranks a
        # later retry that passed, and outranks a skip.
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
