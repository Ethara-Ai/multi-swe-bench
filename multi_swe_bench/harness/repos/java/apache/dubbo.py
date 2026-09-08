import os
import re
import shlex
import textwrap

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MAVEN_VERSION = "3.9.9"
_JDK17_MIN_PR = 6279


# PR-number thresholds are a proxy for the toolchain a commit expects, and the
# proxy breaks where the two disagree. dubbo compiles its $Adaptive extension
# classes at *runtime* through the JDK compiler API; on JDK 17 that fails with
# "Failed to compile class, cause: null", ExtensionLoader cannot build
# Protocol$Adaptive, and every test that touches ServiceConfig dies with
# NoClassDefFoundError before it runs.
#
# pr-8032 is 3.0.0-SNAPSHOT from June 2021 -- three months before JDK 17 was
# released -- yet its number puts it above _JDK17_MIN_PR. Measured on the built
# image, same patches, only JAVA_HOME changed:
#     JDK 17 -> 234 adaptive-instance errors across 21 test classes;
#               ServiceConfigTest 14 errors, gold test FAILED
#     JDK  8 -> Tests run: 14, Failures: 0, Errors: 0, Skipped: 1; BUILD SUCCESS
#
# Listed explicitly rather than by moving _JDK17_MIN_PR: the threshold is right
# for every other PR in this range, and shifting it would re-route PRs whose
# reports are already valid.
# Probed on the built images -- same patches, only JAVA_HOME changed:
#     pr-8379  43 failures / 25 adaptive errors  ->  64 run, 0 Failures, 0 Errors
#     pr-8414  75 failures / 62 adaptive errors  -> 127 run, 0 Failures, 0 Errors
#     pr-9397  43 failures / 25 adaptive errors  ->  64 run, 0 Failures, 0 Errors
#     pr-9525  38 failures / 30 adaptive errors  -> 151 run, 0 Failures, 0 Errors
#     pr-8032  79 failures                       -> 0 failures, p2p 0 -> 1397
# pr-7778 and pr-9526 sit in the same number range and show zero adaptive
# errors, so this is an enumerated set rather than a moved threshold.
_FORCE_JDK8_PRS = frozenset({8032, 8379, 8414, 9397, 9525})


def _jdk_major(pr: PullRequest) -> int:
    if pr.number in _FORCE_JDK8_PRS:
        return 8
    return 17 if pr.number >= _JDK17_MIN_PR else 8


def _strip_diff_prefix(path: str) -> str:
    # Prefix strip, not str.lstrip("a/") -- the latter strips *characters* and
    # would mangle a path such as a/apache-foo/...
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _patch_paths(patch: str) -> list[str]:
    """Repo-relative paths touched by one patch, including rename sources."""
    paths: set[str] = set()
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        for raw in parts[2:4]:
            cleaned = _strip_diff_prefix(raw)
            if cleaned and cleaned != "/dev/null":
                paths.add(cleaned)
    return sorted(paths)


def _changed_paths(pr: PullRequest) -> list[str]:
    """Repo-relative paths touched by either patch, including rename sources."""
    paths: set[str] = set()
    for patch in (pr.fix_patch, pr.test_patch):
        paths.update(_patch_paths(patch))
    return sorted(paths)



