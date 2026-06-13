import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# binarywang/WxJava — the WeChat/Weixin Java SDK, a Maven multi-module project.
#
# Discovery (verified in Docker, maven:3.9.6-eclipse-temurin-8):
#  - 34-PR range #142..#3935 spans WxJava 1.2.0 (2016, ~3 modules) to 4.8.2
#    (~11 modules). Compiler target 1.6 -> 1.8: JDK 8 builds the whole range.
#  - Tests are TestNG. The repo is hostile to automated measurement:
#      * the parent pom hardcodes surefire <skip>true</skip> — tests never run
#        in a normal build;
#      * modules pin surefire 2.17 and restrict execution to curated
#        testng.xml suites;
#      * most tests need live WeChat API credentials (test-config.xml, which
#        is gitignored — only test-config.sample.xml ships).
#  - To make a PR measurable, run.sh transforms the checked-out poms:
#      1. <skip>true</skip> -> <skip>false</skip>          (force tests on)
#      2. bump maven-surefire-plugin to 3.2.5              (reliable -Dtest +
#         per-class JUnit XML for TestNG)
#      3. delete <suiteXmlFiles> blocks                    (don't restrict to
#         the curated suite — most PRs don't update testng.xml)
#    and copies each module's test-config.sample.xml -> test-config.xml so
#    credential-gated test classes at least initialise.
#  - Only the PR's own patched test classes are run (`-Dtest=`). This isolates
#    each PR from TestNG's config-failure cascade (one Redis-dependent class's
#    @BeforeTest otherwise skips every test in the shared <test> group).
#  - parse_log reads surefire's per-class JUnit XML (junitreports/TEST-*.xml),
#    dumped to stdout by run_tests.sh — the TestNG console output collapses
#    everything into one unparseable "TestSuite".


def _test_classes(patch: str) -> list[str]:
    """Simple class names of *Test*.java files added/changed under src/test."""
    classes: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if "/src/test/" in path and path.endswith(".java"):
            base = path.rsplit("/", 1)[1][:-5]
            if "Test" in base:
                classes.add(base)
    return sorted(classes)


class WxJavaImageBase(Image):
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
        # Maven 3.9.6 + JDK 8 (multi-arch). JDK 8 is required — the oldest PRs
        # compile with `-source 1.6`, which JDK 11+ reject.
        return "maven:3.9.6-eclipse-temurin-8"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV MAVEN_OPTS="-Xmx2g"
ENV MAVEN_ARGS="-Dmaven.wagon.http.pool=false -Dhttp.keepAlive=false -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Dmaven.wagon.http.retryHandler.count=5"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates redis-server \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class WxJavaImageDefault(Image):
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
        return WxJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        classes = _test_classes(self.pr.test_patch)
        dtest = ("-Dtest=" + ",".join(classes)) if classes else ""

        check_git = """#!/bin/bash
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

        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false
git reset --hard
bash /home/check_git_changes.sh
git checkout __SHA__
bash /home/check_git_changes.sh

# Warm the ~/.m2 dependency cache (compile main + test sources at base sha).
mvn -B --no-transfer-progress -fae clean test-compile || true
""".replace("__REPO__", repo).replace("__SHA__", sha)

        # Shared test runner: transform the checked-out poms, enable
        # credential-module init, run only this PR's test classes, then dump
        # surefire's per-class JUnit XML for parse_log.
        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__

# 1. Force tests on (parent pom hardcodes surefire <skip>true</skip>).
find . -name pom.xml -print0 | xargs -0 sed -i 's#<skip>true</skip>#<skip>false</skip>#g'
# 2. Bump maven-surefire-plugin to 3.2.5 (the <version> line follows the
#    artifactId line) — reliable -Dtest + per-class JUnit XML for TestNG.
find . -name pom.xml -print0 | xargs -0 sed -i '/maven-surefire-plugin/{n;s#<version>[^<]*</version>#<version>3.2.5</version>#}'
# 3. Drop curated TestNG suite restriction so patched classes are runnable.
find . -name pom.xml -print0 | xargs -0 sed -i '/<suiteXmlFiles>/,/<\\/suiteXmlFiles>/d'

