import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Toolchain constants
# ---------------------------------------------------------------------------
# Ubuntu 22.04 ships Maven 3.6.3, which is exactly at the floor required by the
# Apache parent POM (org.apache:apache:31) and below what several plugins used
# by modern dubbo (surefire 3.5.x, compiler 3.14.x, spotless 2.46.x) are tested
# against. Pin a known-good 3.9.x instead. The Maven distribution is pure Java,
# so the tarball is architecture independent (amd64 + arm64 both work).
_MAVEN_VERSION = "3.9.9"

# dubbo switched its build/CI baseline to JDK 17 around PR #6279 (the 3.x line).
# Anything before that must still be compiled and tested on JDK 8.
_JDK17_MIN_PR = 6279

# Top-level directories that are NOT Maven reactor modules.
# Including them in -pl causes "Could not find the selected project in the
# reactor" errors, which abort the build before any test runs.
_NON_MODULE_DIRS = frozenset(
    {
        ".mvn",
        ".github",
        ".gitignore",
        ".gitattributes",
        ".git",
        "codestyle",
        ".editorconfig",
        ".licenserc.yaml",
    }
)

# Directories that are grouping parents (multi-module aggregators).
# The root pom.xml references their children directly (e.g. dubbo-plugin/dubbo-qos).
# Including e.g. "dubbo-plugin" alone in -pl builds only the parent POM — no tests
# run. We need the two-segment path (e.g. dubbo-plugin/dubbo-qos) to target the
# actual sub-module that contains source code and tests.
_GROUPING_DIRS = frozenset(
    {
        "dubbo-config",
        "dubbo-configcenter",
        "dubbo-container",
        "dubbo-demo",
        "dubbo-dependencies",
        "dubbo-distribution",
        "dubbo-filter",
        "dubbo-metadata",
        "dubbo-metadata-report",
        "dubbo-metrics",
        "dubbo-monitor",
        "dubbo-plugin",
        "dubbo-registry",
        "dubbo-remoting",
        "dubbo-rpc",
        "dubbo-serialization",
        "dubbo-simple",
        "dubbo-spring-boot",
        "dubbo-spring-boot-project",
        "dubbo-test",
    }
)


def _strip_diff_prefix(path: str) -> str:
    """Remove the leading ``a/`` or ``b/`` that ``diff --git`` puts on paths.

    Note this is a prefix strip, not ``str.lstrip("a/")`` — the latter strips
    *characters* and would mangle a path such as ``a/apache-foo/...``.
    """
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _extract_modules_from_patch(patch_text: str) -> set[str]:
    """Extract Maven module paths from a unified diff.

    For files under grouping directories (e.g. dubbo-plugin/dubbo-qos/src/...),
    returns the two-segment module path (dubbo-plugin/dubbo-qos) matching how
    the root pom.xml declares them.

    For files under direct reactor modules (e.g. dubbo-common/src/...),
    returns the single-segment name.

    Filters out non-module directories (.mvn, .github, codestyle, etc.).
    """
    modules: set[str] = set()
    for line in patch_text.split("\n"):
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
        if top in _GROUPING_DIRS:
            if len(segments) >= 3:
                modules.add(f"{segments[0]}/{segments[1]}")
            # Only 2 segments means a file sitting directly in the grouping dir
            # (e.g. dubbo-plugin/pom.xml) — not a buildable module.
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    """Scope the reactor to the modules the PR actually touches.

    dubbo has ~40 reactor modules; a full ``mvn test`` takes hours and buries
    the relevant results. ``-am`` (also-make) is required so the parent POM and
    the BOM the target module depends on are built too.
    """
    all_modules = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(
        pr.test_patch
    )
    all_modules.discard("pom.xml")
    all_modules.discard("")
    if not all_modules:
        return ""
    return "-pl " + ",".join(sorted(all_modules)) + " -am"


def _jdk_major(pr: PullRequest) -> int:
    return 17 if pr.number >= _JDK17_MIN_PR else 8


