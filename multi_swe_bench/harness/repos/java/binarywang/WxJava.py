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
#
# Structure — SINGLE-LEVEL, canonical image.py template (mirrors crossplane):
#   `dependency()` returns the *string* toolchain image (maven:...), so the
#   pipeline's DockerfileEnhancer engages — it injects the # syntax directive,
#   the ARG REPO_URL / ARG BASE_COMMIT block, and the cert/label infra. The
#   dockerfile() below carries everything image.py expects of a string-dep
#   image: apt install, `git clone "${REPO_URL}"` + `git checkout ${BASE_COMMIT}`,
#   the warm-only prepare.sh, then the *verbatim* Image._HARDENING_BLOCK strip
#   (origin/refs/reflog/gc + the four fail-closed post-condition asserts + the
#   submodule pass), and the final CMD. The build passes BASE_COMMIT=pr.base.sha
#   for every string-dep image (build_dataset.py), so no --dataset_generation
#   flag is required. Each PR is a self-contained image — there is no shared base
#   for the enhancer to pin+strip, so the pin-and-strip hazard that forced the
#   earlier 3-level layout does not apply here.


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
    # Level 1 — shared toolchain base, built once as
    # mswebench/binarywang_m_wxjava:base and reused by every PR. dependency() is a
    # *string* (Maven/JDK8), so DockerfileEnhancer.enhance() engages — but this
    # image does NOT clone, so the enhancer only injects its harmless infra/cert/
    # ARG/label block (there is no `RUN git clone` for it to rewrite+pin+strip).
    # The apt layer (git/curl/redis) lives here so it is built once, not per-PR.
    # JDK 8 is required — the oldest PRs compile with `-source 1.6`, which JDK 11+
    # reject.
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
        return "maven:3.9.6-eclipse-temurin-8"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

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

{self.clear_env}