# Let credential-gated test classes initialise (real API calls still fail).
# The shipped samples put Chinese placeholder text in numeric config fields
# (e.g. <expiresTime>可以不填写</expiresTime>); XStream then throws
# NumberFormatException and the whole module's Guice injector fails. Replace
# every leaf element's text with "0" — valid for both String and numeric
# fields — so the injector builds and test classes can load.
for s in $(find . -path '*/src/test/resources/*' \\( -name 'test-config.sample.xml' -o -name 'test-config-sample.xml' \\) 2>/dev/null); do
  d="$(dirname "$s")/test-config.xml"
  cp "$s" "$d" 2>/dev/null || true
  sed -i -E 's#<([a-zA-Z0-9]+)>[^<]*</\\1>#<\\1>0</\\1>#g' "$d" 2>/dev/null || true
done

# Start a local Redis so tests that need it (e.g.
# RedisTemplateSimpleDistributedLockTest connects to 127.0.0.1:6379)
# run correctly instead of NPE-ing and hanging on a dead CountDownLatch.
redis-server --daemonize yes --save '' --appendonly no >/dev/null 2>&1 || true

# Hang protection: -Dsurefire.timeout kills a stuck forked test JVM after
# 15 min; the outer `timeout` is a hard cap on the whole mvn run. A hanging
# test then fails cleanly instead of blocking the evaluation forever.
timeout --signal=KILL 2400 \\
  mvn -B --no-transfer-progress -fae clean test \\
    -DfailIfNoTests=false -Dmaven.test.failure.ignore=true \\
    -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.timeout=900 \\
    __DTEST__ || true

echo '=====WXJAVA_SUREFIRE_XML_BEGIN====='
find . -path '*surefire-reports*' -name 'TEST-*.xml' ! -name 'TEST-TestSuite.xml' -print0 \\
  | xargs -0 cat 2>/dev/null || true
echo '=====WXJAVA_SUREFIRE_XML_END====='
""".replace("__REPO__", repo).replace("__DTEST__", dtest)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.jar --exclude=*.png --exclude=*.PNG --exclude=*.gif "
            "--exclude=*.ico --exclude=*.ttf --exclude=*.woff --exclude=*.woff2 "
            "--exclude=*.jpg --exclude=*.jpeg --exclude=*.zip --exclude=*.p12 "
            "--exclude=*.pem --exclude=*.cer --exclude=*.so --exclude=*.dylib"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("binarywang", "WxJava")
class WxJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WxJavaImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences.
        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Surefire per-class JUnit XML (dumped by run_tests.sh). Each
        # <testcase> is one test method; granularity = classname.methodName.
        #   <testcase name="m" classname="c" time="0"/>                -> pass
        #   <testcase ...><failure.../></testcase>  / <error.../>      -> fail
        #   <testcase ...><skipped/></testcase>                        -> skip
        tc_re = re.compile(r"<testcase\b([^>]*?)(?:/>|>(.*?)</testcase>)", re.S)
        name_re = re.compile(r'\bname="([^"]*)"')
        cls_re = re.compile(r'\bclassname="([^"]*)"')

        for m in tc_re.finditer(clean):
            attrs = m.group(1) or ""
            body = m.group(2) or ""
            nm = name_re.search(attrs)
            cl = cls_re.search(attrs)
            if not nm:
                continue
            name = nm.group(1).strip()
            cls = cl.group(1).strip() if cl else ""
            tid = f"{cls}.{name}" if cls else name

            if "<failure" in body or "<error" in body:
                failed_tests.add(tid)
            elif "<skipped" in body:
                skipped_tests.add(tid)
            else:
                passed_tests.add(tid)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
