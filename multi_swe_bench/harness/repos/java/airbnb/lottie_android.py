
import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_UNIT_TEST_MODULES = frozenset({"lottie", "lottie-compose"})


_PATCH_BUILD_GRADLE_SH = r"""#!/bin/bash
# See lottie_android_2284_to_0.py for full doc.
set -e
REPO_DIR="$1"
[ -z "$REPO_DIR" ] && { echo "usage: $0 <repo_dir>" >&2; exit 1; }
cd "$REPO_DIR"

find . -name 'build.gradle' -not -path './build/*' -print0 | while IFS= read -r -d '' f; do
  sed -i '/^import org\.ajoberstar/d' "$f"
  sed -i '/^import com\.vanniktech\.maven\.publish/d' "$f"
  sed -i '/^import com\.jfrog\.bintray/d' "$f"
  sed -i '/^import com\.novoda\.bintray/d' "$f"
  sed -i -E '/^[[:space:]]*id[[:space:]]+["\x27]org\.ajoberstar\.grgit["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*id[[:space:]]+["\x27]com\.vanniktech\.maven\.publish["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*id[[:space:]]+["\x27]com\.jfrog\.bintray["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*id[[:space:]]+["\x27]com\.novoda\.bintray["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*apply[[:space:]]+plugin:[[:space:]]+["\x27]com\.vanniktech\.maven\.publish["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*apply[[:space:]]+plugin:[[:space:]]+["\x27]org\.ajoberstar\.grgit["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*apply[[:space:]]+from:[[:space:]]+["\x27]gradle-maven-push\.gradle["\x27]/d' "$f"
  sed -i -E '/^[[:space:]]*apply[[:space:]]+from:[[:space:]]+["\x27]gradle-mvn-push\.gradle["\x27]/d' "$f"
  sed -i 's|jcenter()|mavenCentral()\n    maven { url "https://maven.google.com" }|g' "$f"
  sed -i -E "/classpath[[:space:]]+[\"\x27]org\.ajoberstar/d" "$f"
  sed -i -E "/classpath[[:space:]]+[\"\x27]com\.jfrog\.bintray/d" "$f"
  sed -i -E "/classpath[[:space:]]+[\"\x27]com\.novoda\.bintray/d" "$f"
  sed -i -E "/classpath[[:space:]]+[\"\x27]com\.vanniktech.*maven.*publish/d" "$f"
  sed -i -E 's|^[[:space:]]*git[[:space:]]*=[[:space:]]*Grgit\.open.*|  git = null|' "$f"
  sed -i -E 's|^[[:space:]]*gitSha[[:space:]]*=[[:space:]]*git\..*|  gitSha = "unknown"|' "$f"
  sed -i -E 's|^[[:space:]]*gitBranch[[:space:]]*=[[:space:]]*git\..*|  gitBranch = "unknown"|' "$f"
  sed -i -E 's|^([[:space:]]*)([^/].*Grgit.*)$|\1// \2|' "$f"
  awk '
    BEGIN { skip = 0; depth = 0 }
    {
      if (skip == 0 && $0 ~ /^[[:space:]]*(mavenPublish|mavenPublishing)[[:space:]]*\{/) {
        skip = 1
        line = $0
        n_open = gsub(/\{/, "{", line)
        n_close = gsub(/\}/, "}", line)
        depth = n_open - n_close
        next
      }
      if (skip == 1) {
        line = $0
        n_open = gsub(/\{/, "{", line)
        n_close = gsub(/\}/, "}", line)
        depth += n_open - n_close
        if (depth <= 0) { skip = 0 }
        next
      }
      print
    }
  ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done

for s in settings.gradle settings.gradle.kts; do
  [ -f "$s" ] || continue
  awk '
    function should_skip(name) {
      sub(/^:/, "", name)
      return name == "LottieSample" || name == "sample" \
          || name == "snapshot-tests" || name == "sample-compose" \
          || name == "sample-compose-benchmark" \
          || name == "LottieSample-compose" || name == "sample-wear" \
          || name == "issue-repro"
    }
    /^[[:space:]]*include[[:space:]]/ {
      match($0, /^[[:space:]]*include[[:space:]]+/)
      prefix = substr($0, 1, RLENGTH)
      rest = substr($0, RLENGTH + 1)
      out = ""
      kept = 0
      while (match(rest, /[\"\047][^\"\047]+[\"\047]/)) {
        arg = substr(rest, RSTART, RLENGTH)
        inner = substr(arg, 2, length(arg) - 2)
        if (!should_skip(inner)) {
          if (out != "") out = out ", "
          out = out arg
          kept++
        }
        rest = substr(rest, RSTART + RLENGTH)
      }
      if (kept > 0) print prefix out
      else print "// " $0
      next
    }
    { print }
  ' "$s" > "$s.tmp" && mv "$s.tmp" "$s"
done

echo "patch_build_gradle: done"
"""


