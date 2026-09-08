"""strimzi/strimzi-kafka-operator -- Maven multi-module Java config.

Routing: the records in ``strimzi__strimzi-kafka-operator_raw_dataset.jsonl``
carry no ``number_interval`` and no ``tag``, so ``Instance.create`` resolves
every one of them to the plain ``strimzi/strimzi-kafka-operator`` key (see
``harness/instance.py``). That single key is therefore the whole of the routing
surface, and this one module holds the whole config.

The dataset does straddle a toolchain boundary, though: strimzi's root pom
declares ``maven.compiler.source/target = 11`` through the 0.32 line and 17 from
0.33 onward. So :class:`StrimziKafkaOperator` dispatches on ``pr.number`` --

    number <= 7292  -> JDK 11  PRs 5201 / 6669 / 7247 / 7292  (0.25 .. 0.32)
    number >= 7293  -> JDK 17  PR 7788                        (0.33)

-- picking one of two per-PR Image classes, each pointing at its era's SHARED
base image. Everything else (the module-scoped Maven invocation, the shared-base
+ per-PR hardening split, the surefire log parser) is identical across eras.

Two further things drive the shape of this config:

1. **No Maven wrapper.** The repo ships no ``mvnw`` at any of the dataset's base
   commits, so the base image installs Apache Maven from the upstream binary
   tarball (``_MAVEN_VERSION``) rather than relying on ``apt-get install maven``
   -- the Debian/Ubuntu package would drag in a second (default) JDK and shadow
   the ``eclipse-temurin`` toolchain the era base is built on.

2. **The reactor must not be built whole.** The ``systemtest`` module drives a
   live Kubernetes cluster; a full ``mvn test`` would hang or fail on it for
   reasons unrelated to the PR. So each per-PR image runs Maven scoped to the
   modules its own patches touch (``-pl <mods> -am``), derived from the patch
   text by :func:`_modules_for_pr`. ``-am`` pulls in the upstream modules the
   selection depends on, which keeps a fix landing in e.g. ``api`` visible to
   ``cluster-operator`` tests; nothing depends on ``systemtest``, so it stays
   out of the reactor either way.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Every Maven module name that appears in the reactor across the versions this
# dataset spans (0.25 .. 0.33). `test-container` only exists on the 0.25 line;
# the rest are stable. Patch paths are intersected with this set so non-module
# top-level directories (documentation/, packaging/, CHANGELOG.md, ...) never
# leak into a `-pl` selection.
_MODULES = {
    "api",
    "certificate-manager",
    "cluster-operator",
    "config-model",
    "config-model-generator",
    "crd-annotations",
    "crd-generator",
    "kafka-agent",
    "kafka-init",
    "mirror-maker-agent",
    "mockkube",
    "operator-common",
    "test",
    "test-container",
    "topic-operator",
    "tracing-agent",
    "user-operator",
}

# Requires a live Kubernetes cluster -- never selectable, and nothing in the
# reactor depends on it, so `-am` cannot drag it in either.
_EXCLUDED_MODULES = {"systemtest"}

# Fallback when a PR's patches touch no recognised module (should not happen on
# this dataset, but a `-pl` with an empty list would make Maven build nothing).
_DEFAULT_MODULES = ["cluster-operator"]

_MAVEN_VERSION = "3.9.9"

# mikefarah/yq v4. `tools/kafka-versions-tools.sh` -- which every config-model
# generation goes through -- calls `yq eval`, i.e. the Go yq's v4 CLI, NOT the
# Python `yq` wrapper around jq. It is not in any Debian/Ubuntu repo at a usable
# version, so the base pulls the release binary directly.
_YQ_VERSION = "4.44.3"

# `config-model-generator` writes `kafka-<v>-config-model.json` into
# `cluster-operator/src/main/resources/` from its `compile` phase (the
# `generate-all-models` profile is activeByDefault and execs
# `build-config-models.sh build`). cluster-operator's *runtime* code --
# KafkaVersion, KafkaConfiguration, KafkaCluster, CruiseControl,
# KafkaBrokerConfigurationDiff -- loads those files as classpath resources, and
# its tests exercise all of it.
#
# But cluster-operator's pom depends on `config-model` (the library), NOT on
# `config-model-generator`, so there is no dependency edge for `-am` to follow
# and the generator would never enter a `-pl cluster-operator -am` reactor. The
# models would be missing and the tests would fail for reasons that have nothing
# to do with the PR under test.
#
# The files are gitignored (`**/cluster-operator/src/main/resources/kafka-*-config-model.json`),
# which makes the fix clean: generate them ONCE at image build time in
# prepare.sh, and they survive both `git reset --hard` and `git clean -qfd`
# (no `-x`, so ignored files are left alone) into every eval run -- and they
# never trip check_git_changes.sh either.
_CONFIG_MODEL_MODULE = "config-model-generator"
_NEEDS_CONFIG_MODEL = "cluster-operator"

# Standard apt packages for the shared base. The eclipse-temurin image already
# provides the JDK; git + ca-certificates are needed for the HTTPS clone, and
# the rest mirror the harness default environment.
_BASE_APT = "ca-certificates curl build-essential git gnupg make python3 sudo wget unzip"

# Flags applied to every Maven invocation.
#   -B/-ntp     batch mode, no transfer progress -- keeps the log parseable.
#   -fae        fail at end, so one broken module does not hide later results.
#   checkstyle/spotbugs/javadoc skips: static analysis is bound to the strimzi
#   build and would fail a run for style reasons unrelated to the PR under test.
_MVN_FLAGS = (
    "-B -ntp -fae "
    "-DfailIfNoTests=false "
    "-Dcheckstyle.skip=true "
    "-Dspotbugs.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.gitcommitid.skip=true "
    # Pin the JUnit 5 parallel pool to a single thread.
    #
    # cluster-operator/src/test/resources/junit-platform.properties ships:
    #     junit.jupiter.execution.parallel.enabled=true
    #     junit.jupiter.execution.parallel.config.strategy=dynamic
    # `dynamic` sizes the pool from Runtime.availableProcessors(), so inside a
    # loaded container the async cluster-operator tests get starved of CPU. The
    # Vert.x JUnit 5 extension gives each test a 30s budget
    # (VertxExtension.DEFAULT_TIMEOUT_DURATION = 30) that is NOT configurable by
    # any system property in vertx-junit5 4.2.4, and none of these classes carry
    # an @Timeout, so a starved checkpoint fails as:
    #     java.util.concurrent.TimeoutException: The test execution timed out.
    # observed on KafkaConnectApiMockTest / StrimziPodSetControllerMockTest /
    # KafkaConnectBuildTest. Because the timeouts land on DIFFERENT classes each
    # run, a class can pass at baseline and fail after the fix, tripping the
    # harness Rule 4 "anomalous pattern" and invalidating an otherwise good
    # instance (~171 passing tests either side).
    #
    # System properties take precedence over junit-platform.properties, so this
    # tunes the ENVIRONMENT without editing any file in the repo under test.
    # Corroborating evidence: pr-7788 was valid in the serial instance run and
    # invalid at 2 workers -- same images, same commit, only load differed.
    "-Djunit.jupiter.execution.parallel.config.strategy=fixed "
    "-Djunit.jupiter.execution.parallel.config.fixed.parallelism=1 "
    "-Djunit.jupiter.execution.parallel.config.fixed.max-pool-size=1"
)

# Proxy vars MUST NOT be visible to the test JVM. strimzi production code does
# `System.getenv("HTTP_PROXY")` and tests `!= null`, so an EMPTY string counts as
# set (cluster-operator/.../model/KafkaConnectDockerfile.java). The shared base
# declares http_proxy/https_proxy/no_proxy unconditionally -- needed for apt, the
# Maven tarball and the clone at BUILD time, and taken from the reference
# Dockerfile -- but at TEST time they make KafkaConnectDockerfile emit three
# extra `ARG ...` lines the fixtures do not expect:
#     Expected: FROM myImage:latest
#          but: ARG http_proxy= / ARG https_proxy= / ARG no_proxy=... / FROM ...
# That broke ~50 assertions across 11 cluster-operator test classes (58-68 hits
# per PR) and, because the failing set shifted between baseline and fix, tripped
# the harness Rule 4 "anomalous pattern" (PASS -> NONE -> FAIL), invalidating
# every cluster-operator PR. pr-7247 (never builds cluster-operator) had 0 such
# failures and was the only cleanly valid instance. Unsetting here, rather than
# dropping the vars from the base, keeps proxy support for the image build while
# giving the tests a clean environment.
_UNSET_PROXY = "unset http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY\n"

_NEW_PATH_RE = re.compile(r"^\+\+\+ b/(\S+)", re.M)


def _modules_for_pr(pr: PullRequest) -> list:
    """Maven modules touched by this PR's fix + test patches, reactor order-free.

    Both patches are considered: a fix can land in an upstream module (``api``,
    ``operator-common``) while the tests live downstream, and PR 6669's test
    patch even edits ``mockkube/src/main`` -- all of those must be in the
    reactor for the run to be meaningful.
    """
    text = (pr.fix_patch or "") + "\n" + (pr.test_patch or "")
    found = set()
    for path in _NEW_PATH_RE.findall(text):
        top = path.split("/", 1)[0]
        if top in _MODULES and top not in _EXCLUDED_MODULES:
            found.add(top)
    if not found:
        return list(_DEFAULT_MODULES)
    return sorted(found)


def _settings_b64() -> str:
    """`_MAVEN_SETTINGS` as one base64 line, for embedding in the base Dockerfile.

    Two constraints force this over the obvious alternatives:

    * It cannot be a `COPY`, because the base image folder must contain nothing
      but the Dockerfile.
    * It cannot be a BuildKit heredoc (`RUN cat > f <<'EOF'`). The harness picks
      its builder in `docker_util.build`: with `--platform` it shells out to
      buildx, without it (our case) it goes through `_build_with_sdk`, the
      Python Docker SDK. Under that path the heredoc silently produced a
      ZERO-BYTE settings.xml, and Maven treats an empty settings file as fatal
      ("Non-readable settings ...: input contained no data"), so every build
      that used it was broken end to end.

    A single-line `echo '<b64>' | base64 -d` has no multi-line parsing
    subtleties in either builder. The Dockerfile asserts the result is non-empty
    and well-terminated so a regression of this class fails the build loudly
    instead of silently shipping an unusable image.
    """
    import base64

    return base64.b64encode(_MAVEN_SETTINGS.encode("utf-8")).decode("ascii")


def _config_model_shell(selected: list) -> str:
    """Build-time snippet generating cluster-operator's Kafka config models.

    Only emitted when ``cluster-operator`` is in the reactor -- an api-only PR
    (7247) never loads those resources, and each generation runs a nested Maven
    build per supported Kafka version, so it is not free.

    Deliberately NOT best-effort, unlike the dependency warm-up below it: if the
    models are missing the eval run fails in a way that looks like a genuine
    test failure, which is exactly the silent corruption this config must not
    ship. Failing the image build instead makes the problem loud and local.
    """
    if _NEEDS_CONFIG_MODEL not in selected:
        return "# cluster-operator not in reactor -- no config models needed\n"

    return (
        "# --- Generate cluster-operator Kafka config models ---\n"
        "# build-config-models.sh writes kafka-<v>-config-model.json into\n"
        "# cluster-operator/src/main/resources/ (one per supported Kafka\n"
        "# version). Those paths are gitignored, so they survive\n"
        "# `git reset --hard` + `git clean -qfd` into every eval run.\n"
        "#\n"
        "# The script is invoked DIRECTLY rather than through a Maven phase,\n"
        "# because the two eras in this dataset bind it differently:\n"
        "#   0.29+ : config-model-generator's `generate-all-models` profile is\n"
        "#           activeByDefault and execs it at the `compile` phase.\n"
        "#   0.25  : that profile does not exist -- the pom declares\n"
        "#           exec-maven-plugin with NO <executions>, and only\n"
        "#           `make java_build` runs the script. A plain `mvn compile`\n"
        "#           there succeeds while generating nothing at all.\n"
        "# Calling the script directly is correct on both, and each era's own\n"
        "# copy of the script already knows whether it needs -Pgenerate-model.\n"
        "#\n"
        "# The script shells out to a NESTED `mvn verify exec:java` whose\n"
        "# reactor contains only config-model-generator, so it resolves\n"
        "# io.strimzi:config-model from ~/.m2 rather than from the sibling\n"
        "# module's target/. It must therefore be installed first -- `compile`\n"
        "# alone leaves the nested build failing with 'Could not find artifact\n"
        "# io.strimzi:config-model:jar:<version>'.\n"
        "#\n"
        "# MVN_ARGS is honoured by the script and passed to that nested build;\n"
        "# it carries the same static-analysis skips as the outer invocations\n"
        "# so a style violation cannot fail model generation.\n"
        'export MVN_ARGS="-B -ntp -Dcheckstyle.skip=true -Dspotbugs.skip=true'
        ' -Dmaven.javadoc.skip=true"\n'
        f"mvn {_MVN_FLAGS} -pl config-model -am -DskipTests install\n"
        f"( cd {_CONFIG_MODEL_MODULE} && bash ./build-config-models.sh build )\n"
        "_models=$(ls cluster-operator/src/main/resources/kafka-*-config-model.json 2>/dev/null | wc -l)\n"
        'if [ "${_models}" -lt 1 ]; then\n'
        '  echo "prepare: config-model generation produced no '
        'kafka-*-config-model.json files" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "prepare: generated ${_models} Kafka config model(s)"\n'
        "# --- end config models ---\n"
    )


def _mvn_test_cmd(pr: PullRequest) -> str:
    """Maven test run scoped to this PR's modules plus their upstream deps.

    The goal is `package`, NOT `test`, and that is load-bearing. The `api`
    module binds CRD generation to `process-classes` via exec-maven-plugin, with
    an explicit -classpath of::

        ../crd-generator/target/crd-generator-<version>.jar

    i.e. the sibling module's packaged JAR on disk, not its target/classes and
    not a ~/.m2 artifact. `mvn test` stops at the `test` phase and never runs
    `package`, so that JAR is never produced and the exec dies with
    "Could not find or load main class io.strimzi.crdgenerator.CrdGenerator".
    The api module then FAILS before surefire runs, which silently costs the
    run every api and cluster-operator test while still reporting the upstream
    modules' tests as passing.

    `package` is the earliest phase that satisfies the pom's own requirement:
    reactor order builds crd-generator (through package, producing the JAR)
    before api, so the exec finds it. Surefire still runs -- `test` precedes
    `package` -- so test results are unchanged in content, only in completeness.
    """
    mods = ",".join(_modules_for_pr(pr))
    return f"mvn {_MVN_FLAGS} -pl {mods} -am package"


def parse_maven_log(test_log: str) -> TestResult:
    """Parse maven-surefire output into a TestResult, at TEST-CLASS granularity.

    Surefire's default reporter emits, per test class::

        [INFO] Running io.strimzi.operator.topic.ConfigTest
        [INFO] Tests run: 7, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 0.6 s - in io.strimzi.operator.topic.ConfigTest

    and, only for classes that fail, additional per-METHOD lines::

        [ERROR] io.strimzi...ClusterCaTest.testRemoveOldCertificate  Time elapsed: 0.1 s  <<< FAILURE!
        [ERROR] ClusterCaTest.testRemoveOldCertificate:412 expected...   (summary block, UNQUALIFIED)

    Recording those method lines as test names -- which an earlier version of
    this parser did -- corrupts the cross-stage comparison in three ways, all
    observed on this dataset:

    1. MIXED GRANULARITY. Both `...ClusterCaTest` and
       `...ClusterCaTest.testRemoveOldCertificate` entered the sets, so one test
       was counted twice.
    2. UNQUALIFIED DUPLICATES. The failure-summary block prints
       `ClusterCaTest.testX` with no package -- a *different* string from the
       fully-qualified inline form -- so one test appeared under two names.
    3. PHANTOM TRANSITIONS. Method lines exist ONLY in stages where the class
       fails. `Report.__post_init__` unions names across the three stages and
       fills the gaps with `TestStatus.NONE`, so a method seen only in the fix
       stage reads NONE -> NONE -> FAIL. It also captured
       `KafkaConnectApiTest.before`, a @BeforeEach setup method that is not a
       test at all.

    Class names are the only identifier surefire prints in EVERY stage, always
    fully qualified, so they are the only stable key. Method lines are therefore
    used solely to mark their enclosing class failed -- never added as names.

    A class counts as skipped only when all of its tests were skipped
    (`skipped >= total`); any failure or error marks it failed and removes it
    from the passed set, keeping the three sets disjoint as
    `TestResult.__post_init__` requires.
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    # `[INFO] Running <fqcn>` opens a class and is the fallback owner for a
    # counts line that omits the trailing `- in <fqcn>`.
    running_re = re.compile(r"^\[[A-Z]+\]\s+Running\s+(\S+)\s*$")

    # Per-class counts line. The trailing `- in <fqcn>` is optional: surefire
    # omits it on the reactor-level summary, which must NOT be recorded.
    counts_re = re.compile(
        r"^\[[A-Z]+\]\s+Tests run:\s*(\d+),\s*"
        r"Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
        r"(?:,\s*Time elapsed.*?)?"
        r"(?:\s*<<<\s*(?:FAILURE|ERROR)!)?"
        r"(?:\s*-\s*in\s+(\S+))?\s*$"
    )

    current_class = None
    for line in clean_log.splitlines():
        line = line.rstrip()

        m = running_re.match(line)
        if m:
            current_class = m.group(1)
            continue

        m = counts_re.match(line)
        if not m:
            continue

        total, failures, errors, skipped = (int(g) for g in m.groups()[:4])
        name = m.group(5) or current_class
        if not name:
            # Reactor-level "Tests run:" summary -- no class, do not record.
            continue

        if failures or errors:
            failed_tests.add(name)
            passed_tests.discard(name)
            skipped_tests.discard(name)
        elif total and skipped >= total:
            if name not in failed_tests:
                skipped_tests.add(name)
        elif total:
            if name not in failed_tests and name not in skipped_tests:
                passed_tests.add(name)
        current_class = None

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )

