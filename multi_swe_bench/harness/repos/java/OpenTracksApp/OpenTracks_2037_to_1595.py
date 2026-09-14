import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "ubuntu:22.04"
_BASE_TAG = "base-2037_to_1595"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
"""

_EMIT_RESULTS_PY = """import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

_ANN = re.compile(r"^\\s*@(Test|ParameterizedTest|RepeatedTest)\\b")
_METHOD = re.compile(r"\\bvoid\\s+([A-Za-z_$][A-Za-z0-9_$]*)\\s*\\(")
_PACKAGE = re.compile(r"^\\s*package\\s+([A-Za-z0-9_.]+)\\s*;")
_CLASS = re.compile(r"\\bclass\\s+([A-Za-z_$][A-Za-z0-9_$]*)")


def list_tests(paths):
    for path in paths:
        if not os.path.exists(path):
            continue
        pkg = None
        cls = None
        pending = False
        for line in open(path, encoding="utf-8", errors="ignore"):
            if pkg is None:
                found = _PACKAGE.match(line)
                if found:
                    pkg = found.group(1)
                    continue
            if cls is None:
                found = _CLASS.search(line)
                if found:
                    cls = found.group(1)
            if _ANN.match(line):
                pending = True
            elif pending:
                found = _METHOD.search(line)
                if found and pkg and cls:
                    print(pkg + "." + cls + " > " + found.group(1))
                    pending = False


def emit(repo, expected):
    results = {}
    for path in sorted(glob.glob(repo + "/**/test-results/**/TEST-*.xml", recursive=True)):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            cls = (case.get("classname") or "").strip()
            name = (case.get("name") or "").strip()
            if not cls or not name:
                continue
            status = "PASSED"
            for child in case:
                tag = child.tag.split("}")[-1].lower()
                if tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if tag == "skipped":
                    status = "SKIPPED"
                    break
            key = cls + " > " + name
            if status == "FAILED" or key not in results:
                results[key] = status
    if expected and os.path.exists(expected):
        for line in open(expected):
            key = line.strip()
            if key:
                results[key] = "FAILED"
    for key, status in sorted(results.items()):
        print("TESTCASE " + status + " " + key)
    sys.stderr.write("emit_results: %d test cases\\n" % len(results))


if sys.argv[1] == "--tests":
    list_tests(sys.argv[2:])
