import re
import shlex
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MAVEN_VERSION = "3.9.9"
_JDK17_MIN_PR = 6279


def _jdk_major(pr: PullRequest) -> int:
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
_MVN_BASE = (
    "mvn -B -ntp clean test -fn "
    "-Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false "
    "-Dmaven.compiler.failOnError=false "
    f"{_SKIP_FLAGS}"
)

# Full reactor, installed at base-commit state. Its only job is to make every
# module resolvable from ~/.m2 so the graded stages never need -am.
_MVN_WARMUP = f"mvn -B -ntp clean install -fn -DskipTests {_SKIP_FLAGS}"

# Checkout + scrub, in the PR Dockerfile rather than the base (which is shared
# and must stay unpinned) or prepare.sh. Asserts the isolation the graded run
# depends on: HEAD is the base commit and no other commit is reachable.
_HARDEN_BLOCK = """WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:+UseParallelGC"

cd /home/__REPO__
git reset --hard
bash /home/check_git_changes.sh

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


primary = load(sys.argv[1])
seen = {ident(line) for line in primary}
merged = list(primary)
for line in load(sys.argv[2]):
    key = ident(line)
    if key not in seen:
        seen.add(key)
        merged.append(line)
sys.stdout.write("".join(line + "\n" for line in merged))
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
    return f"{_MVN_BASE} $MVN_PL\n" + _emit_to(_STAGE_TESTS) + f"cat {_STAGE_TESTS}\n"


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
    body = (
        "git apply --whitespace=nowarn /home/test.patch\n\n"
        "# ---- pass A: the graded run, gold test patch applied.\n"
        f"{_MVN_BASE} $MVN_PL 2>&1 | tee {_PASS_A_LOG}\n"
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
            f"  {_MVN_BASE} $MVN_PL\n"
            # Not indented: a quoted heredoc's terminator must sit at column 0,
            # and indenting the body would break the Python inside it too.
            + _emit_to(_PASS_B_TESTS).strip()
            + "\nfi\n"
        )

    body += (
        f"\npython3 - {_PASS_A_TESTS} {_PASS_B_TESTS} <<'MERGE_TESTCASES'\n"
        + _MERGE_PY.strip()
        + "\nMERGE_TESTCASES\n"
    )
    return body


_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx2g -XX:+UseParallelGC"

cd /home/{repo}
# Written by prepare.sh at image-build time; absent means the image is broken,
# and failing here beats silently building the whole 40-module reactor.
MVN_PL="$(cat /home/mvn_pl.txt)"
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

        jdk = _jdk_major(self.pr)

        # `git -C /home clone`, not `git clone`, and deliberately so. Both
        # DockerfileEnhancer._standardize_repo_fetch (regex `^RUN git clone ...`)
        # and _inject_final_sanitize (substring "git clone") key on the plain
        # spelling; either one firing would append `git checkout ${BASE_COMMIT}`
        # and the scrub to this image, pinning a base that five PRs share and
        # putting the hardening in the wrong file. REPO_URL is the ARG the
        # enhancer declares and build_dataset passes.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    fontconfig \\
    git \\
    openjdk-{jdk}-jdk \\
    python3 \\
    tar \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/lib/jvm/java-{jdk}-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-{jdk}-openjdk

RUN wget -q https://archive.apache.org/dist/maven/maven-3/{_MAVEN_VERSION}/binaries/apache-maven-{_MAVEN_VERSION}-bin.tar.gz -O /tmp/maven.tar.gz && \\
    tar xzf /tmp/maven.tar.gz -C /opt && \\
    ln -sf /opt/apache-maven-{_MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn && \\
    rm /tmp/maven.tar.gz

ENV JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk \\
    LC_ALL=C.UTF-8 \\
    MAVEN_HOME=/opt/apache-maven-{_MAVEN_VERSION} \\
    MAVEN_OPTS="-Xmx2g -XX:+UseParallelGC"

{self.clear_env}

RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}

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
            .replace("__RESOLVER__", resolver.strip())
            .replace("__WARMUP__", _MVN_WARMUP)
        )

    def files(self) -> list[File]:
        preamble = _SCRIPT_PREAMBLE.format(repo=self.pr.repo)
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

        harden_commands = _HARDEN_BLOCK.format(
            repo=self.pr.repo, sha=self.pr.base.sha
        )

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
        sections = [f"FROM {name}:{tag}"]
        for part in (
            self.global_env,
            harden_commands,
            proxy_setup,
            copy_commands,
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