class StrimziBaseImage(Image):
    """SINGLE SHARED base per JDK era, built ONCE and reused as the FROM parent
    of every per-PR image in that era.

    It clones the FULL repo history and KEEPS ``origin`` so each per-PR image can
    ``git checkout`` its own base commit; git hardening happens PER-PR in
    :class:`StrimziPRImage`, after that checkout, which is what lets this one
    base stay shared. It also warms ``~/.m2`` by resolving dependencies once, so
    the (very large) strimzi dependency download is not repeated per PR.

    The leading ``# syntax`` directive makes ``DockerfileEnhancer.enhance()``
    return this Dockerfile VERBATIM (it early-returns when the directive is
    present). That is deliberate: it stops the enhancer rewriting the clone into
    a ``git checkout ${BASE_COMMIT}`` + history-strip, which would pin this
    shared base to whichever PR happened to build it first.

    Subclasses set ``JDK_IMAGE`` and the ``ERA_LO``/``ERA_HI`` PR numbers that
    name the image tag.
    """

    JDK_IMAGE = "eclipse-temurin:11"
    # Inclusive PR-number bounds of the era, newest first in the tag.
    ERA_LO = 0
    ERA_HI = 0

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self):
        return self.JDK_IMAGE

    def image_tag(self) -> str:
        return f"base-{self.ERA_HI}_to_{self.ERA_LO}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        # Deliberately empty: the base image build context must contain nothing
        # but the Dockerfile. Maven's settings.xml is written by a heredoc in
        # dockerfile() rather than COPY'd, so no support file has to sit
        # alongside it in the image folder.
        return []

    def dockerfile(self) -> str:
        jdk = self.JDK_IMAGE
        org, repo = self.pr.org, self.pr.repo
        mvn = _MAVEN_VERSION
        return f"""# syntax=docker/dockerfile:1.6
FROM {jdk}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
ARG MAVEN_VERSION="{mvn}"
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CI=true \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    MAVEN_HOME="/opt/maven" \\
    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1024m" \\
    PATH="/opt/maven/bin:${{PATH}}" \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_BASE_APT} && rm -rf /var/lib/apt/lists/*

# yq v4 (Go implementation). Required by tools/kafka-versions-tools.sh, which
# every config-model generation sources; the Python `yq` does not speak `eval`.
RUN set -eux; \\
    case "${{TARGETARCH:-amd64}}" in \\
        amd64|"") _yqarch=amd64 ;; \\
        arm64) _yqarch=arm64 ;; \\
        *) _yqarch="${{TARGETARCH}}" ;; \\
    esac; \\
    curl -fsSL -o /usr/local/bin/yq \\
        "https://github.com/mikefarah/yq/releases/download/v{_YQ_VERSION}/yq_linux_${{_yqarch}}"; \\
    chmod +x /usr/local/bin/yq; \\
    yq --version

# strimzi-kafka-operator ships no Maven wrapper, and `apt-get install maven`
# would pull in a second JDK that shadows the eclipse-temurin toolchain, so
# Maven comes from the upstream binary tarball.
RUN set -eux; \\
    curl -fsSL -o /tmp/maven.tar.gz \\
        "https://archive.apache.org/dist/maven/maven-3/${{MAVEN_VERSION}}/binaries/apache-maven-${{MAVEN_VERSION}}-bin.tar.gz"; \\
    mkdir -p /opt/maven; \\
    tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1; \\
    rm -f /tmp/maven.tar.gz; \\
    mvn -v

RUN set -eux; \\
    mkdir -p /root/.m2; \\
    echo '{_settings_b64()}' | base64 -d > /root/.m2/settings.xml; \\
    test -s /root/.m2/settings.xml; \\
    grep -q '</settings>' /root/.m2/settings.xml

RUN git clone "${{REPO_URL}}" /home/{repo}
CMD ["/bin/bash"]
"""


