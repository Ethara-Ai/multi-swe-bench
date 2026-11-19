from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

EMIT_TESTS = r"""
emit_tests() {
  find . -path '*/test-results/*' -name 'TEST-*.xml' 2>/dev/null | sort | while read -r f; do
    tr '\n' ' ' < "$f" | sed 's/<testcase/\n<testcase/g' | sed '1d' | awk '{ print }' | while IFS= read -r el; do
      cls=$(printf '%s' "$el" | sed -n 's/.*classname="\([^"]*\)".*/\1/p')
      meth=$(printf '%s' "$el" | sed -n 's/.*[^s]name="\([^"]*\)".*/\1/p')
      [ -z "$cls" ] && continue
      [ -z "$meth" ] && continue
      meth=$(printf '%s' "$meth" | sed -e 's/{[^}]*}//g' -e 's/\[[^][]*\]$//' -e 's/[[:space:]]*$//')
      status=PASS
      case "$el" in
        *"<failure"*|*"<error"*) status=FAIL ;;
        *"<skipped"*)            status=SKIP ;;
      esac
      printf '%s %s#%s\n' "$status" "$cls" "$meth"
    done
  done | sort -u | awk '
    { st=$1; name=$2;
      if (!(name in best)) best[name]=st;
      else {
        if (st=="FAIL" || best[name]=="FAIL") best[name]="FAIL";
        else if (st=="PASS" || best[name]=="PASS") best[name]="PASS";
      }
    }
    END { for (n in best) printf "MSWB_TEST: %s %s\n", best[n], n }
  ' | sort -k3
}
"""

INIT_GRADLE = r"""
mkdir -p /root/.gradle
cat > /root/.gradle/mswb-init.gradle <<'MSWB_INIT_GRADLE'
allprojects {
    tasks.withType(Test).configureEach {
        testLogging {
            events 'passed', 'failed', 'skipped'
            showStandardStreams = false
            exceptionFormat = 'short'
        }
        outputs.upToDateWhen { false }
        ignoreFailures = true
        maxParallelForks = 1
        forkEvery = 0
    }
}
gradle.taskGraph.whenReady { graph ->
    graph.allTasks.findAll { it.name.toLowerCase().contains('lint') }.each {
        it.enabled = false
    }
}
MSWB_INIT_GRADLE
"""

GRADLE_TEST = (
    "bash ./gradlew --no-daemon --offline"
    " --init-script /root/.gradle/mswb-init.gradle"
    " :exoplayer-library-sabr:testStstableDebugUnitTest"
    " --continue --console=plain --max-workers 2"
)


class ImageBase(Image):

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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-6202-to-6202"

    def workdir(self) -> str:
        return "base-6202-to-6202"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM --platform=linux/amd64 {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    CI=true \\
    ANDROID_SDK_ROOT="/opt/android-sdk" \\
    ANDROID_HOME="/opt/android-sdk" \\
    GRADLE_USER_HOME="/root/.gradle" \\
    GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx4g" \\
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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    sudo \\
    unzip \\
    wget \\
    openjdk-11-jdk-headless \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    if [ -z "${{HTTP_PROXY}}" ]; then unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; fi; \\
    mkdir -p /opt/android-sdk/cmdline-tools; \\
    curl -sSLo /tmp/cmdline-tools.zip \\
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip; \\
    unzip -qq /tmp/cmdline-tools.zip -d /opt/android-sdk/cmdline-tools; \\
    mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest; \\
    rm -f /tmp/cmdline-tools.zip; \\
    yes | /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --licenses > /dev/null 2>&1 || true

RUN set -eux; \\
    if [ -z "${{HTTP_PROXY}}" ]; then unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; fi; \\
    /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --install \\
        "platforms;android-34" \\
        "platforms;android-29" \\
        "platforms;android-27" \\
        "build-tools;34.0.0" \\
        "build-tools;30.0.3" \\
        "platform-tools" > /dev/null; \\
    test -d /opt/android-sdk/platforms/android-34; \\
    test -d /opt/android-sdk/build-tools/30.0.3

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):

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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

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
                "prepare.sh",
                f"""#!/bin/bash
set -euxo pipefail
{INIT_GRADLE}
cd /home/{repo}

git config core.autocrlf input
git config core.filemode false
git reset --hard
git clean -fdx -e SharedModules -e MediaServiceCore
bash /home/check_git_changes.sh
git checkout --detach {sha}
git submodule update --init --recursive
bash /home/check_git_changes.sh

export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)
export PATH="$JAVA_HOME/bin:$PATH"
for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
  if [ -z "${{!v}}" ]; then unset "$v"; fi
done
java -version

bash ./gradlew --no-daemon \\
    --init-script /root/.gradle/mswb-init.gradle \\
    :exoplayer-library-sabr:testStstableDebugUnitTest \\
    --continue --console=plain --max-workers 2 || true

bash ./gradlew --no-daemon --offline \\
    --init-script /root/.gradle/mswb-init.gradle \\
    :exoplayer-library-sabr:tasks --console=plain > /dev/null
test -d /opt/android-sdk/platforms/android-34

aapt2_bin="$(ls -1 /opt/android-sdk/build-tools/*/aapt2 2>/dev/null | head -1)"
test -n "$aapt2_bin"
"$aapt2_bin" version > /dev/null
find . -path '*/test-results/*' -name 'TEST-*.xml' | grep -q .
echo "DEPS_OK"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)
export PATH="$JAVA_HOME/bin:$PATH"
for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
  if [ -z "${{!v}}" ]; then unset "$v"; fi
done
{EMIT_TESTS}
cd /home/{repo}

find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

gradle_status=0
{GRADLE_TEST} || gradle_status=$?

emit_tests

if ! find . -path '*/test-results/*' -name 'TEST-*.xml' | grep -q .; then
  echo "run.sh: no test XML produced (gradle exit ${{gradle_status}})" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)
export PATH="$JAVA_HOME/bin:$PATH"
for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
  if [ -z "${{!v}}" ]; then unset "$v"; fi
done
{EMIT_TESTS}
cd /home/{repo}

find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

gradle_status=0
{GRADLE_TEST} || gradle_status=$?

emit_tests

if ! find . -path '*/test-results/*' -name 'TEST-*.xml' | grep -q .; then
  echo "test-run.sh: no test XML produced (gradle exit ${{gradle_status}})" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)
export PATH="$JAVA_HOME/bin:$PATH"
for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
  if [ -z "${{!v}}" ]; then unset "$v"; fi
done
{EMIT_TESTS}
cd /home/{repo}

find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

gradle_status=0
{GRADLE_TEST} || gradle_status=$?

emit_tests

if ! find . -path '*/test-results/*' -name 'TEST-*.xml' | grep -q .; then
  echo "fix-run.sh: no test XML produced (gradle exit ${{gradle_status}})" >&2
  exit 1
fi
""",
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

        global_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""FROM --platform=linux/amd64 {name}:{tag}
{global_env}
ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
{prepare_commands}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{self.clear_env}
"""


@Instance.register("yuliskov", "SmartTube_6202_to_6202")
class SMARTTUBE_6202_TO_6202(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        emitted = re.compile(r"^MSWB_TEST:\s+(PASS|FAIL|SKIP)\s+(\S+)\s*$")

        for line in clean_log.splitlines():
            match = emitted.match(line.strip())
            if not match:
                continue

            status = match.group(1)
            test_name = match.group(2)

            if status == "FAIL":
                failed_tests.add(test_name)
            elif status == "SKIP":
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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


Instance.register("yuliskov", "SmartTube")(SMARTTUBE_6202_TO_6202)
