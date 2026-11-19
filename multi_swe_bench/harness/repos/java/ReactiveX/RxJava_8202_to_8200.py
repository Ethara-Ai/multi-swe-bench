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
      # Normalize the method name so it is IDENTICAL across stages. JUnit 5's
      # reporter appends an argument list ("foo()", "foo(TestInfo)") while the
      # JUnit 4 Vintage engine does not. PR 8202's fix patch removes
      # junit-vintage-engine, so the SAME test is reported as "foo" in the
      # baseline and "foo()" in the fix stage. Without stripping the argument
      # list those look like two different tests, which turns ~12k P2P tests
      # into bogus "lost" + "new" pairs and destroys the classification.
      meth=$(printf '%s' "$meth" | sed -e 's/([^()]*)$//' -e 's/{[^}]*}//g' -e 's/\[[^][]*\]$//' -e 's/[[:space:]]*$//')
      # Re-check: stripping a display name can leave the method name empty.
      [ -z "$meth" ] && continue
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
    }
}
gradle.taskGraph.whenReady { graph ->
    graph.allTasks.findAll {
        it.name.toLowerCase().contains('pmd') ||
        it.name.toLowerCase().contains('checkstyle') ||
        it.name.toLowerCase().contains('javadoc') ||
        it.name.toLowerCase().contains('jacoco')
    }.each {
        it.enabled = false
    }
}
MSWB_INIT_GRADLE
"""

GRADLE_TEST = (
    "./gradlew --no-daemon"
    " --init-script /root/.gradle/mswb-init.gradle"
    " test --continue --console=plain --max-workers 2"
)

# Maven Central / plugins.gradle.org return HTTP 429 (Too Many Requests) when
# several builds resolve dependencies from one IP at the same time. Dependency
# resolution is therefore retried with backoff; this is a transport-level retry
# only -- it never masks a test failure, because the gate below is a separate,
# non-tolerant command.
GRADLE_RETRY = r"""
gradle_retry() {
  attempt=1
  max_attempts=4
  while :; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "gradle_retry: giving up after ${attempt} attempts: $*" >&2
      return 1
    fi
    sleep_for=$((attempt * 45))
    echo "gradle_retry: attempt ${attempt} failed, sleeping ${sleep_for}s before retry" >&2
    sleep "$sleep_for"
    attempt=$((attempt + 1))
  done
}
"""


class RxJavaImageBase(Image):

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
        return "eclipse-temurin:26-jdk"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-8202-to-8200"

    def workdir(self) -> str:
        return "base-8202-to-8200"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class RxJavaImageDefault(Image):

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
        return RxJavaImageBase(self.pr, self._config)

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
{GRADLE_RETRY}
cd /home/{repo}

git config core.autocrlf input
git config core.filemode false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Warm the dependency cache with compileTestJava ONLY -- do not run the suite here.
# compileTestJava resolves every main + test dependency, which is the entire purpose
# of this warm-up, so the former full `test` run downloaded nothing extra while
# costing ~20-34 minutes (far worse under qemu emulation for multi-arch builds).
# It was also a correctness hazard: it populated TEST-*.xml inside the image layer,
# which is what the purge below exists to undo.
gradle_retry ./gradlew --no-daemon \\
    --init-script /root/.gradle/mswb-init.gradle \\
    compileTestJava --console=plain

# The warm-up above leaves TEST-*.xml on disk. If those survive into the image,
# emit_tests would report them in a stage where Gradle never ran the tests (e.g.
# a compile failure under the test patch), making the baseline's results look
# like the patched tree's. Remove them so every stage starts with no results.
find . -path '*/test-results/*' -name 'TEST-*.xml' -delete
echo "DEPS_OK"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{EMIT_TESTS}
{GRADLE_RETRY}
cd /home/{repo}

# Resolve dependencies first, with retry, so a transient HTTP 429 from Maven
# Central cannot be mistaken for a test failure. Tests then run exactly ONCE --
# retrying them would re-run the suite on a legitimate failure.
# Remove any stale results so emit_tests can only ever report tests that THIS
# stage actually ran.
find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

# Tolerant on purpose: compilation legitimately fails in the test stage (the
# test patch may need the fix), and that must not abort the stage. Its only job
# is to warm the dependency cache past a transient HTTP 429.
gradle_retry ./gradlew --no-daemon \\
    --init-script /root/.gradle/mswb-init.gradle \\
    testClasses --console=plain || true

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
{EMIT_TESTS}
{GRADLE_RETRY}
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# Resolve dependencies first, with retry, so a transient HTTP 429 from Maven
# Central cannot be mistaken for a test failure. Tests then run exactly ONCE --
# retrying them would re-run the suite on a legitimate failure.
# Remove any stale results so emit_tests can only ever report tests that THIS
# stage actually ran.
find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

# Tolerant on purpose: compilation legitimately fails in the test stage (the
# test patch may need the fix), and that must not abort the stage. Its only job
# is to warm the dependency cache past a transient HTTP 429.
gradle_retry ./gradlew --no-daemon \\
    --init-script /root/.gradle/mswb-init.gradle \\
    testClasses --console=plain || true

gradle_status=0
{GRADLE_TEST} || gradle_status=$?

emit_tests

# NOT fatal here, unlike run.sh/fix-run.sh. Under the test patch alone,
# compilation can legitimately fail (the new tests may need APIs the fix patch
# introduces), so producing no results is a valid observation: every test is
# then NONE in this stage, and report.py classifies via the baseline stage.
if ! find . -path '*/test-results/*' -name 'TEST-*.xml' | grep -q .; then
  echo "test-run.sh: no test XML produced (gradle exit ${{gradle_status}}); treating as empty test stage" >&2
fi
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
{EMIT_TESTS}
{GRADLE_RETRY}
cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# Resolve dependencies first, with retry, so a transient HTTP 429 from Maven
# Central cannot be mistaken for a test failure. Tests then run exactly ONCE --
# retrying them would re-run the suite on a legitimate failure.
# Remove any stale results so emit_tests can only ever report tests that THIS
# stage actually ran.
find . -path '*/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

# Tolerant on purpose: compilation legitimately fails in the test stage (the
# test patch may need the fix), and that must not abort the stage. Its only job
# is to warm the dependency cache past a transient HTTP 429.
gradle_retry ./gradlew --no-daemon \\
    --init-script /root/.gradle/mswb-init.gradle \\
    testClasses --console=plain || true

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

        return f"""FROM {name}:{tag}
{global_env}
ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
{prepare_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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


@Instance.register("ReactiveX", "RxJava_8202_to_8200")
class RXJAVA_8202_TO_8200(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RxJavaImageDefault(self.pr, self._config)

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


Instance.register("ReactiveX", "RxJava")(RXJAVA_8202_TO_8200)