class StrimziPRImage(Image):
    """Per-PR image: FROM the shared era base, check out THIS PR's base commit,
    warm the per-SHA Maven dependency set, then STRIP git history so the
    evaluated agent cannot recover the fix from ``git log``/``git show``.

    Subclasses set ``BASE_CLASS`` to the matching :class:`StrimziBaseImage`.
    """

    BASE_CLASS = StrimziBaseImage

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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns and leaves dockerfile() verbatim, so the hardening below
        # is applied by hand, anchored on the checked-out base commit.
        return self.BASE_CLASS(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha
        mvn_test = _mvn_test_cmd(self.pr)
        selected = _modules_for_pr(self.pr)
        mods = ",".join(selected)
        config_model_sh = _config_model_shell(selected)

        # Integrity guard. `git status --porcelain` lists modified, staged and
        # untracked files but NOT gitignored ones, so the warm `target/`
        # artifacts left by the dependency pass do not trip it -- it fires only
        # on a genuinely dirty tree.
        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            # This script runs LAST in the Dockerfile, after the hardening pass
            # has already detached HEAD at the base commit and deleted every
            # other ref, so the checkout below is a reassertion rather than a
            # move. Kept so the script is still correct run standalone.
            # No `|| true`: a failed reset must abort the build.
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            + config_model_sh
            + "\n"
            "# Warm ~/.m2 for THIS base commit: resolve and compile the selected\n"
            "# modules once at build time so the eval runs do not re-download the\n"
            "# reactor's dependency set. Best-effort -- a failure here must not\n"
            "# fail the image build, the eval run is the source of truth.\n"
            f"mvn {_MVN_FLAGS} -pl {mods} -am -DskipTests package || true\n"
            "\n"
            "git checkout -- .\n"
            "bash /home/check_git_changes.sh\n"
        )
        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            + _UNSET_PROXY
            + f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            f"{mvn_test}\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            + _UNSET_PROXY
            + f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{mvn_test}\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            + _UNSET_PROXY
            + f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{mvn_test}\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        number = self.pr.number

        return f"""FROM {name}:{tag}

{copy_commands}
RUN set -eux; \\
    cd /home/{repo}; \\
    git cat-file -e {sha}^{{commit}} 2>/dev/null \\
        || git fetch --no-tags origin +refs/pull/{number}/head:refs/remotes/origin/pr-{number}; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN set -eux; \\
    cd /home/{repo}; \\
    if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive 'git checkout --detach HEAD; git remote remove origin 2>/dev/null || true; git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; git reflog expire --expire=now --all; git reflog expire --expire-unreachable=now --all; git gc --prune=now --aggressive; rm -f .git/objects/info/alternates;'; \\
    fi

RUN bash /home/prepare.sh
"""