CMD ["/bin/bash"]
"""


class WxJavaImageDefault(Image):
    # Level 2 — per-PR image. dependency() is the WxJavaImageBase *Image*, so the
    # harness builds the shared base first and DockerfileEnhancer.enhance()
    # returns this dockerfile VERBATIM (Image deps are not enhanced). That means:
    #  - no enhancer-injected ARG REPO_URL / ARG BASE_COMMIT — we declare
    #    ARG BASE_COMMIT="<pr.base.sha>" ourselves (Image deps get no BASE_COMMIT
    #    build-arg), and hardcode the clone URL;
    #  - the clone lives HERE (per-PR), so it is pinned to this PR's own sha —
    #    there is no shared cloned layer for the enhancer to pin+strip.
    # We embed Image._HARDENING_BLOCK verbatim (same strip + fail-closed asserts),
    # keyed on ${BASE_COMMIT}.
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
        return WxJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        classes = _test_classes(self.pr.test_patch)
        dtest = ("-Dtest=" + ",".join(classes)) if classes else ""

        # Warm-only prep. Runs AFTER the dockerfile's clone+checkout of
        # ${BASE_COMMIT} and BEFORE Image._HARDENING_BLOCK, so the full clone is
        # still present while the ~/.m2 cache is populated. Does NOT check out
        # (the dockerfile already did) and does NOT harden (the verbatim
        # _HARDENING_BLOCK that follows does that, keyed on ${BASE_COMMIT}).
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
git config core.autocrlf input
git config core.filemode false

# Warm the ~/.m2 dependency cache (compile main + test sources at base sha).
mvn -B --no-transfer-progress -fae clean test-compile || true

# Pre-warm maven-surefire-plugin 3.2.5 (+ its TestNG/JUnit runtime providers)
# into ~/.m2 so the run/test/fix stages NEVER fetch it from Maven Central.
# run_tests.sh sed-bumps surefire to 3.2.5 at run time; under concurrent instance
# execution many containers request 3.2.5 from Central simultaneously and get
# HTTP 429 (Too Many Requests). Maven caches that failed resolution -> BUILD
# FAILURE -> zero tests captured. Apply the same pom transforms run_tests.sh
# uses, run one no-op surefire pass to force plugin-core resolution, explicitly
# fetch the providers, then `git reset --hard` so test.patch/fix.patch still
# apply cleanly. ~/.m2 is outside the work tree, so the reset keeps the downloads.
find . -name pom.xml -print0 | xargs -0 sed -i 's#<skip>true</skip>#<skip>false</skip>#g'
find . -name pom.xml -print0 | xargs -0 sed -i '/maven-surefire-plugin/{n;s#<version>[^<]*</version>#<version>3.2.5</version>#}'
mvn -B --no-transfer-progress -fae test \
    -Dtest=__surefire_warmup_no_such_test__ -DfailIfNoTests=false \
    -Dmaven.test.failure.ignore=true -Dsurefire.failIfNoSpecifiedTests=false \
    -Dsurefire.timeout=120 || true
for prov in surefire-testng surefire-junit4 surefire-junit-platform; do
  mvn -B --no-transfer-progress \
    org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get \
    -Dartifact=org.apache.maven.surefire:${prov}:3.2.5 || true
done

# Revert the pom edits (keep the warmed ~/.m2) so the working tree is clean at
# base sha for Image._HARDENING_BLOCK's `git checkout --detach ${BASE_COMMIT}`
# and for the run-time `git apply` of test.patch / fix.patch.
git reset --hard
""".replace("__REPO__", repo)

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
  # Some sample configs at certain commits declare the same leaf element twice
  # (e.g. <msgAuditLibPath> appears inline AND again as an empty multi-line
  # element). XStream maps both to the one Java field and throws "Duplicate
  # field", which kills the whole module's Guice injector -> 0 tests captured at
  # every stage. Drop duplicate sibling elements (keep first), handling both the
  # inline <t>..</t> and the multi-line <t>\\n..\\n</t> forms. POSIX-awk only.
  awk '
    inblock { if ($0 ~ ("</" curtag ">")) { inblock=0; if (!skip) print; next } if (!skip) print; next }
    match($0, /<[a-zA-Z0-9]+>/) {
      t=substr($0, RSTART+1, RLENGTH-2)
      if (t=="xml") { print; next }
      if ($0 ~ ("</" t ">")) { if (seen[t]++) next; print; next }
      curtag=t; skip=(seen[t]++ ? 1 : 0); inblock=1; if (!skip) print; next
    }
    { print }
  ' "$d" > "$d.dedup" 2>/dev/null && mv "$d.dedup" "$d" 2>/dev/null || true
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
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Image dependency -> enhancer returns this verbatim. We hand-write what
        # the enhancer would otherwise inject: ARG BASE_COMMIT defaulted to this
        # PR's base sha (Image deps get no BASE_COMMIT build-arg), a hardcoded
        # clone URL, the checkout, then Image._HARDENING_BLOCK verbatim (keyed on
        # ${BASE_COMMIT}). prepare.sh runs BEFORE the strip so the warm sees the
        # full clone.
        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

RUN git clone https://github.com/{self.pr.org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
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


# ---------------------------------------------------------------------------
# Value-agnostic routing shim -- REGISTRY-SCOPED (this file only).
#
# This WxJava dataset is release-tag-range bundled: each instance's
# number_interval is the FULL set of PRs merged in the "vLo..vHi" window
# (e.g. "3935-3938-...-3996"), not a single PR number. Instance.create() looks
# up f"{org}/{number_interval}" when number_interval is set, and those exact
# multi-PR bundle keys are not registered (only the single-PR keys and the
# repo-level "binarywang/WxJava" key above are). Enumerating every bundle
# string would be brittle, so -- mirroring charmbracelet/crush -- we install a
# small, idempotent, binarywang/WxJava-scoped fallback on Instance.create:
# if the interval-based lookup misses, route to "binarywang/WxJava" regardless
# of the number_interval value. Other repos are unaffected (the fallback only
# fires for binarywang/WxJava, and only after the normal lookup raised).
# ---------------------------------------------------------------------------
_WXJAVA_ORG = "binarywang"
_WXJAVA_REPO = "WxJava"

if not getattr(Instance, "_wxjava_route_shim", False):
    _wxjava_orig_create = Instance.create.__func__

    def _wxjava_create(cls, pr, config, *args, **kwargs):
        try:
            return _wxjava_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _WXJAVA_ORG
                and getattr(pr, "repo", "") == _WXJAVA_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_wxjava_create)
    Instance._wxjava_route_shim = True