# Runs inside prepare.sh as a heredoc, so no separate file lands in the image
# build context. CHANGED is baked in at render time; the reactor is read from
# the real pom tree -- default modules plus activeByDefault profile modules,
# the same set Maven puts in the reactor for a plain `mvn test`. Each changed
# path maps to the longest module directory that prefixes it, so a
# profile-gated tree such as dubbo-test simply never matches.
_RESOLVER_PY = r'''
import os
import sys
import xml.etree.ElementTree as ET

CHANGED = __CHANGED__


def load(pom):
    root = ET.parse(pom).getroot()
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def declared_modules(pom):
    try:
        root = load(pom)
    except (ET.ParseError, OSError):
        return []
    out = []
    for mods in root.findall("modules"):
        out += [m.text.strip() for m in mods.findall("module") if m.text]
    for prof in root.iterfind("profiles/profile"):
        flag = prof.find("activation/activeByDefault")
        if flag is not None and (flag.text or "").strip().lower() == "true":
            for mods in prof.findall("modules"):
                out += [m.text.strip() for m in mods.findall("module") if m.text]
    return out


def reactor(root_dir):
    found, stack = set(), [""]
    while stack:
        rel = stack.pop()
        pom = os.path.join(root_dir, rel, "pom.xml")
        if not os.path.isfile(pom):
            continue
        for mod in declared_modules(pom):
            child = os.path.normpath(os.path.join(rel, mod))
            target = os.path.join(root_dir, child)
            if os.path.isfile(target):
                child = os.path.dirname(child)
            if not child or child in found:
                continue
            if not os.path.isdir(os.path.join(root_dir, child)):
                continue
            found.add(child)
            stack.append(child)
    return found


def main():
    root_dir = sys.argv[1]
    modules = reactor(root_dir)
    picked = set()
    for path in CHANGED:
        best = None
        for mod in modules:
            if path == mod or path.startswith(mod + "/"):
                if best is None or len(mod) > len(best):
                    best = mod
        if best:
            picked.add(best)
    sys.stdout.write(",".join(sorted(picked)))


main()
'''

# spotless runs a palantir-java-format *check* bound to process-sources on
# JDK >= 11 and fails the build; a gold patch is not guaranteed to be
# palantir-formatted, so the test and fix stages would die before surefire ran.
# checkstyle / rat / jacoco / enforcer / javadoc are not test signal.
_SKIP_FLAGS = (
    "-Dspotless.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true "
    "-Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true -Drat.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

# -fn (fail-never) makes Maven exit 0 on test failures -- the expected outcome
# of the test-patch stage -- while still exiting non-zero when the reactor
# itself cannot be built. That is what lets the run scripts keep
# `set -eo pipefail` with no `|| true` on the test command.
#
# -Dmaven.compiler.failOnError=false is what keeps the test stage from
# collapsing to 0/0/0. A gold test that references a symbol the fix patch
# introduces cannot compile before the fix (#1856's test imports io.swagger.*
# and DubboSwaggerApiListingResource, both added by the fix), and javac fails
# the whole module's test-compile -- so Surefire never ran and the module's
# other, unrelated test classes went missing too. With failOnError off, javac
# reports the errors, emits classes for the files that did compile, and the
# test phase still runs: the pre-existing tests report normally and only the
# gold test is absent, which is exactly the NONE the n2p classification wants.
# It cannot hide a broken fix -- if the gold test fails to compile in the fix
# stage it produces no !PASS -> PASS transition and Report.check rejects the
# instance.
# Tests that need a live ZooKeeper on localhost:2181. The container is offline
# by design, so each one burns curator's 30s connect timeout and two hang
# outright -- pr-10730's run stage produced a 43 MB log that was 50% connection
# retries and never finished. Measured from the stage logs: every class here
# either took >=25s or never reported a result.
#
# Scoped per PR, deliberately NOT global: ReferenceConfigTest is on this list
# and is a *graded* test for pr-3639 (its test patch modifies it). Excluding it
# everywhere would silently delete that instance's signal.
_ZK_DEPENDENT_TESTS = (
    "ConfigCenterBeanTest",
    "ConfigCenterConfigTest",
    "DubboBootstrapTest",
    "DubboConfigBeanInitializerTest",
    "Issue6000Test",
    "Issue6252Test",
    "Issue7003Test",
    "LocalCallMultipleReferenceAnnotationsTest",
    "MultiInstanceTest",
    "MultipleConsumerAndProviderTest",
    "MultipleRegistryCenterExportMetadataIntegrationTest",
    "MultipleRegistryCenterExportProviderIntegrationTest",
    "MultipleRegistryCenterInjvmIntegrationTest",
    "MultipleRegistryCenterServiceDiscoveryRegistryIntegrationTest",
    "ReferenceConfigTest",
    "SingleRegistryCenterExportMetadataIntegrationTest",
    "SingleRegistryCenterExportProviderIntegrationTest",
    "SingleRegistryCenterInjvmIntegrationTest",
    "SpringBootConfigPropsTest",
    "SpringBootImportDubboXmlTest",
    "SpringBootMultipleConfigPropsTest",
)

# PRs whose reactor selection pulls the classes above in. None of these PRs
# grades any of them -- verified against their test patches.
_ZK_AFFECTED_PRS = frozenset({10683, 10730})


def _mvn_excludes(pr: PullRequest) -> str:
    """Surefire negations for this PR, or "" when none apply.

    Emitted into the script preamble as a shell variable rather than baked into
    the command, so the graded command string stays byte-identical across
    run.sh / test-run.sh / fix-run.sh by construction.
    """
    if pr.number not in _ZK_AFFECTED_PRS:
        return ""
    negated = ",".join("!" + name for name in _ZK_DEPENDENT_TESTS)
    return f"-Dtest={negated} -Dsurefire.failIfNoSpecifiedTests=false"


_MVN_BASE = (
    "mvn -B -ntp clean test -fn "
    "-Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false "
    "-Dmaven.compiler.failOnError=false "
    f"{_SKIP_FLAGS}"
)

# Full reactor, installed at base-commit state. Its only job is to make every
# module resolvable from ~/.m2 so the graded stages never need -am.
_MVN_WARMUP = f"mvn -B -ntp clean install -fn -DskipTests {_SKIP_FLAGS}"

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export LC_ALL=C.UTF-8
export MAVEN_HOME=/opt/apache-maven-__MVNVER__
export MAVEN_OPTS="-Xmx2g -XX:+UseParallelGC"
# Must match the graded stages' JDK or the warmup populates ~/.m2 from a
# different compiler than the one the tests then run under.
export JAVA_HOME=__JAVA_HOME__
export PATH="$JAVA_HOME/bin:$MAVEN_HOME/bin:$PATH"

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh

git checkout __SHA__
bash /home/check_git_changes.sh

# ---- toolchain. Deliberately not in the base image: the base is shared by
# every PR and the standard stops it at the clone, so the JDK symlinks and the
# Maven install live here. Install-chain lines, hence the suppressed exit
# status; the hard verification below is what actually proves they worked.
ln -sfn /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk || true
ln -sfn /usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-17-openjdk || true
wget -q https://archive.apache.org/dist/maven/maven-3/__MVNVER__/binaries/apache-maven-__MVNVER__-bin.tar.gz -O /tmp/maven.tar.gz || true
tar xzf /tmp/maven.tar.gz -C /opt || true
ln -sf /opt/apache-maven-__MVNVER__/bin/mvn /usr/local/bin/mvn || true
rm -f /tmp/maven.tar.gz || true

PL_MODULES="$(python3 - /home/__REPO__ <<'RESOLVE_MODULES'
__RESOLVER__
RESOLVE_MODULES
)"
if [ -n "$PL_MODULES" ]; then
  printf -- '-pl %s' "$PL_MODULES" > /home/mvn_pl.txt