else:
    emit(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
"""

_PREPARE_SH = """#!/bin/bash
set -e

[ -n "$HTTP_PROXY" ] || unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

sed -i "s|throw new GradleException('VersionName could not be read:' + ignored.message)|return 'v0.0.0'|" build.gradle
sed -i "s|throw new GradleException('VersionCode could not be read:' + ignored.message)|return 1|" build.gradle

NS=$(sed -n "s/^[[:space:]]*namespace[[:space:]]*['\\"]\\([^'\\"]*\\)['\\"].*/\\1/p" build.gradle | head -1)
TDIR=src/test/java/$(echo "$NS" | tr . /)/test
mkdir -p src/main/res/raw src/test/resources "$TDIR"
cp -f src/androidTest/res/raw/* src/main/res/raw/

{
    echo "package $NS.test;"
    echo 'public final class R {'
    echo '    public static final class raw {'
    for f in src/androidTest/res/raw/*; do
        n=$(basename "$f")
        n=${n%%.*}
        echo "        public static final int $n = $NS.R.raw.$n;"
    done
    echo '    }'
    echo '}'
} > "$TDIR/R.java"

{
    echo "package $NS.test;"
    echo 'public final class BuildConfig {'
    echo "    public static final boolean DEBUG = $NS.BuildConfig.DEBUG;"
    echo "    public static final String APPLICATION_ID = $NS.BuildConfig.APPLICATION_ID;"
    echo "    public static final String BUILD_TYPE = $NS.BuildConfig.BUILD_TYPE;"
    echo "    public static final String VERSION_NAME = $NS.BuildConfig.VERSION_NAME;"
    echo "    public static final int VERSION_CODE = $NS.BuildConfig.VERSION_CODE;"
    echo '}'
} > "$TDIR/BuildConfig.java"

echo "application=$NS.TestApplication" > src/test/resources/robolectric.properties
if [ "$(dpkg --print-architecture)" != "amd64" ]; then
    echo 'graphicsMode=LEGACY' >> src/test/resources/robolectric.properties
    SDKV=$(sed -n "s/^[[:space:]]*compileSdk[[:space:]]*\\([0-9][0-9]*\\).*/\\1/p" build.gradle | head -1)
    AAPT2=$ANDROID_HOME/build-tools/$SDKV.0.0/aapt2
    test -x "$AAPT2"
    echo "android.aapt2FromMavenOverride=$AAPT2" >> gradle.properties
fi

cat >> build.gradle <<'EOG'

android {
    testOptions {
        unitTests {
            includeAndroidResources = true
            returnDefaultValues = true
            all { t ->
                t.maxHeapSize = '2g'
                t.ignoreFailures = true
            }
        }
    }
    sourceSets {
        test {
            java.srcDirs += 'src/androidTest/java'
        }
    }
}

dependencies {
    testImplementation 'junit:junit:4.13.2'
    testImplementation 'org.robolectric:robolectric:4.14.1'
    testImplementation 'androidx.test:core:1.6.1'
    testImplementation 'androidx.test.ext:junit:1.2.1'
    testImplementation 'androidx.test:rules:1.6.1'
    testImplementation 'androidx.test:runner:1.6.2'
    testImplementation 'androidx.test.espresso:espresso-core:3.6.1'
    testImplementation 'org.mockito:mockito-core:5.14.2'
}

afterEvaluate {
    def names = tasks.names.findAll { it ==~ /^test.*DebugUnitTest$/ }.sort()
    tasks.register('otTest') { dependsOn names.first() }
}
EOG

printf 'org.gradle.jvmargs=-Xmx2g -XX:MaxMetaspaceSize=768m\\norg.gradle.daemon=false\\norg.gradle.parallel=false\\nandroid.useAndroidX=true\\n' >> gradle.properties

WRAP=gradle/wrapper/gradle-wrapper.properties
DURL=$(sed -n 's/^distributionUrl=//p' "$WRAP" | tr -d '\\\\')
case "$DURL" in
    http*)
        ZIP=/home/$(basename "$DURL")
        wget -nv --tries=5 --waitretry=15 --timeout=120 -O "$ZIP" "$DURL"
        test -s "$ZIP"
        sed -i "s|^distributionUrl=.*|distributionUrl=file:$ZIP|" "$WRAP"
        ;;
esac
./gradlew --version --no-daemon --console=plain
rm -f "${ZIP:-}"

./gradlew otTest --no-daemon --console=plain --continue || true
python3 /home/emit_results.py /home/__REPO__ > /home/baseline-tests.txt
test -s /home/baseline-tests.txt
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

[ -n "${HTTP_PROXY:-}" ] || unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

cd /home/__REPO__

PATCHED=""
while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -n "$(git status --porcelain -- "$f")" ]; then PATCHED=1; fi
done < /home/patched_tests.txt
echo "===== test patch applied: $([ -n "$PATCHED" ] && echo yes || echo no) ====="

rm -rf build/test-results
./gradlew otTest --no-daemon --console=plain --continue --offline
echo "===== gradle exit: $? ====="

