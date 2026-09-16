import os
import re
import shlex
import textwrap

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MAVEN_VERSION = "3.9.9"
_JDK17_MIN_PR = 6279


_FORCE_JDK8_PRS = frozenset({8032, 8379, 8414, 9397, 9525})


def _jdk_major(pr: PullRequest) -> int:
    if pr.number in _FORCE_JDK8_PRS:
        return 8
    return 17 if pr.number >= _JDK17_MIN_PR else 8


def _strip_diff_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _patch_paths(patch: str) -> list[str]:
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
    paths: set[str] = set()
    for patch in (pr.fix_patch, pr.test_patch):
        paths.update(_patch_paths(patch))
    return sorted(paths)


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

_SKIP_FLAGS = (
    "-Dspotless.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true "
    "-Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true -Drat.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

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

_ZK_AFFECTED_PRS = frozenset({10683, 10730})


def _mvn_excludes(pr: PullRequest) -> str:
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
    return (
        "\npython3 - "
        + target
        + " <<'EMIT_TESTCASES'\n"
        + _EMIT_PY.strip()
        + "\nEMIT_TESTCASES\n"
    )


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

_COMPILE_ERROR_RE = r'^\[ERROR\] .*/src/test/.*\.java:\[[0-9]+,[0-9]+\]'


def _graded_body() -> str:
    return (
        f"{_MVN_BASE} $MVN_PL $MVN_EXCLUDES\n"
        + _emit_to(_STAGE_TESTS)
        + f"cat {_STAGE_TESTS}\n"
    )


def _test_stage_body(pr: PullRequest) -> str:
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        infra = DockerfileEnhancer._infrastructure_block(self, image_name, True)

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

        harden_commands = (
            "# Git stripping / hardening. Pins the tree to the base commit and\n"
            "# reduces the repository to exactly that history, then asserts the\n"
            "# four invariants: HEAD == base commit, no residual refs, no remotes,\n"
            "# no unreachable objects.\n"
            + Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)
        )

        harden_workdir = f"WORKDIR /home/{self.pr.repo}"
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

_RUNNING_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Running\s+(\S+)\s*$")

_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
)

_IN_CLASS_RE = re.compile(r"(?:--|-)\s+in\s+(\S+)\s*$")

_RESULTS_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Results\s*:\s*$")

_FAILURE_MARKER_RE = re.compile(r"<<<\s*(FAILURE|ERROR)!")

_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _disjoint(
    passed: set[str], failed: set[str], skipped: set[str]
) -> TestResult:
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

        if passed_tests or failed_tests or skipped_tests:
            return _disjoint(passed_tests, failed_tests, skipped_tests)

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