# Skips that keep the run focused on tests only:
#  * spotless (activated for JDK >= 11 and bound to process-sources) runs a
#    palantir-java-format *check* that fails the build. A test.patch / fix.patch
#    is not guaranteed to be palantir-formatted, so leaving it on would make the
#    test and fix stages fail before surefire ever runs — producing 0/0/0.
#  * checkstyle / rat / jacoco / enforcer / javadoc are not test signal.
_SKIP_FLAGS = (
    "-Dspotless.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true "
    "-Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true -Drat.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

# -B      : batch mode, no ANSI colour, stable machine-readable output
# -ntp    : no transfer progress spam (keeps the captured log parseable)
# -fn     : fail-never. Test failures are the *expected* outcome of the
#           test-patch stage; -fn makes Maven exit 0 on them while still exiting
#           non-zero when the reactor itself cannot be built. That is what lets
#           the run scripts use `set -eo pipefail` with no `|| true`.
_MVN_BASE = (
    f"mvn -B -ntp clean test -fn "
    f"-Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false "
    f"{_SKIP_FLAGS}"
)


def _mvn_test_command(pr: PullRequest) -> str:
    pl_flag = _build_pl_flag(pr)
    return f"{_MVN_BASE} {pl_flag}" if pl_flag else _MVN_BASE


def _mvn_warmup_command(pr: PullRequest) -> str:
    """Dependency/plugin warm-up for the image build: resolve + compile, no run."""
    pl_flag = _build_pl_flag(pr)
    base = f"{_MVN_BASE} -DskipTests"
    return f"{base} {pl_flag}" if pl_flag else base


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
        # Invariant: the tag must be unique per PR. This image bakes in
        # BASE_COMMIT and scrubs every other git object, while the builder
        # skips any image_full_name() that already exists — so a tag shared
        # between two PRs would serve the second PR the first PR's tree.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        jdk = _jdk_major(self.pr)

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    fontconfig \\
    git \\
    openjdk-{jdk}-jdk \\
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
    MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

{code}

{self.clear_env}

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

    def files(self) -> list[File]:
        mvn_cmd = _mvn_test_command(self.pr)
        mvn_warmup = _mvn_warmup_command(self.pr)
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

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{mvn_warmup} || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha, mvn_warmup=mvn_warmup),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
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
            # Extract proxy host and port
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
            prepare_commands,
            proxy_cleanup,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# parse_log helpers
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")

# "[INFO] Running org.apache.dubbo.common.utils.LRUCacheTest"
_RUNNING_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Running\s+(\S+)\s*$")

# "Tests run: 4, Failures: 1, Errors: 0, Skipped: 0"
_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
)

# Surefire 3.x appends the owning class: "... 0.12 s -- in com.foo.BarTest"
# (older 3.0 milestones used a single dash: "- in com.foo.BarTest")
_IN_CLASS_RE = re.compile(r"(?:--|-)\s+in\s+(\S+)\s*$")

# "[INFO] Results:" starts the aggregate section, whose "Tests run:" line is a
# module-wide total and must NOT be attributed to the last-seen class.
_RESULTS_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Results:\s*$")

_FAILURE_MARKER_RE = re.compile(r"<<<\s*(FAILURE|ERROR)!")


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
        """Parse Maven Surefire output into per-test-class results.

        Surefire only enumerates individual methods when they fail, so the
        stable unit that exists in every stage is the fully qualified test
        *class*. Class FQNs carry no timing/count metadata, so the same class
        yields an identical name in the run / test / fix stages.

        The scan is a single linear pass — no multi-line regex with nested
        quantifiers, which would backtrack badly on a multi-megabyte reactor log.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        current_class: str | None = None
        for line in clean_log.split("\n"):
            running = _RUNNING_RE.match(line.strip())
            if running:
                current_class = running.group(1)
                continue

            if _RESULTS_RE.match(line.strip()):
                # Aggregate section — nothing after this belongs to a class
                # unless Surefire re-states it via "-- in <class>".
                current_class = None
                continue

            summary = _SUMMARY_RE.search(line)
            if not summary:
                continue

            in_class = _IN_CLASS_RE.search(line)
            test_name = in_class.group(1) if in_class else current_class
            # A "Tests run:" line with neither an "-- in <class>" suffix nor a
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

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A class can legitimately show up more than once (e.g. built
        # under more than one reactor module), so resolve by severity:
        # failed > passed > skipped.
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