def _extract_modules_from_patch(patch_text: str) -> set[str]:
    modules = set()
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                segments = path.split("/")
                if len(segments) < 2:
                    continue
                top = segments[0]
                if top in _UNIT_TEST_MODULES:
                    modules.add(top)
    return modules


def _build_candidate_modules(pr: PullRequest) -> str:
    all_modules = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(pr.test_patch)
    if not all_modules:
        return "lottie"
    return " ".join(sorted(all_modules))


class _ImageBase(Image):
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
        return "eclipse-temurin:17-jdk"

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
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl unzip ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        candidate_modules = _build_candidate_modules(self.pr)
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

""",
            ),
            File(
                ".",
                "build_task_list.sh",
                """#!/bin/bash
CANDIDATES="{candidates}"
REPO_DIR="/home/{repo}"
TASKS=""
for mod in $CANDIDATES; do
  if [ -f "$REPO_DIR/$mod/build.gradle" ] || [ -f "$REPO_DIR/$mod/build.gradle.kts" ]; then
    TASKS="$TASKS :$mod:testDebugUnitTest"
  fi
done
if [ -z "$TASKS" ]; then
  TASKS=":lottie:testDebugUnitTest"
fi
echo "$TASKS"
""".format(candidates=candidate_modules, repo=self.pr.repo),
            ),
            File(
                ".",
                "print_test_results.sh",
                """#!/bin/bash
REPO_DIR="/home/{repo}"
QLIST=/tmp/quarantine_list.txt
echo "===== BEGIN TEST RESULTS ====="
find "$REPO_DIR" -path '*/build/test-results/test*UnitTest/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null
if [ -s "$QLIST" ]; then
  echo '<testsuite name="quarantined-uncompilable">'
  while IFS=$'\\t' read -r c m; do
    if [ -n "$c" ] && [ -n "$m" ]; then
      printf '<testcase name="%s" classname="%s"><failure message="test sources failed to compile before fix patch"></failure></testcase>\\n' "$m" "$c"
    fi
  done < "$QLIST"
  echo '</testsuite>'