else
  : > /home/mvn_pl.txt
fi
echo "reactor selection: $(cat /home/mvn_pl.txt)"

# Full reactor, installed at base-commit state, so the graded stages resolve
# every module they do not build themselves from ~/.m2 and never need -am.
__WARMUP__ || true

# ---- HARD VERIFICATION. No error suppression below this line, by design.
# The warmup above suppresses its exit status because a partial reactor is
# survivable -- which also means a warmup that 403s, OOMs or resolves nothing
# still exits 0 and ships a hollow image: builds green, reports 0/0/0 at test
# time. These lines are the only thing standing between that and delivery, so
# every one of them must fail loud.
java -version
mvn -v
test -d "$HOME/.m2/repository/org/apache/dubbo"
test -n "$(find "$HOME/.m2/repository/org/apache/dubbo" -name '*.jar' -print -quit)"

# Compile the exact reactor selection the graded stages will run. Proves the
# toolchain, the plugins and every test-scope dependency actually resolved --
# a check `dependency:resolve` alone would not make.
mvn -B -ntp clean test-compile $(cat /home/mvn_pl.txt) __SKIPFLAGS__

bash /home/check_git_changes.sh
"""

# Surefire names individual methods on the console only when they fail, so a
# console-only parse can key on the test class at best. That loses the gold
# signal whenever a test patch adds methods to an *existing* class (#2603 adds
# MethodConfigTest#testStaticConstructor): the class reads PASS -> NONE -> PASS
# and lands in p2p with f2p and n2p both empty. The XML reports Surefire always
# writes carry every method with its status, so each stage replays them as
# compact `TESTCASE <status> <class>#<method>` lines. That is the identifier
# shape report.py already resolves -- _file_hosts_test maps the class half to
# MethodConfigTest.java and _candidate_identifiers pulls the method half out for
# the added-lines match.
_EMIT_PY = r'''
import glob
import sys
import xml.etree.ElementTree as ET

lines = []
for path in glob.glob("**/target/surefire-reports/TEST-*.xml", recursive=True):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        continue
    for tc in root.iter("testcase"):
        cls = (tc.get("classname") or "").strip()
        name = (tc.get("name") or "").strip()
        if not cls or not name:
            continue
        status = "PASSED"
        for child in tc:
            if child.tag in ("failure", "error"):
                status = "FAILED"
                break
            if child.tag == "skipped":
                status = "SKIPPED"
        lines.append("TESTCASE %s %s#%s" % (status, cls, name))

with open(sys.argv[1], "w") as fh:
    fh.write("".join(line + "\n" for line in lines))
'''


def _emit_to(target: str) -> str:
    """Harvest this stage's Surefire XML into the file `target`.

    Always a real file, never /dev/stdout: opening /dev/stdout for writing
    truncates the target when stdout happens to be a regular file, which would
    silently eat the Maven output already written to the stage log.
    """
    return (
        "\npython3 - "
        + target
        + " <<'EMIT_TESTCASES'\n"
        + _EMIT_PY.strip()
        + "\nEMIT_TESTCASES\n"
    )


# Pass A wins any identifier it reported; pass B only fills in identifiers pass
# A never emitted. That ordering is what protects the graded signal: a gold test
# that compiled and FAILED under test.patch keeps its FAIL (and its f2p credit)
# even though pass B, running without the patch, would not report it at all.
_MERGE_PY = r'''
import os
import sys


def load(path):
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return [line.rstrip("\n") for line in fh if line.strip()]


def ident(line):
    parts = line.split(None, 2)
    return parts[2] if len(parts) > 2 else None


def simple_class(key):
    return key.split("#", 1)[0].rsplit(".", 1)[-1] if key else ""


# Classes the gold test patch touches. Pass A is the graded observation for
# these and must never be overwritten -- that is the f2p/n2p signal itself.
graded = {c for c in sys.argv[3].split(",") if c} if len(sys.argv) > 3 else set()

pass_a = load(sys.argv[1])
pass_b = load(sys.argv[2])

merged = {}
order = []
for line in pass_a:
    key = ident(line)
    if key is None:
        continue
    if key not in merged:
        order.append(key)
    merged[key] = line

# Pass B is a clean re-run at base-commit state with the gold test patch
# reverted, so it is the *correct* observation for every class the patch does
# not touch. Pass A's view of those is contaminated: when the gold test fails
# to compile, javac stops emitting classes partway through the module and the
# module's other tests then die on NoClassDefFoundError for helpers that were
# never built. Measured on pr-10730: ExtensionLoaderTest reported 39 FAILED / 9
# PASSED in pass A and 48 PASSED in pass B and in the fix stage -- keeping pass
# A produced 128 bogus FAIL -> PASS transitions, none of them in a graded class.
#
# This overwrites rather than only filling gaps. Nothing is invented: every
# line written here was observed by a real Surefire run.
for line in pass_b:
    key = ident(line)
    if key is None or simple_class(key) in graded:
        continue
    if key not in merged:
        order.append(key)
    merged[key] = line

sys.stdout.write("".join(merged[k] + "\n" for k in order))
'''

_STAGE_TESTS = "/home/stage.tests"
_PASS_A_LOG = "/home/test-pass-a.log"
_PASS_A_TESTS = "/home/test-pass-a.tests"
_PASS_B_TESTS = "/home/test-pass-b.tests"

# A javac diagnostic under src/test is the signal that the gold test patch did
# not compile: "[ERROR] /home/dubbo/.../src/test/java/.../FooTest.java:[20,42]
# error: cannot find symbol". Narrowed to src/test on purpose -- a main-source
# compile error is not something reverting the test patch can repair, so paying
# for a second reactor pass there would be pure waste.
_COMPILE_ERROR_RE = r'^\[ERROR\] .*/src/test/.*\.java:\[[0-9]+,[0-9]+\]'


def _graded_body() -> str:
    """Single-pass stage: run, harvest, print. Used by run.sh and fix-run.sh."""
    return (
        f"{_MVN_BASE} $MVN_PL $MVN_EXCLUDES\n"
        + _emit_to(_STAGE_TESTS)
        + f"cat {_STAGE_TESTS}\n"
    )


def _test_stage_body(pr: PullRequest) -> str:
    """Two-pass test stage.

    Pass A is the graded run and is exactly what the single-pass body did:
    test.patch applied, `mvn test`, harvest the XML.

    The problem it papers over is that the gold test usually *cannot* compile at
    this point -- it references a symbol the fix patch introduces, which is the
    whole point of the stage. `-Dmaven.compiler.failOnError=false` keeps the
    build alive, but javac still aborts code generation partway through the
    module, so it writes class files for only an arbitrary prefix of the
    module's *other* test classes and Surefire silently runs none of the rest.
    Measured on this repo: pr-2603 lost all 23 test classes of dubbo-config-api
    to 3 uncompilable files, and pr-3578 lost 46 of 92 to 2. Those bystanders
    reach report.py as (run PASS, test NONE, fix PASS) and survive only because
    Report.check infers them back to p2p via reclassified_from_target.

    Pass B recovers them by observation instead of inference. It restores every
    path test.patch touched to its base-commit state -- reverting modified
    files, deleting added ones -- and re-runs the same reactor selection. The
    module now compiles, the bystanders run and report normally, and the gold
    tests are absent exactly as they should be.

    Pass B is skipped whenever nothing under src/test failed to compile, so a
    test stage that was already clean costs precisely what it costs today.
    """
    paths = _patch_paths(pr.test_patch)
    graded_classes = ",".join(
        sorted(
            {
                os.path.basename(path)[: -len(".java")]
                for path in paths
                if path.endswith(".java")
            }
        )
    )
    body = (
        "git apply --whitespace=nowarn /home/test.patch\n\n"
        "# ---- pass A: the graded run, gold test patch applied.\n"
        f"{_MVN_BASE} $MVN_PL $MVN_EXCLUDES 2>&1 | tee {_PASS_A_LOG}\n"
        + _emit_to(_PASS_A_TESTS)
    )

    if paths:
        quoted = " ".join(shlex.quote(p) for p in paths)
        body += (
            "\n# ---- pass B: recover the bystanders javac took down with the\n"
            "# gold test. Only runs when the gold test actually failed to compile.\n"
            f"if grep -qE {shlex.quote(_COMPILE_ERROR_RE)} {_PASS_A_LOG}; then\n"
            '  echo "test stage: gold test patch did not compile; running recovery pass"\n'
            f"  for path in {quoted}; do\n"
            '    if git cat-file -e "HEAD:$path" 2>/dev/null; then\n'
            '      git checkout HEAD -- "$path"\n'
            "    else\n"
            '      rm -f "$path"\n'
            "    fi\n"
            "  done\n"
            f"  {_MVN_BASE} $MVN_PL $MVN_EXCLUDES\n"
            # Not indented: a quoted heredoc's terminator must sit at column 0,
            # and indenting the body would break the Python inside it too.
            + _emit_to(_PASS_B_TESTS).strip()
            + "\nfi\n"
        )

    body += (
        f"\npython3 - {_PASS_A_TESTS} {_PASS_B_TESTS} {shlex.quote(graded_classes)}"
        + " <<'MERGE_TESTCASES'\n"
        + _MERGE_PY.strip()
        + "\nMERGE_TESTCASES\n"
    )
    return body


_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export LC_ALL=C.UTF-8
export MAVEN_OPTS="-Xmx2g -XX:+UseParallelGC"
# The base image ships both JDKs; the era this PR belongs to picks one. The
# toolchain itself is installed by prepare.sh at image-build time, not by the
# base, so these paths exist in the PR layer.
export JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/{repo}
# Written by prepare.sh at image-build time; absent means the image is broken,
# and failing here beats silently building the whole 40-module reactor.
MVN_PL="$(cat /home/mvn_pl.txt)"
# Surefire negations for suites that need a live ZooKeeper. Empty for PRs whose
# reactor selection does not pull them in. See _mvn_excludes().
MVN_EXCLUDES="{excludes}"
"""