# Baked into the shared base at /root/.m2/settings.xml. Central only -- strimzi's
# poms declare the extra repositories they need themselves; pinning the mirror
# here keeps resolution deterministic and off any inherited mirror config.
_MAVEN_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0
                              https://maven.apache.org/xsd/settings-1.0.0.xsd">
  <localRepository>/root/.m2/repository</localRepository>
  <interactiveMode>false</interactiveMode>
  <!-- Route `central` through Google's read-only mirror of Maven Central.
       repo.maven.apache.org and repo1.maven.org both rate-limit heavy CI use
       and start returning HTTP 429. When that happens mid-eval, surefire's
       provider jar (org.apache.maven.surefire:surefire-junit-platform) cannot
       be resolved, so NO tests execute and the stage reports 0/0/0, which the
       harness rejects as "no test results were captured". Observed on
       2026-09-07 with both Apache hosts at 429 while this mirror served 200.
       Same artifacts, same checksums; only the host differs.
       NOTE: no double hyphen may appear inside an XML comment; it makes
       settings.xml malformed and Maven then dies with a bare
       "Error executing Maven." before any goal runs. -->
  <mirrors>
    <mirror>
      <id>central-gcs</id>
      <name>Google-hosted Maven Central mirror</name>
      <url>https://maven-central.storage-download.googleapis.com/maven2</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
  <profiles>
    <profile>
      <id>strimzi-msb</id>
      <repositories>
        <repository>
          <id>central</id>
          <url>https://repo.maven.apache.org/maven2</url>
          <releases><enabled>true</enabled></releases>
          <snapshots><enabled>false</enabled></snapshots>
        </repository>
      </repositories>
      <pluginRepositories>
        <pluginRepository>
          <id>central</id>
          <url>https://repo.maven.apache.org/maven2</url>
          <releases><enabled>true</enabled></releases>
          <snapshots><enabled>false</enabled></snapshots>
        </pluginRepository>
      </pluginRepositories>
    </profile>
  </profiles>
  <activeProfiles>
    <activeProfile>strimzi-msb</activeProfile>
  </activeProfiles>