fi
echo ""
echo "===== END TEST RESULTS ====="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "enumerate_tests.sh",
                """#!/bin/bash
f="$1"
[ -f "$f" ] || exit 0
pkg=$(sed -n 's/^[[:space:]]*package[[:space:]]\\{1,\\}\\([A-Za-z0-9_.]*\\).*/\\1/p' "$f" | head -1)
cls=$(basename "$f")
cls="${cls%.*}"
if [ -n "$pkg" ]; then
  fq="$pkg.$cls"
else
  fq="$cls"
fi
awk -v fq="$fq" '
  {
    if ($0 ~ /@Test/) { pending = 1 }
    if (pending && match($0, /(void|fun)[ \\t]+[A-Za-z0-9_]+[ \\t]*\\(/)) {
      s = substr($0, RSTART, RLENGTH)
      sub(/^(void|fun)[ \\t]+/, "", s)
      sub(/[ \\t]*\\($/, "", s)
      if (s != "") { print fq "\\t" s }
      pending = 0
    }
  }
' "$f" | sort -u
exit 0
""",
            ),
            File(
                ".",
                "run_gradle.sh",
                """#!/bin/bash
REPO_DIR="/home/{repo}"
QDIR=/tmp/quarantine
QLIST=/tmp/quarantine_list.txt
rm -rf "$QDIR"
mkdir -p "$QDIR"
: > "$QLIST"

cd "$REPO_DIR"
TASKS=$(bash /home/build_task_list.sh)

has_xml() {{
  [ -n "$(find "$REPO_DIR" -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ]
}}

attempt=0
while [ "$attempt" -lt 6 ]; do
  attempt=$((attempt + 1))
  timeout --kill-after=60 1800 ./gradlew $TASKS --no-daemon --continue -Pandroid.aapt2.threads=1 -Pandroid.experimental.aapt2.useWorkers=false > /tmp/gradle_attempt.log 2>&1 || true
  cat /tmp/gradle_attempt.log
  if has_xml; then
    break
  fi
  BAD=$( {{ sed -n 's#^\\(/home/[^ :]*/src/test/[^ :]*\\.java\\):[0-9][0-9]*: error:.*#\\1#p' /tmp/gradle_attempt.log; sed -n 's#^e: file://\\(/home/[^ :]*/src/test/[^ :]*\\.kt\\):[0-9][0-9]*:[0-9][0-9]* .*#\\1#p' /tmp/gradle_attempt.log; }} | sort -u )
  NEW=""
  for f in $BAD; do
    if [ -f "$f" ]; then
      NEW="$NEW $f"
    fi
  done
  if [ -z "$(echo "$NEW" | tr -d ' ')" ]; then
    break
  fi
  for f in $NEW; do
    bash /home/enumerate_tests.sh "$f" >> "$QLIST"
    mkdir -p "$QDIR/$(dirname "${{f#/}}")"
    mv "$f" "$QDIR/${{f#/}}"
    echo "QUARANTINED_UNCOMPILABLE_TEST_FILE: $f" >&2
  done
done
exit 0
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "patch_build_gradle.sh",
                _PATCH_BUILD_GRADLE_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export LC_ALL=C.UTF-8
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools
export GRADLE_OPTS="$GRADLE_OPTS -Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

ARCH=$(dpkg --print-architecture)
CODENAME="$(. /etc/os-release 2>/dev/null && echo "$VERSION_CODENAME")"
if [ "$ARCH" = "arm64" ]; then
  if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    sed -i '/^Architectures:/d' /etc/apt/sources.list.d/ubuntu.sources
    sed -i 's|^Types: deb.*|&\\nArchitectures: arm64|' /etc/apt/sources.list.d/ubuntu.sources
  fi
  if [ -s /etc/apt/sources.list ]; then
    sed -i 's|^deb |deb [arch=arm64] |g' /etc/apt/sources.list
  fi
  if [ -n "$CODENAME" ]; then
    printf 'deb [arch=amd64] http://archive.ubuntu.com/ubuntu/ %s main universe\\ndeb [arch=amd64] http://archive.ubuntu.com/ubuntu/ %s-updates main universe\\n' "$CODENAME" "$CODENAME" > /etc/apt/sources.list.d/amd64.list
    dpkg --add-architecture amd64
  fi
fi
apt-get update || true
apt-get install -y --no-install-recommends git curl unzip ca-certificates
if [ "$ARCH" = "arm64" ]; then
  apt-get install -y --no-install-recommends libc6:amd64 libstdc++6:amd64 zlib1g:amd64 || echo "WARNING: amd64 cross-libs unavailable on arm64"
fi
rm -rf /var/lib/apt/lists/*

if [ ! -x /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager ]; then
  mkdir -p /opt/android-sdk/cmdline-tools
  cd /opt/android-sdk/cmdline-tools
  curl -sSLo cmdtools.zip https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
  unzip -q cmdtools.zip && rm cmdtools.zip && mv cmdline-tools latest
fi
mkdir -p /opt/android-sdk/licenses
printf '\\n8933bad161af4178b1185d1a37fbf41ea5269c55\\n24333f8a63b6825ea9c5514f83c2829b004d1fee\\nd56f5187479451eabf01fb78af6dfcb131a6481e\\n' > /opt/android-sdk/licenses/android-sdk-license
printf '\\n84831b9409646a918e30573bab4c9c91346d8abd\\n504667f4c0de7af1a06de9f4b1727b84351f2910\\n' > /opt/android-sdk/licenses/android-sdk-preview-license
printf '\\n601085b94cd77f0b54ff86406957099ebe79c4d6\\n' > /opt/android-sdk/licenses/android-sdk-arm-dbt-license
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager --install "platform-tools" "platforms;android-33" "platforms;android-34" "platforms;android-36" "build-tools;33.0.2" "build-tools;34.0.0" "build-tools;36.0.0" >/dev/null

# ---------- Section 1: PIN the tree to this PR's base commit ----------
# The shared base kept full history and is pinned to nothing, so this layer owns the
# pin and the commit is guaranteed reachable -- the `git fetch --depth=1` fallback
# that used to live here is gone, and with it a network remote inside the graded
# image. It only ever existed because the base was being scrubbed to one arbitrary
# PR's ancestry.
#
# --detach is required, not stylistic: the prune block in the PR Dockerfile ASSERTS
# HEAD == BASE_COMMIT rather than re-establishing it, then deletes every ref.
cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION ----------
# From here the tree is INTENTIONALLY dirty -- patch_build_gradle.sh rewrites tracked
# build files to strip jcenter/bintray/Grgit and the sample modules. No clean-tree
# assertion may appear below this line.
bash /home/patch_build_gradle.sh /home/{repo}

# Pre-warm the Gradle distribution. Tolerated: a warm cache is an optimisation, and
# the hard gate below re-checks the same machinery non-tolerantly.
timeout --kill-after=30 300 ./gradlew help --no-daemon || true

# ---------- Section 3: HARD GATE ----------
# Non-tolerant, last, and deep enough to catch a partial SDK install: compiling the
# graded module's unit-test sources exercises the JDK, the wrapper, the Android SDK
# platforms and the build-tools in one step. A half-provisioned image must fail HERE,
# at build time, rather than three stages later behind a parse_log that returns 0/0/0
# and trips Report.check() rule 1 (report.py:204) only after three full builds.
test -d /opt/android-sdk/platforms/android-34
timeout --kill-after=60 1800 ./gradlew :lottie:compileDebugUnitTestJavaWithJavac \\
  --no-daemon -Dorg.gradle.configuration-cache=false
echo DEPS_OK
""".format(
                    repo=self.pr.repo, sha=self.pr.base.sha, org=self.pr.org
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools
export GRADLE_OPTS="$GRADLE_OPTS -Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
bash /home/run_gradle.sh
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ] && [ ! -s /tmp/quarantine_list.txt ]; then
  echo "ERROR: no JUnit XML reports produced and nothing salvageable" >&2
  exit 1
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools
export GRADLE_OPTS="$GRADLE_OPTS -Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
# --exclude='*.zip' is required, not defensive. This dataset's test.patch carries a
# TRUNCATED binary hunk for a snapshot-test asset -- a "Binary files ... differ"
# header with no `GIT binary patch` payload -- and `git apply` is atomic, so it
# rejects the WHOLE patch ("cannot apply binary patch ... without full index line")
# and the Java test file never lands either. The excluded asset is consumed only by
# the `snapshot-tests` module, which is not in _UNIT_TEST_MODULES and is stripped
# from settings.gradle by patch_build_gradle.sh, so nothing graded reads it.
#
# The trailing `|| true` was removed deliberately: without it an unapplied test.patch
# made this stage identical to the baseline, so no test transitioned !PASS -> PASS and
# Report.check() rule 3 (report.py:217) discarded the instance -- silently, after a
# full Android build had run three times.
git apply --whitespace=nowarn --exclude='*.zip' /home/test.patch
bash /home/run_gradle.sh
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ] && [ ! -s /tmp/quarantine_list.txt ]; then
  echo "ERROR: no JUnit XML reports produced and nothing salvageable" >&2
  exit 1
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export ANDROID_HOME=/opt/android-sdk
export ANDROID_SDK_ROOT=/opt/android-sdk
export PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools
export GRADLE_OPTS="$GRADLE_OPTS -Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
# test.patch FIRST, then fix.patch -- the fix must land on a tree that already carries
# the new tests. Same --exclude and same no-`|| true` reasoning as test-run.sh;
# fix.patch itself touches only lottie/src/main/java/** and carries no binary hunk,
# so the exclusion is a no-op there and just keeps the two scripts symmetric.
git apply --whitespace=nowarn --exclude='*.zip' /home/test.patch /home/fix.patch
bash /home/run_gradle.sh
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ] && [ ! -s /tmp/quarantine_list.txt ]; then
  echo "ERROR: no JUnit XML reports produced and nothing salvageable" >&2
  exit 1
fi
""".format(repo=self.pr.repo),
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
                ENV GRADLE_OPTS="${{GRADLE_OPTS}} -Dhttp.proxyHost={proxy_host} -Dhttp.proxyPort={proxy_port} -Dhttps.proxyHost={proxy_host} -Dhttps.proxyPort={proxy_port}"
                """
                )

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}
{prepare_commands}

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
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

{self.clear_env}
"""


@Instance.register("airbnb", "lottie-android")
class LottieAndroid(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            ansi_escape_pattern = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        testcase_re = re.compile(
            r'<testcase\b[^>]*?\bname="([^"]+)"[^>]*?\bclassname="([^"]+)"[^>]*?'
            r'(/>|>(.*?)</testcase>)',
            re.DOTALL,
        )

        for m in testcase_re.finditer(test_log):
            name = m.group(1)
            classname = m.group(2)
            closing = m.group(3)
            inner = m.group(4) or ""
            test_id = f"{classname}.{name}"

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

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