class DubboImageBase(Image):
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
        # One base for the whole repo config: the image holds only the
        # toolchain and an unpinned clone, so nothing in it is PR-specific.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The standard requires a base that stops at the clone: no checkout, no
        # history stripping, nothing between `RUN git clone` and CMD.
        #
        # DockerfileEnhancer.enhance() would break that. It force-appends
        # Image._HARDENING_BLOCK to any base containing the substring
        # "git clone" (_inject_final_sanitize), and the only escapes are its
        # sentinel marker or a non-standard clone spelling -- both of which put
        # something in the base that does not belong there.
        #
        # enhance() returns the Dockerfile untouched when it already carries the
        # syntax directive:
        #     if cls.SYNTAX_DIRECTIVE in raw:
        #         return raw
        # so this method emits the directive itself and takes responsibility for
        # the infrastructure block. That block is *generated by the harness*,
        # never transcribed, so the proxy ARGs, CA symlink farm, OCI labels and
        # the REPO_URL / BASE_COMMIT ARGs cannot drift from what the pipeline
        # expects. build_dataset.py passes REPO_URL and BASE_COMMIT as build
        # args to every base image (isinstance(dependency(), str)), which is why
        # both must remain declared.
        #
        # Hardening lives in the PR Dockerfile, after prepare.sh.
        infra = DockerfileEnhancer._infrastructure_block(self, image_name, True)

        # No `_jdk_major(self.pr)` here on purpose. image_tag() is the constant
        # "base", so build_dataset.py builds this image exactly once (it skips
        # any image whose full name already exists, build_dataset.py:610) and
        # whichever PR won the race would otherwise bake *its* JDK into an image
        # the whole dataset shares. This dataset spans _JDK17_MIN_PR -- pr-3639
        # needs JDK 8 (42/42 pass; JDK 17 fails the same suite) while
        # 7778..10730 need JDK 17 -- so both are installed and the PR layer
        # selects one via JAVA_HOME. That keeps the image PR-independent as its
        # tag claims, and preserves one repo config == one base Dockerfile.
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    fontconfig \\
    git \\
    openjdk-8-jdk \\
    openjdk-17-jdk \\
    python3 \\
    tar \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class DubboImageDefault(Image):
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
        return DubboImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        resolver = _RESOLVER_PY.replace(
            "__CHANGED__", repr(_changed_paths(self.pr))
        )
        return (
            _PREPARE_SH.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__SKIPFLAGS__", _SKIP_FLAGS)
            .replace("__MVNVER__", _MAVEN_VERSION)
            .replace(
                "__JAVA_HOME__",
                f"/usr/lib/jvm/java-{_jdk_major(self.pr)}-openjdk",
            )
            .replace("__RESOLVER__", resolver.strip())
            .replace("__WARMUP__", _MVN_WARMUP)
        )

    def files(self) -> list[File]:
        preamble = _SCRIPT_PREAMBLE.format(
            repo=self.pr.repo,
            jdk=_jdk_major(self.pr),
            excludes=_mvn_excludes(self.pr),
        )
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
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", preamble + _graded_body()),
            File(".", "test-run.sh", preamble + _test_stage_body(self.pr)),
            File(
                ".",
                "fix-run.sh",
                preamble
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + _graded_body(),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Referenced from the harness, never pasted, so the four integrity
        # asserts cannot drift from what the pipeline expects. It expands
        # ${BASE_COMMIT}, which build_dataset.py passes only to base images
        # (isinstance(dependency(), str)) -- a PR layer receives no build args,
        # so the ARG below carries a literal default.
        # Sourced from the harness, never transcribed, so the four integrity
        # asserts cannot drift. ${BASE_COMMIT} is interpolated to the literal SHA
        # because build_dataset.py passes build args only to base images
        # (isinstance(dependency(), str)) -- a PR layer receives none, so the
        # variable would expand to empty and every assert would pass vacuously.
        harden_commands = (
            "# Git stripping / hardening. Pins the tree to the base commit and\n"
            "# reduces the repository to exactly that history, then asserts the\n"
            "# four invariants: HEAD == base commit, no residual refs, no remotes,\n"
            "# no unreachable objects.\n"
            + Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)
        )

        # The base's last WORKDIR is /home/ (the standard stops the base at the
        # clone), so the hardening RUN needs the repo root set here. prepare.sh
        # cds for itself and does not rely on it.
        harden_workdir = f"WORKDIR /home/{self.pr.repo}"
        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        # Maven does not read http_proxy/https_proxy; it needs ~/.m2/settings.xml.
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
        # Order is fixed by the standard: FROM, ARG, COPYs, prepare.sh, and
        # only then the hardening. prepare.sh does the checkout and installs at
        # that commit; stripping history before it ran would leave nothing to
        # check out.
        # Order per the standard: FROM, the seven COPYs, WORKDIR, the hardening
        # and submodule scrub, then prepare.sh. No CMD -- it is inherited from
        # the base and never consulted anyway: docker_util.run() passes the
        # command explicitly to containers.run(image=..., command=...).
        sections = [f"FROM {name}:{tag}"]
        for part in (
            self.global_env,
            proxy_setup,
            copy_commands,
            harden_workdir,
            harden_commands,
            prepare_commands,
            proxy_cleanup,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")

# "Running org.apache.dubbo.common.utils.LRUCacheTest" (2.19.x, unprefixed) and
# "[INFO] Running ..." (2.22.x).
_RUNNING_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Running\s+(\S+)\s*$")

_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
)

