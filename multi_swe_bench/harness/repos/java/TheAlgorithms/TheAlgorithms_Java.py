"""Generic TheAlgorithms/Java config.

Used for jsonl records that carry no ``number_interval``, so ``Instance.create``
resolves them to the plain ``"TheAlgorithms/Java"`` key (the bundle-specific
configs register under ``TheAlgorithms_Java_<range>`` keys instead).

Three dataset-specific defects drive the design here; each is handled in
``prepare.sh``/``*-run.sh`` rather than by touching the raw jsonl.

1. ``base.sha`` IS NOT THE PATCH PRE-IMAGE.
   The collector builds patches from ``GET /compare/{base_sha}...{head_sha}``
   (``multi_swe_bench/collect/build_dataset.py:111``), and GitHub's compare
   endpoint diffs from the MERGE BASE, not from ``base_sha`` literally. Most of
   these PRs were opened from long-stale forks, so the merge base sits years
   behind the recorded base and the patches only apply there. Measured over the
   37 rows: 4/37 apply at ``base.sha``, 37/37 at the merge base.

   ``prepare.sh`` therefore fetches ``refs/pull/<N>/head`` and checks out
   ``git merge-base <base.sha> FETCH_HEAD``. Resolving it in-container (rather
   than baking a host-computed SHA into the file) keeps rendering independent of
   the host clone's ref state.

2. UNAPPLIABLE DIFFS ARE EMBEDDED IN THE PATCHES.
   The collector emits ``Binary files a/x and b/x differ`` with no accompanying
   ``GIT binary patch`` payload, so those diffs carry no data to apply. One
   non-UTF-8 text blob (``myfile.txt``) was additionally lossy-decoded into
   U+FFFD replacement characters, so its hunk can never match the real blob.
   Both kinds are unappliable by construction, not by misconfiguration.

   ``_undeliverable_paths()`` finds them by inspecting the patch text at render
   time and emits a ``--exclude=<path>`` per path. ``git apply`` then skips
   exactly those files and applies everything else. Note this drops
   ``gradle/wrapper/gradle-wrapper.jar`` (binary), so ``./gradlew`` is NOT
   usable and the Gradle-era rows need the system Gradle.

3. THE BUILD FILE ARRIVES WITH THE FIX PATCH.
   For most rows ``pom.xml``/``build.gradle`` does not exist at the merge base
   and is CREATED by the fix patch. The run and test stages consequently have no
   build system and report zero tests, while the fix stage reports many. That is
   a genuine property of these PRs (they are "add a build system + tests"
   changes), and ``report.py`` classifies the result as N2P rather than F2P. The
   run scripts exit 0 with no output when no build file is present, so the empty
   stages stay clean rather than erroring.

Toolchain is selected at eval time, not build time: the required JDK is only
knowable after the patches land, so the base image carries JDK 8/11/17 plus
Maven and Gradle, and ``select_toolchain.sh`` picks per stage.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Gradle 6.9.4 is the newest release that still accepts the `compile` and
# `testRuntime` configurations these 2019-2021 build.gradle files use; both were
# removed in Gradle 7. It also still runs on JDK 8, which the same files request
# via `sourceCompatibility = '1.8'`.
_GRADLE_VERSION = "6.9.4"


def _split_diffs(patch: str) -> list[str]:
    """Split a patch into its per-file ``diff --git`` sections."""
    if not patch:
        return []
    return re.findall(r"(?:^diff --git .*?)(?=^diff --git |\Z)", patch, re.M | re.S)


def _diff_paths(diff: str) -> set[str]:
    """Every path a single file-diff refers to.

    Reads the ``---``/``+++`` pair first because those tolerate spaces in paths
    (``Screenshot from 2018-10-09.png``), which the ``diff --git`` header does
    not parse unambiguously. The header is still consulted since binary diffs
    may carry no ``---``/``+++`` lines at all.
    """
    out: set[str] = set()
    for m in re.finditer(r'^(?:---|\+\+\+) (?:"?[ab]/)?(.+?)"?$', diff, re.M):
        path = m.group(1).strip()
        if path and path != "/dev/null":
            out.add(path)
    header = diff.split("\n", 1)[0]
    m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', header)
    if m:
        out.add(m.group(1))
        out.add(m.group(2))
    return out


def _undeliverable_paths(*patches: str) -> list[str]:
    """Paths whose diffs cannot be applied because their content is absent.

    Two causes, both introduced upstream of the harness:
      * ``Binary files ... differ`` with no ``GIT binary patch`` payload.
      * Mojibake: a non-UTF-8 blob lossy-decoded to U+FFFD, so no hunk matches.
    """
    bad: set[str] = set()
    for patch in patches:
        for diff in _split_diffs(patch):
            if (
                "Binary files" in diff
                or "GIT binary patch" in diff
                or "\ufffd" in diff
            ):
                bad |= _diff_paths(diff)
    return sorted(bad)


class TheAlgorithmsJavaImageBase(Image):
    """Shared toolchain image: clone only, no checkout.

    One image for all PRs, so it must NOT pin a commit. Emitting the BuildKit
    syntax directive opts out of ``DockerfileEnhancer._standardize_repo_fetch``,
    which would otherwise rewrite the clone into ``git checkout ${BASE_COMMIT}``
    and pin an image shared by 37 PRs to whichever ``base.sha`` won image dedup.
    Opting out means this file emits the proxy/cert/label infrastructure itself.
    """

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    MAVEN_OPTS="-Xmx2g" \\
    GRADLE_HOME="/opt/gradle" \\
    GRADLE_OPTS="-Xmx2g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

# JDK 8, 11 and 17 are all installed: the JDK a stage needs is only knowable
# after the patches land (the build file itself usually arrives with the fix
# patch), so selection happens at eval time in select_toolchain.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    unzip \\
    openjdk-8-jdk \\
    openjdk-11-jdk \\
    openjdk-17-jdk \\
    maven \\
    python3 \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

# System Gradle, not ./gradlew: gradle-wrapper.jar is a binary blob that the
# collector recorded as "Binary files ... differ" with no payload, so it is one
# of the excluded paths and the wrapper is never materialised.
#
# Fetched from the GitHub release rather than services.gradle.org, which is only
# a 307 redirect to this same asset but serves it at ~31 KB/s and drops the TLS
# connection partway (curl 56) - the 107 MB zip could not complete in 2195s.
# Measured side by side: 30,927 B/s vs 5,599,378 B/s.
RUN curl -fsSL --retry 5 --retry-connrefused --retry-delay 5 \\
      -o /tmp/gradle.zip \\
      "https://github.com/gradle/gradle-distributions/releases/download/v{_GRADLE_VERSION}/gradle-{_GRADLE_VERSION}-bin.zip" \\
 && unzip -q /tmp/gradle.zip -d /opt \\
 && mv /opt/gradle-{_GRADLE_VERSION} /opt/gradle \\
 && rm -f /tmp/gradle.zip \\
 && /opt/gradle/bin/gradle --version

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class TheAlgorithmsJavaImageDefault(Image):
    """Per-PR image: resolves the merge base and pre-warms the dependency cache."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha
        number = self.pr.number

        # Rendered into every apply site so the three stages apply an identical
        # file set. Computed from the patch text alone - no git, no network.
        excludes = " ".join(
            f"--exclude='{path}'"
            for path in _undeliverable_paths(self.pr.test_patch, self.pr.fix_patch)
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
            File(
                ".",
                "apply_patches.sh",
                r"""#!/bin/bash
# Apply the recorded patches, skipping the diffs whose content the collector
# never captured (see _undeliverable_paths in the config module). Without the
# exclusions git apply rejects the whole patch over files it cannot write.
#
# Usage: apply_patches.sh <patch> [<patch>...]
set -e

EXCLUDES=([[EXCLUDES]])

for patch in "$@"; do
    if ! git apply --whitespace=nowarn "${EXCLUDES[@]}" "$patch"; then
        echo "Error: git apply failed for $patch" >&2
        exit 1
    fi
done
""".replace("[[EXCLUDES]]", excludes),
            ),
            File(
                ".",
                "select_toolchain.sh",
                r"""#!/bin/bash
# Pick JAVA_HOME from whatever the working tree now asks for, and export BUILD_SYSTEM.
# Must run AFTER patches are applied: for most of these PRs the build file is
# created by the fix patch, so the baseline tree has nothing to inspect.
#
# Sourced, not executed - it exports into the caller's shell.

_ta_jdk() {
    local want="$1"
    local arch
    arch="$(dpkg --print-architecture)"
    local home="/usr/lib/jvm/java-${want}-openjdk-${arch}"
    if [ -d "$home" ]; then
        echo "$home"
    else
        echo ""
    fi
}

BUILD_SYSTEM="none"
JAVA_WANT="17"

if [ -f pom.xml ]; then
    BUILD_SYSTEM="maven"
    # <java.version>, <maven.compiler.source> or <maven.compiler.release>.
    ver="$(sed -n -E 's@.*<(java\.version|maven\.compiler\.source|maven\.compiler\.release)>([^<]+)</.*@\2@p' pom.xml | head -1)"
    case "$ver" in
        1.8|8) JAVA_WANT="8" ;;
        11)    JAVA_WANT="11" ;;
        *)     JAVA_WANT="17" ;;
    esac
elif [ -f build.gradle ] || [ -f build.gradle.kts ]; then
    BUILD_SYSTEM="gradle"
    gfile="build.gradle"
    [ -f "$gfile" ] || gfile="build.gradle.kts"
    ver="$(sed -n -E "s@.*(sourceCompatibility|targetCompatibility)[[:space:]]*=[[:space:]]*['\"]?([0-9.]+)['\"]?.*@\2@p" "$gfile" | head -1)"
    case "$ver" in
        1.8|8) JAVA_WANT="8" ;;
        11)    JAVA_WANT="11" ;;
        *)     JAVA_WANT="8" ;;
    esac
fi

JAVA_HOME="$(_ta_jdk "$JAVA_WANT")"
if [ -z "$JAVA_HOME" ]; then
    JAVA_HOME="$(_ta_jdk 17)"
fi

export BUILD_SYSTEM
export JAVA_HOME
export PATH="$JAVA_HOME/bin:/opt/gradle/bin:$PATH"

echo "select_toolchain: build=$BUILD_SYSTEM jdk=$JAVA_WANT JAVA_HOME=$JAVA_HOME"
""",
            ),
            File(
                ".",
                "fixup_build.sh",
                r"""#!/bin/bash
# Make the project's own build file actually able to RUN its tests.
#
# The 2019-era pom.xml files declare junit-jupiter-api but ship neither a
# surefire plugin nor the jupiter engine, so `mvn test` compiles the tests and
# then runs nothing. The Gradle files predate the `gradle test` default of
# failing the build on test failure and omit useJUnitPlatform().
#
# This edits tracked files, so it runs at eval time: the image's hardening pass
# re-checks-out the merge base after extra_setup(), which would revert any edit
# made during the build.
set -e

SUREFIRE_VERSION=3.2.5

# PR 1786's test patch files ROTNTest.java under src/main/java/, so the main
# source set - which has no junit on its classpath - tries to compile it and the
# whole build dies before any test runs. Relocate by declared package, falling
# back to the default package. Verified a no-op on every other buildable row.
if [ -f pom.xml ] || [ -f build.gradle ] || [ -f build.gradle.kts ]; then
    for stray in $(find src/main -name '*Test*.java' 2>/dev/null); do
        pkg="$(sed -n 's@^package[[:space:]]\+\([A-Za-z0-9_.]*\);.*@\1@p' "$stray" | head -1)"
        if [ -n "$pkg" ]; then
            dest="src/test/java/$(echo "$pkg" | tr . /)"
        else
            dest="src/test/java"
        fi
        mkdir -p "$dest"
        mv -f "$stray" "$dest/$(basename "$stray")"
        echo "fixup_build: relocated $stray -> $dest"
    done
fi

if [ -f pom.xml ]; then
    if grep -q "junit-jupiter" pom.xml && ! grep -q "maven-surefire-plugin" pom.xml; then
        sed -i '/<\/plugins>/i \
            <plugin>\
                <groupId>org.apache.maven.plugins</groupId>\
                <artifactId>maven-surefire-plugin</artifactId>\
                <version>'"$SUREFIRE_VERSION"'</version>\
            </plugin>' pom.xml
    fi

    # Surefire 2.22.2 (what these poms pin) bundles junit-platform-launcher 1.3.1
    # and refuses to upgrade it, so with a JUnit 5.8+/5.9 engine the launcher
    # cannot discover anything and the run reports "Tests run: 0" on a BUILD
    # SUCCESS - silently zero tests. 3.x resolves a launcher matching the
    # project's own platform version. Measured on PR 4817: 0 -> 3861 tests.
    # Surefire 3.x talks to its forked JVM over the process pipe by default, and
    # a lot of this repo's algorithm code reads System.in. The first such read
    # consumes surefire's control bytes and the fork dies with "[SUREFIRE] std/in
    # stream corrupted" AFTER running every test, so the XML reports exist but the
    # build fails. SurefireForkNodeFactory moves the channel to a TCP socket and
    # leaves stdin alone. Measured on PR 4212: BUILD FAILURE -> BUILD SUCCESS.
    python3 - "$SUREFIRE_VERSION" <<'PYEOF'
import re
import sys

FORK_NODE = (
    "\n                <configuration>"
    "\n                    <forkNode"
    ' implementation="org.apache.maven.plugin.surefire.extensions.SurefireForkNodeFactory"/>'
    "\n                </configuration>"
)

wanted = sys.argv[1]
with open("pom.xml") as fh:
    pom = fh.read()

patched = re.sub(
    r"(<artifactId>maven-surefire-plugin</artifactId>\s*<version>)[^<]+(</version>)",
    lambda m: m.group(1) + wanted + m.group(2),
    pom,
)
if "SurefireForkNodeFactory" not in patched:
    patched = re.sub(
        r"(<artifactId>maven-surefire-plugin</artifactId>\s*<version>[^<]+</version>)",
        lambda m: m.group(1) + FORK_NODE,
        patched,
    )
if patched != pom:
    with open("pom.xml", "w") as fh:
        fh.write(patched)
PYEOF

    # Only inject an engine when the pom genuinely has none. The modern poms
    # (PRs 4212/4226/4817) depend on the `junit-jupiter` aggregate, which already
    # pulls junit-jupiter-engine transitively; injecting a second, hardcoded
    # engine version there downgrades the engine below the api/platform versions
    # managed by junit-bom and test discovery silently finds nothing.
    has_engine=no
    grep -q "junit-jupiter-engine" pom.xml && has_engine=yes
    grep -q "<artifactId>junit-jupiter</artifactId>" pom.xml && has_engine=yes
    grep -q "<artifactId>junit-bom</artifactId>" pom.xml && has_engine=yes

    if grep -q "junit-jupiter-api" pom.xml && [ "$has_engine" = no ]; then
        # Match the engine to the api version the pom already asks for, so the
        # two halves of JUnit 5 stay on the same platform revision.
        api_version="$(sed -n 's@.*<junit-jupiter-api\.version>\([0-9][0-9.]*\)</junit-jupiter-api\.version>.*@\1@p' pom.xml | head -n1)"
        if [ -z "$api_version" ]; then
            api_version="$(grep -A2 '<artifactId>junit-jupiter-api</artifactId>' pom.xml \
                | sed -n 's@.*<version>\([0-9][0-9.]*\)</version>.*@\1@p' | head -n1)"
        fi
        [ -n "$api_version" ] || api_version=5.5.0
        sed -i '/<\/dependencies>/i \
        <dependency>\
            <groupId>org.junit.jupiter</groupId>\
            <artifactId>junit-jupiter-engine</artifactId>\
            <version>'"$api_version"'</version>\
            <scope>test</scope>\
        </dependency>' pom.xml
    fi
fi

if [ -f build.gradle ]; then
    # Maven Central dropped plaintext HTTP in 2020 and now answers every http://
    # request with "501 HTTPS Required". These build files predate that, so
    # dependency resolution fails outright and no test ever compiles.
    sed -i 's@http://repo\.maven\.apache\.org@https://repo.maven.apache.org@g; s@http://repo1\.maven\.org@https://repo1.maven.org@g; s@http://jcenter\.bintray\.com@https://repo.maven.apache.org/maven2@g' build.gradle

    # The api artifact alone gives no engine, so the JUnit platform starts and
    # reports "failed to execute tests" with zero testcases. Match every
    # configuration name these files use - testCompile and testRuntime are the
    # pre-Gradle-7 spellings and are what most of these PRs actually wrote.
    if ! grep -q "junit-jupiter-engine" build.gradle; then
        sed -i -E \
            "s@^([[:space:]]*)(testCompile|testImplementation|compile|implementation)[[:space:]]*\(?['\"]org\.junit\.jupiter:junit-jupiter-api:([0-9.]+)['\"]\)?@&\n\1testRuntimeOnly 'org.junit.jupiter:junit-jupiter-engine:\3'@" \
            build.gradle
    fi

    grep -q "useJUnitPlatform" build.gradle || printf '\ntest {\n    useJUnitPlatform()\n    ignoreFailures = true\n}\n' >> build.gradle
fi
""",
            ),
            File(
                ".",
                "emit_results.py",
                r'''#!/usr/bin/env python3
"""Emit one ``STATUS test.id`` line per testcase from the JUnit XML reports.

Reads the XML rather than scraping stdout: Maven's console output collapses a
class to one line, Gradle's depends on testLogging config, and neither reports
per-method status reliably. Both build systems write the same JUnit schema.
"""

import glob
import xml.etree.ElementTree as ET

PATTERNS = (
    "target/surefire-reports/TEST-*.xml",
    "build/test-results/test/TEST-*.xml",
    "**/target/surefire-reports/TEST-*.xml",
    "**/build/test-results/test/TEST-*.xml",
)

seen = set()
for pattern in PATTERNS:
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        for tc in root.iter("testcase"):
            cls = tc.get("classname") or ""
            name = tc.get("name") or ""
            if not cls or not name:
                continue
            tid = f"{cls}.{name}"
            if tid in seen:
                continue
            seen.add(tid)
            kids = {child.tag for child in tc}
            if "failure" in kids or "error" in kids:
                status = "FAILED"
            elif "skipped" in kids:
                status = "SKIPPED"
            else:
                status = "PASSED"
            print(f"{status} {tid}")
''',
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
# Shared test driver for all three stages.
#
# Exits 0 and prints nothing when the tree has no build system. That is the
# normal baseline state for most of these PRs (the build file arrives with the
# fix patch), and an empty result set is the correct, honest signal - the
# harness records zero tests for the stage rather than a spurious error.
set -eo pipefail

source /home/select_toolchain.sh
bash /home/fixup_build.sh

MVN_SKIPS="-Dcheckstyle.skip=true -Dpmd.skip=true -Dspotbugs.skip=true -Djacoco.skip=true"

GRADLE_FLAGS="--no-daemon --continue -Dorg.gradle.caching=false -Dorg.gradle.configuration-cache=false"

case "$BUILD_SYSTEM" in
    maven)
        mvn -B test -Dstyle.color=never -Dmaven.test.failure.ignore=true \
            $MVN_SKIPS 2>&1 || true
        ;;
    gradle)
        # Some of these build files declare sourceCompatibility 1.8 while the
        # source they ship is Java 9+ (PR 1613 uses the diamond operator on an
        # anonymous class). Probe the main compile at the declared level first and
        # only raise it when that genuinely fails, so rows that honestly build at
        # 1.8 keep compiling at 1.8. The declared level drives JDK choice, so
        # select_toolchain has to be re-sourced after a bump.
        if ! gradle compileJava $GRADLE_FLAGS >/dev/null 2>&1; then
            sed -i -E "s@(sourceCompatibility|targetCompatibility)([[:space:]]*=[[:space:]]*)['\"]?(1\.8|8)['\"]?@\1\2'11'@g" build.gradle
            source /home/select_toolchain.sh
            echo "run_tests: main compile failed at the declared source level, retrying at 11"
        fi
        gradle test $GRADLE_FLAGS 2>&1 || true
        ;;
    none)
        echo "run_tests: no build system in tree, no tests to run"
        exit 0
        ;;
