import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Top-level directories that are NOT Maven reactor modules.
# Including them in -pl causes "Could not find the selected project in the reactor" errors.
_NON_MODULE_DIRS = frozenset({
    ".mvn", ".github", ".gitignore", ".gitattributes", ".git",
    "codestyle", "dev", "docs", "licenses", "publications",
    "website", "hooks", ".editorconfig", ".licenserc.yaml",
})

# Directories that are grouping parents (multi-module aggregators).
# The root pom.xml references their children directly (e.g. extensions-core/hdfs-storage).
# Including e.g. "extensions-core" alone in -pl builds only the parent POM — no tests run.
# We need the two-segment path (e.g. extensions-core/hdfs-storage) to target the
# actual sub-module that contains source code and tests.
_GROUPING_DIRS = frozenset({
    "cloud",
    "extensions",
    "extensions-contrib",
    "extensions-core",
})


def _extract_modules_from_patch(patch_text: str) -> set[str]:
    """Extract Maven module paths from a unified diff.

    For files under grouping directories (e.g. extensions-core/hdfs-storage/src/...),
    returns the two-segment module path (extensions-core/hdfs-storage) matching how
    the root pom.xml declares them.

    For files under direct reactor modules (e.g. processing/src/...),
    returns the single-segment name.

    Filters out non-module directories (.github, docs, codestyle, etc.).
    """
    modules = set()
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                path = parts[2].lstrip("a/")
                segments = path.split("/")
                if len(segments) < 2:
                    continue
                top = segments[0]
                # Skip non-module dirs
                if top in _NON_MODULE_DIRS:
                    continue
                # For grouping dirs, use two-segment path (e.g. extensions-core/hdfs-storage)
                if top in _GROUPING_DIRS:
                    if len(segments) >= 3:
                        modules.add(f"{segments[0]}/{segments[1]}")
                    # If only 2 segments, it's a file directly in the grouping dir
                    # (e.g. extensions-core/pom.xml) — skip, not a buildable module
                    continue
                modules.add(top)
    return modules


def _build_pl_flag(pr) -> str:
    all_modules = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(pr.test_patch)
    # Filter out root-level files that aren't modules
    all_modules.discard("pom.xml")
    all_modules.discard("")
    if not all_modules:
        return ""
    return "-pl " + ",".join(sorted(all_modules)) + " -am"


def _extract_test_classes_from_patch(patch_text: str) -> set[str]:
    """Extract JUnit test class names from a unified diff.

    Returns bare class names (e.g. HdfsDataSegmentKillerTest) suitable for
    surefire -Dtest=, taken from any *.java file under a src/test/ tree.
    """
    classes = set()
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                if "/src/test/" not in path or not path.endswith(".java"):
                    continue
                classes.add(path.rsplit("/", 1)[-1][: -len(".java")])
    return classes


def _build_test_flag(pr) -> str:
    """Scope surefire to only the test classes this PR actually touches.

    Druid ships ~1704 test classes across the modules a typical patch pulls in
    via -am, and running all of them takes hours per pass -- four passes and two
    architectures makes a full-scope multi-arch build take days. Measured on this
    repo: compiling 2506 main + 1166 test sources takes ~1.7 min, while the tests
    take 20+ min for just 66 of 1066 classes in one module. Compilation is
    therefore cheap and is deliberately left at full scope (every module still
    compiles, so a patch that breaks compilation elsewhere is still caught);
    only test EXECUTION is narrowed.

    Trade-off, stated plainly: this shrinks the p2p baseline to the touched test
    classes. It does not affect f2p/n2p, which come from exactly these classes.
    Pairs with -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false so modules holding none of them do not fail.
    """
    classes = _extract_test_classes_from_patch(pr.test_patch)
    if not classes:
        return ""
    # Append "*" to each name so surefire also matches NESTED static test
    # classes. Verified on this PR: ParallelIndexSupervisorTaskTest is an outer
    # container with ZERO @Test methods of its own -- every test lives in a
    # nested static class (CreateMergeIoConfigsTest, ConstructorTest,
    # StaticUtilsTest), compiled as Outer$Nested.class. A bare -Dtest=Outer
    # matched only the empty outer class, so that file contributed 0 tests at
    # every stage. "Outer*" matches the outer AND its nested classes.
    return "-Dtest=" + ",".join(f"{c}*" for c in sorted(classes))