EXPECTED=""
if [ -n "$PATCHED" ] && ! ls build/test-results/*/TEST-*.xml > /dev/null 2>&1; then
    echo "===== no results, restoring baseline test sources ====="
    EXPECTED=/home/expected_tests.txt
    python3 /home/emit_results.py --tests $(cat /home/patched_tests.txt) > "$EXPECTED"
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        git checkout HEAD -- "$f" 2>/dev/null || rm -f "$f"
    done < /home/patched_tests.txt
    rm -rf build/test-results
    ./gradlew otTest --no-daemon --console=plain --continue --offline
    echo "===== gradle exit: $? ====="
fi

echo "===== test results ====="
python3 /home/emit_results.py /home/__REPO__ "$EXPECTED"
exit 0
"""

_RUN_SH = """#!/bin/bash
set -e
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git openjdk-17-jdk python3 unzip wget \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-17-openjdk

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk \\
    ANDROID_HOME=/opt/android-sdk \\
    ANDROID_SDK_ROOT=/opt/android-sdk \\
    GRADLE_USER_HOME=/home/gradle-home

RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \\
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O /tmp/clt.zip && \\
    unzip -q /tmp/clt.zip -d ${ANDROID_HOME}/cmdline-tools && \\
    mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest && \\
    rm /tmp/clt.zip

RUN set -e; \\
    [ -n "${HTTP_PROXY}" ] || unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; \\
    SDK=${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager; \\
    yes | $SDK --licenses > /dev/null; \\
    $SDK --install "platforms;android-34" "platforms;android-35" \\
    "build-tools;34.0.0" "build-tools;35.0.0" > /dev/null

RUN set -e; \\
    ARCH=$(dpkg --print-architecture); \\
    if [ "$ARCH" != "amd64" ]; then \\
        apt-get update; \\
        apt-get install -y --no-install-recommends file qemu-user-static; \\
        sed -i "s|^deb |deb [arch=$ARCH] |" /etc/apt/sources.list; \\
        for s in jammy jammy-updates jammy-security; do \\
            echo "deb [arch=amd64] http://archive.ubuntu.com/ubuntu $s main universe" >> /etc/apt/sources.list; \\
        done; \\
        dpkg --add-architecture amd64; \\
        apt-get update; \\
        mkdir -p /tmp/amd64libs /opt/x86_64-sysroot; \\
        (cd /tmp/amd64libs && apt-get download \\
            libc6:amd64 libstdc++6:amd64 libgcc-s1:amd64 zlib1g:amd64 && \\
            for d in *.deb; do dpkg -x "$d" /opt/x86_64-sysroot; done); \\
        rm -rf /tmp/amd64libs /var/lib/apt/lists/*; \\
        find /opt/x86_64-sysroot -type l | while read -r l; do \\
            t=$(readlink "$l"); \\
            case "$t" in /*) ln -sfn "/opt/x86_64-sysroot$t" "$l";; esac; \\
        done; \\
        test -e /opt/x86_64-sysroot/lib64/ld-linux-x86-64.so.2; \\
        for b in ${ANDROID_HOME}/build-tools/*/*; do \\
            if [ -f "$b" ] && file -b "$b" | grep -q "x86-64"; then \\
                mv "$b" "$b.x86_64"; \\
                printf '#!/bin/sh\\nexec /usr/bin/qemu-x86_64-static -L /opt/x86_64-sysroot "%s.x86_64" "$@"\\n' "$b" > "$b"; \\
                chmod 0755 "$b"; \\
            fi; \\
        done; \\
        for a in ${ANDROID_HOME}/build-tools/*/aapt2; do \\
            "$a" version; \\
        done; \\
    fi

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
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
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

def _materialize_annotations() -> None:
    import builtins
    import typing

    from multi_swe_bench.harness import report as _report
    from multi_swe_bench.harness import test_result as _test_result

    for cls in (_report.Report, _report.FinalReport, _test_result.TestResult):
        try:
            typing.get_type_hints(cls)
            continue
        except TypeError:
            pass
        shadowed = {
            name: cls.__dict__[name]
            for name in ("dict", "list", "set", "tuple", "type", "frozenset")
            if name in cls.__dict__ and hasattr(builtins, name)
        }
        if not shadowed:
            continue
        for name in shadowed:
            delattr(cls, name)
        try:
            typing.get_type_hints(cls)
        except Exception:
            pass
        finally:
            for name, value in shadowed.items():
                setattr(cls, name, value)


try:
    _materialize_annotations()
except Exception:
    pass


_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/.+?"? "?b/(.+?)"?$', re.M)
_TEST_PATH_RE = re.compile(r"(?:^|/)src/[A-Za-z]*[Tt]est[A-Za-z]*/java/.+\.java$")


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
    )


def _patched_tests(pr: PullRequest) -> str:
    files = []
    for path in _DIFF_FILE_RE.findall((pr.test_patch or "").replace("\r", "")):
        if _TEST_PATH_RE.search(path) and path not in files:
            files.append(path)
    return "".join(f + "\n" for f in files)


class OpenTracksImageBase2037To1595(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return _render(
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency()), self.pr
        )


class OpenTracksImageDefault2037To1595(Image):
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
        return OpenTracksImageBase2037To1595(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "patched_tests.txt", _patched_tests(self.pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        prepare_copy = "".join(f"COPY {f.name} /home/\n" for f in self.prepare_files())
        graded_copy = "".join(f"COPY {f.name} /home/\n" for f in self.graded_files())

        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("OpenTracksApp", "OpenTracks")
class OPENTRACKS_2037_TO_1595(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenTracksImageDefault2037To1595(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in test_log.replace("\r", "").split("\n"):
            match = _TESTCASE_RE.match(line.rstrip())
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("OpenTracksApp", "OpenTracks_2037_to_1595")(OPENTRACKS_2037_TO_1595)
