import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_UNIT_TEST_MODULES = frozenset({"lottie", "lottie-compose"})


# See lottie_android_jdk8.py for rationale.
_PATCH_BUILD_GRADLE_SH = r"""#!/bin/bash
# See lottie_android_jdk8.py for full doc.
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


class LottieAndroidJdk11ImageBase(Image):
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
        return "eclipse-temurin:11-jdk"

    def image_tag(self) -> str:
        return "base-jdk11"

    def workdir(self) -> str:
        return "base-jdk11"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
WORKDIR /home/

# Multiarch: enable amd64 so qemu can find /lib64/ld-linux-x86-64.so.2 when
# running AGP's x86_64 AAPT2 binaries on arm64 hosts. On arm64 base images,
# /etc/apt/sources.list points to ports.ubuntu.com (arm64-only). We restrict
# existing sources to arm64 and add archive.ubuntu.com for amd64.
RUN ARCH=$(dpkg --print-architecture) && \\
    if [ "$ARCH" = "arm64" ]; then \\
        sed -i 's|^deb |deb [arch=arm64] |g' /etc/apt/sources.list && \\
        echo "deb [arch=amd64] http://archive.ubuntu.com/ubuntu/ jammy main universe" > /etc/apt/sources.list.d/amd64.list && \\
        echo "deb [arch=amd64] http://archive.ubuntu.com/ubuntu/ jammy-updates main universe" >> /etc/apt/sources.list.d/amd64.list && \\
        echo "deb [arch=amd64] http://archive.ubuntu.com/ubuntu/ jammy-security main universe" >> /etc/apt/sources.list.d/amd64.list ; \\
    fi && \\
    dpkg --add-architecture amd64 && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    git curl unzip ca-certificates \\
    libc6:amd64 libstdc++6:amd64 zlib1g:amd64 \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/android-sdk/cmdline-tools && cd /opt/android-sdk/cmdline-tools && \\
    curl -sSLo cmdtools.zip https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip && \\
    unzip -q cmdtools.zip && rm cmdtools.zip && mv cmdline-tools latest && \\
    mkdir -p /opt/android-sdk/licenses && \\
    printf '\\n8933bad161af4178b1185d1a37fbf41ea5269c55\\n24333f8a63b6825ea9c5514f83c2829b004d1fee\\nd56f5187479451eabf01fb78af6dfcb131a6481e\\n' > /opt/android-sdk/licenses/android-sdk-license && \\
    printf '\\n84831b9409646a918e30573bab4c9c91346d8abd\\n504667f4c0de7af1a06de9f4b1727b84351f2910\\n' > /opt/android-sdk/licenses/android-sdk-preview-license && \\
    printf '\\n601085b94cd77f0b54ff86406957099ebe79c4d6\\n' > /opt/android-sdk/licenses/android-sdk-arm-dbt-license && \\
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && \\
    yes | sdkmanager --licenses >/dev/null 2>&1 || true && \\
    sdkmanager --install "platform-tools" "platforms;android-29" "platforms;android-30" "platforms;android-31" "build-tools;29.0.3" "build-tools;30.0.2" "build-tools;30.0.3" "build-tools;31.0.0" >/dev/null

{code}

{self.clear_env}

"""


class LottieAndroidJdk11ImageDefault(Image):
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
        return LottieAndroidJdk11ImageBase(self.pr, self._config)

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
echo "===== BEGIN TEST RESULTS ====="
find "$REPO_DIR" -path '*/build/test-results/test*UnitTest/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

bash /home/patch_build_gradle.sh /home/{repo} || true

timeout --kill-after=30 300 ./gradlew help --no-daemon || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
TASKS=$(bash /home/build_task_list.sh)
# AAPT2 single-threaded + no-daemon: avoids "AAPT2 Daemon startup failed" under qemu/arm64
timeout --kill-after=60 1800 ./gradlew $TASKS --no-daemon --continue \
  -Pandroid.aapt2.threads=1 \
  -Pandroid.experimental.aapt2.useWorkers=false || true
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ]; then
  echo "ERROR: no JUnit XML reports produced — gradle may have failed to start" >&2
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
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
TASKS=$(bash /home/build_task_list.sh)
# AAPT2 single-threaded + no-daemon: avoids "AAPT2 Daemon startup failed" under qemu/arm64
timeout --kill-after=60 1800 ./gradlew $TASKS --no-daemon --continue \
  -Pandroid.aapt2.threads=1 \
  -Pandroid.experimental.aapt2.useWorkers=false || true
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ]; then
  echo "ERROR: no JUnit XML reports produced — gradle may have failed to start" >&2
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
# Disable Gradle 7+/8.x configuration cache (caused BuildFlowService serialization
# failures on newer PRs). Older Gradle versions silently ignore unknown properties.
# Disable build cache to avoid stale state across runs.
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.unsafe.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
TASKS=$(bash /home/build_task_list.sh)
# AAPT2 single-threaded + no-daemon: avoids "AAPT2 Daemon startup failed" under qemu/arm64
timeout --kill-after=60 1800 ./gradlew $TASKS --no-daemon --continue \
  -Pandroid.aapt2.threads=1 \
  -Pandroid.experimental.aapt2.useWorkers=false || true
bash /home/print_test_results.sh
if [ -z "$(find /home/{repo} -path '*/build/test-results/test*UnitTest/TEST-*.xml' -print -quit 2>/dev/null)" ]; then
  echo "ERROR: no JUnit XML reports produced — gradle may have failed to start" >&2
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

{self.clear_env}

"""


@Instance.register("airbnb", "lottie_android_jdk11")
class LottieAndroidJdk11(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LottieAndroidJdk11ImageDefault(self.pr, self._config)

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
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
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

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