class DruidJdk8ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # Per-PR, NOT a shared per-JDK tag. The injected hardening block in the
        # rendered base Dockerfile detaches at one ${BASE_COMMIT}, deletes every
        # other ref and gc-prunes unreachable objects, then asserts
        # rev-list --all == rev-list HEAD. A tag shared across an era would
        # therefore be permanently pinned to whichever PR built it FIRST, and a
        # second PR reusing it would find its own base commit already pruned --
        # its prepare.sh checkout would fail, or silently test the wrong tree.
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

        return f"""FROM {image_name}

{self.global_env}

# DEBIAN_FRONTEND and LANG are deliberately NOT set here. DockerfileEnhancer._ENV_BLOCK
# already sets both (plus TZ and the proxy/CA vars) earlier in every rendered Dockerfile,
# so repeating them produced a literal duplicate that this project Dockerfile QC flags.
# LC_ALL is KEPT: the enhancer does not supply it and the JVM needs it for locale-stable
# test output.
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-8-jdk maven

RUN ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk
ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk

{code}

{self.clear_env}

"""


class DruidJdk8ImageDefault(Image):
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
        return DruidJdk8ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pl_flag = _build_pl_flag(self.pr)
        # Scope surefire to the test classes this PR touches (see _build_test_flag).
        # Applied IDENTICALLY to prepare/run/test-run/fix-run so the three graded
        # stages stay comparable and only the applied patch differs between them.
        test_flag = _build_test_flag(self.pr)
        suffix = " ".join(x for x in (pl_flag, test_flag) if x)
        # prepare.sh uses "clean" to do a full build during Docker image creation
        mvn_prepare_base = "mvn clean test -fn -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        mvn_prepare_cmd = f"{mvn_prepare_base} {suffix}" if suffix else mvn_prepare_base
        # run/test-run/fix-run scripts reuse pre-built artifacts — no "clean"
        mvn_run_base = "mvn test -o -fn -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        mvn_run_cmd = f"{mvn_run_base} {suffix}" if suffix else mvn_run_base
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{mvn_cmd} || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha, mvn_cmd=mvn_prepare_cmd),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{mvn_cmd} || true

# Dump surefire XML reports to stdout. Surefire console output only reports
# per-CLASS summaries, but this PR adds new @Test METHODS to test classes that
# already exist, so at class granularity the class passes both before and after
# the fix -> classified p2p, and the new methods are invisible (f2p/n2p empty).
# The XML reports are surefire own authoritative per-method record; emitting
# them lets parse_log classify each test method individually. Same line in all
# three graded scripts, so the stages stay comparable.
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true
""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{mvn_cmd} || true

# Dump surefire XML reports to stdout -- see run.sh for why (method granularity).
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true

""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{mvn_cmd} || true

# Dump surefire XML reports to stdout -- see run.sh for why (method granularity).
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true

""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
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

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("apache", "druid_0_to_16976")
class DruidJdk8(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DruidJdk8ImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # --- Preferred path: surefire XML (METHOD granularity) ---
        # The graded scripts cat target/surefire-reports/TEST-*.xml to stdout.
        # Console output is per-CLASS only, which cannot represent a PR that adds
        # new @Test METHODS to a test class that already exists: the class passes
        # both before and after the fix, so it lands in p2p and f2p/n2p come out
        # EMPTY (validator Rule 4 failure). Parsing the XML gives one entry per
        # test method, so a method that only exists after fix.patch is correctly
        # classified. Falls back to the console parser below when no XML is
        # present (e.g. the build failed before surefire ran).
        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for m in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(m.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True
            name = f"{cls}.{meth}" if cls else meth
            body = m.group(3) or ""
            if "<failure" in body or "<error" in body:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif "<skipped" in body:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        if saw_xml:
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )


        # Surefire 3.x: "[INFO] Tests run: 5, ... Time elapsed: 1.23 s -- in com.foo.BarTest"
        # Surefire 2.x: "Tests run: 5, ... Time elapsed: 0.203 sec" (no class name suffix)
        # Use "Running <class>" line to capture test name, then match "Tests run:" line below.
        #
        # The skip-zone between "Running X" and its "Tests run:" line must stop at
        # the next "Running Y" line AND at Maven's "Results:" aggregate-summary
        # header -- otherwise a class whose own fork crashes (OOM, JVM died) before
        # printing its per-class "Tests run:" line gets silently paired with a
        # LATER, unrelated "Tests run:" line (the next class's, or the whole
        # build's final aggregate), which falsely reports a crashed class as
        # passed. Verified: without the two extra negative lookaheads, a
        # class that OOMs right after "Running BetaTest" with no other test
        # classes following it gets matched against the trailing aggregate
        # "Tests run: 3, Failures: 0, ..." and reported PASSED even though it
        # never actually ran.
        re_pass_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            )
        ]
        re_fail_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*<<<\s*FAILURE!"
            )
        ]

        for re_fail_test in re_fail_tests:
            for m in re_fail_test.finditer(test_log):
                failed_tests.add(m.group(1))

        for re_pass_test in re_pass_tests:
            for m in re_pass_test.finditer(test_log):
                test_name = m.group(1)
                if test_name in failed_tests:
                    continue
                tests_run = int(m.group(2))
                failures = int(m.group(3))
                errors = int(m.group(4))
                skipped = int(m.group(5))
                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