esac

python3 /home/emit_results.py
""",
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true

# base.sha is NOT the commit the patches were cut against. GitHub's compare API
# diffs from the merge base, so that is where the patches apply. Resolve it here
# rather than baking in a host-computed value, so the image does not depend on
# the state of the host clone's refs.
git fetch --no-tags origin '+refs/pull/[[NUMBER]]/head:refs/msb/pr/[[NUMBER]]'
git fetch --no-tags origin [[SHA]] 2>/dev/null || git fetch --no-tags origin 2>/dev/null || true

MERGE_BASE="$(git merge-base [[SHA]] refs/msb/pr/[[NUMBER]])"
if [ -z "$MERGE_BASE" ]; then
    echo "prepare: could not resolve merge base for PR [[NUMBER]]" >&2
    exit 1
fi
echo "prepare: base.sha=[[SHA]] merge_base=$MERGE_BASE"
echo "$MERGE_BASE" > /home/merge_base.txt

git checkout -f "$MERGE_BASE"
bash /home/check_git_changes.sh

# Pre-warm the dependency cache so the graded stages do not need the network.
# Runs against the fully patched tree because that is the only state with a
# build file, then reverts - the hardening pass re-checks-out the merge base
# anyway, so nothing here may persist in the tree.
bash /home/apply_patches.sh /home/test.patch /home/fix.patch || true
if [ -f pom.xml ] || [ -f build.gradle ]; then
    source /home/select_toolchain.sh
    bash /home/fixup_build.sh || true
    java -version 2>&1 | head -1
    case "$BUILD_SYSTEM" in
        maven)
            mvn -B -q -Dstyle.color=never \
                -Dcheckstyle.skip=true -Dpmd.skip=true \
                -Dspotbugs.skip=true -Djacoco.skip=true \
                dependency:go-offline test-compile || true
            ;;
        gradle)
            gradle testClasses --no-daemon || true
            ;;
    esac
fi

git checkout -- .
git clean -xfd
bash /home/check_git_changes.sh
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha)
                .replace("[[NUMBER]]", str(number)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/apply_patches.sh /home/test.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
bash /home/apply_patches.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The hardening pass re-pins HEAD and strips every other ref so the fix
        # cannot be read back out of git history. It re-reads the merge base
        # that prepare.sh resolved, since that - not base.sha - is the commit
        # this instance is actually anchored to.
        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    MERGE_BASE="$(cat /home/merge_base.txt)"; \\
    git checkout --detach "$MERGE_BASE"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace refs/msb \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$MERGE_BASE"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace refs/msb)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("TheAlgorithms", "Java")
class TheAlgorithmsJava(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJavaImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        line_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$")
        for raw in test_log.splitlines():
            m = line_re.match(raw.strip())
            if not m:
                continue
            status, tid = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(tid)
            elif status == "FAILED":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

        # A test that both passed and failed across reports (retry, or the same
        # id emitted by two modules) counts as failed.
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