# "... 0.12 s - in com.foo.BarTest"; some 3.0 milestones use a double dash.
_IN_CLASS_RE = re.compile(r"(?:--|-)\s+in\s+(\S+)\s*$")

# Starts the aggregate section, whose "Tests run:" line is a module-wide total.
_RESULTS_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Results\s*:\s*$")

_FAILURE_MARKER_RE = re.compile(r"<<<\s*(FAILURE|ERROR)!")

# "TESTCASE PASSED com.alibaba.dubbo.config.MethodConfigTest#testStaticConstructor"
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _disjoint(
    passed: set[str], failed: set[str], skipped: set[str]
) -> TestResult:
    """TestResult.__post_init__ requires the three sets pairwise disjoint.

    A test can legitimately be reported twice -- retried, or built under more
    than one reactor module -- so resolve by severity: failed > passed > skipped.
    """
    passed = passed - failed
    skipped = skipped - failed - passed
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("apache", "dubbo")
class Dubbo(Instance):
    """Registered on the bare org/repo key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DubboImageDefault(self.pr, self._config)

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
        """Parse Surefire output into per-test-class results.

        Single linear pass -- no multi-line regex with nested quantifiers, which
        would backtrack badly on a multi-megabyte reactor log.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        for line in clean_log.split("\n"):
            case = _TESTCASE_RE.match(line)
            if not case:
                continue
            status, name = case.group(1), case.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        # Method-level names win outright. Falling back per-stage would mix two
        # naming schemes, and a class that is a name in one stage and a set of
        # method names in another reads as an all-NONE test in both.
        if passed_tests or failed_tests or skipped_tests:
            return _disjoint(passed_tests, failed_tests, skipped_tests)

        # No XML: the module never compiled, or an older image. Fall back to the
        # console summaries, keyed on the test class.
        current_class: str | None = None
        for line in clean_log.split("\n"):
            stripped = line.strip()

            running = _RUNNING_RE.match(stripped)
            if running:
                current_class = running.group(1)
                continue

            if _RESULTS_RE.match(stripped):
                current_class = None
                continue

            summary = _SUMMARY_RE.search(line)
            if not summary:
                continue

            in_class = _IN_CLASS_RE.search(line)
            test_name = in_class.group(1) if in_class else current_class
            # A "Tests run:" line with neither an "in <class>" suffix nor a
            # preceding "Running <class>" is a module/reactor total. Ignore it.
            current_class = None
            if not test_name:
                continue

            tests_run = int(summary.group(1))
            failures = int(summary.group(2))
            errors = int(summary.group(3))
            skipped = int(summary.group(4))

            if failures > 0 or errors > 0 or _FAILURE_MARKER_RE.search(line):
                failed_tests.add(test_name)
            elif tests_run == 0:
                continue
            elif skipped == tests_run:
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

        return _disjoint(passed_tests, failed_tests, skipped_tests)