</settings>
"""


class StrimziJdk11Base(StrimziBaseImage):
    """Shared base for the 0.25 .. 0.32 line (PRs 5201 .. 7292)."""

    JDK_IMAGE = "eclipse-temurin:11"
    ERA_LO = 5201
    ERA_HI = 7292


class StrimziJdk17Base(StrimziBaseImage):
    """Shared base for the 0.33 line onward (PRs 7293 .. 7788).

    The lower bound sits just above the last confirmed JDK 11 record, so any
    later PR added to this dataset lands here by default.
    """

    JDK_IMAGE = "eclipse-temurin:17"
    ERA_LO = 7293
    ERA_HI = 7788


class StrimziJdk11ImageDefault(StrimziPRImage):
    BASE_CLASS = StrimziJdk11Base


class StrimziJdk17ImageDefault(StrimziPRImage):
    BASE_CLASS = StrimziJdk17Base


# Inclusive upper bound of the JDK 11 build environment. 7292 is the newest
# dataset PR whose base pom still declares maven.compiler.source=11 (0.32 line);
# 7788 (0.33 line) declares 17.
_JDK11_MAX = 7292


@Instance.register("strimzi", "strimzi-kafka-operator")
class StrimziKafkaOperator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= _JDK11_MAX:
            return StrimziJdk11ImageDefault(self.pr, self._config)
        return StrimziJdk17ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_maven_log(test_log)
