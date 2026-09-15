import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "Discord4J"


_CHECK_GIT_CHANGES_SH = f"""\
#!/bin/bash
set -e
cd /home/{REPO_DIR}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: uncommitted changes"
    git status --short
    exit 1
fi
echo "check_git_changes: no uncommitted changes"
"""


def _base_dockerfile(from_image: str, org: str) -> str:
    return f"""\
# syntax=docker/dockerfile:1.6

FROM {from_image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{REPO_DIR}.git"
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
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx2g" \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{REPO_DIR}" \\
      org.opencontainers.image.description="{org}/{REPO_DIR} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{REPO_DIR}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends ca-certificates git curl gnupg; \\
    rm -rf /var/lib/apt/lists/*

RUN git -C /home clone "${{REPO_URL}}" {REPO_DIR}

CMD ["/bin/bash"]
"""


def _pr_dockerfile(name: str, tag: str, sha: str, copy_commands: str) -> str:
    return f"""\
FROM {name}:{tag}

WORKDIR /home/{REPO_DIR}

RUN git reset --hard
RUN git checkout {sha}

{copy_commands}
RUN set -eux; \\
    cd /home/{REPO_DIR}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{REPO_DIR}/.gitmodules ]; then \\
        cd /home/{REPO_DIR}; \\
        git submodule foreach --recursive ' \\
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

RUN bash /home/prepare.sh
"""


def _prepare_sh(sha: str) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

find . -name 'build.gradle' -exec sed -i -E '/^apply plugin:.*gradle-git-properties/d; /^gitProperties[[:space:]]*[{{]/,/^[}}]/d; /gradle-git-properties/d' {{}} + || true

find . -name 'build.gradle' -exec sed -i 's|com\.sedmelluq:lavaplayer:1\.3\.47|com.github.sedmelluq:lavaplayer:1.3.78|g' {{}} + || true

sed -i 's/^discordJsonVersion=.*/discordJsonVersion=1.3.0/; s/^storesVersion=.*/storesVersion=3.1.0/' gradle.properties || true

mkdir -p /root/.gradle/init.d
cat > /root/.gradle/init.d/mswebench-repos.gradle <<'GRADLE_INIT'
allprojects {{
    buildscript {{
        repositories {{
            mavenCentral()
            gradlePluginPortal()
            maven {{ url 'https://jitpack.io' }}
        }}
    }}
    repositories {{
        mavenCentral()
        gradlePluginPortal()
        maven {{ url 'https://jitpack.io' }}
    }}
}}
settingsEvaluated {{ settings ->
    settings.pluginManagement.repositories {{
        mavenCentral()
        gradlePluginPortal()
        maven {{ url 'https://jitpack.io' }}
    }}
}}
GRADLE_INIT

chmod +x ./gradlew || true
./gradlew --no-daemon --version || true
./gradlew --no-daemon testClasses || true

./gradlew --no-daemon help
"""


_JUNIT_DUMP_TRAP = f"""\
_dump_junit_xml() {{
    echo "===== JUNIT XML DUMP BEGIN ====="
    find /home/{REPO_DIR} -path '*/build/test-results/test/*.xml' -print -exec cat {{}} \\; 2>/dev/null || true
    echo "===== JUNIT XML DUMP END ====="
}}
trap _dump_junit_xml EXIT
"""


def _run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_JUNIT_DUMP_TRAP}
cd /home/{REPO_DIR}
./gradlew --no-daemon --continue test
"""


def _test_run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_JUNIT_DUMP_TRAP}
cd /home/{REPO_DIR}

git ls-files > /tmp/pre_patch_files.txt
git apply --whitespace=nowarn /home/test.patch
git ls-files --others --exclude-standard > /tmp/new_files.txt
grep -E '(Test|Tests|Spec|IT)\\.(java|kt|scala|groovy)$' /tmp/new_files.txt > /tmp/new_test_files.txt || true

echo "--- New test files added by test.patch: ---" >&2
cat /tmp/new_test_files.txt >&2 || true

if ! ./gradlew --no-daemon --continue compileTestJava 2>&1 | tee /tmp/compile.log; then
    excluded=0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        bn=$(basename "$f")
        if grep -qE "(^|/)$f:[0-9]+.*error:" /tmp/compile.log \\
           || grep -qE "(^|/)$bn:[0-9]+.*error:" /tmp/compile.log; then
            echo "Excluding uncompileable new test file: $f" >&2
            rm -f "$f"
            excluded=$((excluded + 1))
        fi
    done < /tmp/new_test_files.txt
    echo "Excluded $excluded uncompileable test file(s) added by test.patch" >&2
fi

./gradlew --no-daemon --continue test
"""


def _fix_run_sh() -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

{_JUNIT_DUMP_TRAP}
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew --no-daemon --continue test
"""


def _pr_files(pr: PullRequest) -> list:
    sha = pr.base.sha
    return [
        File(".", "fix.patch", pr.fix_patch),
        File(".", "test.patch", pr.test_patch),
        File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
        File(".", "prepare.sh", _prepare_sh(sha)),
        File(".", "run.sh", _run_sh()),
        File(".", "test-run.sh", _test_run_sh()),
        File(".", "fix-run.sh", _fix_run_sh()),
    ]


def _pr_copy_commands(files: list) -> str:
    return "\n".join(f"COPY {f.name} /home/" for f in files) + "\n"


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

    def dependency(self) -> str:
        return "eclipse-temurin:11-jdk-jammy"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-jdk11"

    def workdir(self) -> str:
        return "base_jdk11"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return _base_dockerfile(self.dependency(), self.pr.org)


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        return _pr_files(self.pr)

    def dockerfile(self) -> str:
        image = self.dependency()
        return _pr_dockerfile(
            image.image_name(),
            image.image_tag(),
            self.pr.base.sha,
            _pr_copy_commands(self.files()),
        )


@Instance.register("Discord4J", "Discord4J")
class Discord4J(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", log)
        passed: set = set()
        failed: set = set()
        skipped: set = set()

        testcase_re = re.compile(
            r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL
        )
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        for m in testcase_re.finditer(clean):
            nm = name_re.search(m.group(1))
            cn = classname_re.search(m.group(1))
            if not nm or not cn:
                continue
            tid = f"{cn.group(1)}.{nm.group(1)}"
            inner = m.group(3) or ""
            if m.group(2) == "/>":
                passed.add(tid)
            elif "<failure" in inner or "<error" in inner:
                failed.add(tid)
            elif "<skipped" in inner:
                skipped.add(tid)
            else:
                passed.add(tid)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
