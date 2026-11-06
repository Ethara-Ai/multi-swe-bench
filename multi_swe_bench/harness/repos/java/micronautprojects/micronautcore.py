from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    GRADLE_USER_HOME=/opt/gradle-home \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_HARDENING_BLOCK = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_SUBMODULE_SCRUB_BLOCK = """RUN if [ -f .gitmodules ]; then \\
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
    fi"""


_ORG = "micronaut-projects"
_REPO = "micronaut-core"

_JDK_IMAGES = {
    17: "gradle:8.14.3-jdk17",
    25: "gradle:9.8.2-jdk25",
}

_TEST_SOURCE_RE = re.compile(
    r"^(?P<dir>[^\s]+?)/src/test/(?:java|groovy|kotlin|python)/(?P<rel>[^\s]+)\.(?:java|groovy|kt|py)$"
)

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)

_INIT_SCRIPT = """def msbDirs = (System.getenv("MSB_TEST_DIRS") ?: "").split(",").findAll { it }
def msbClasses = (System.getenv("MSB_TEST_CLASSES") ?: "").split(",").findAll { it }
allprojects {
    def msbProjectPath = path
    tasks.withType(Test).configureEach { t ->
        t.ignoreFailures = true
        t.filter.failOnNoMatchingTests = false
        msbClasses.each { c ->
            t.filter.includeTestsMatching(c)
            t.filter.includeTestsMatching(c + '$*')
        }
        t.doFirst { t.maxParallelForks = 1 }
        t.afterTest { d, r ->
            println "MSB_TEST ${r.resultType} ${msbProjectPath} ${d.className} > ${d.name.replaceAll(/\\s+/, ' ')}"
        }
    }
}
rootProject { root ->
    root.tasks.register("msbTest") { agg ->
        root.allprojects.each { p ->
            def rel = root.projectDir.toPath().relativize(p.projectDir.toPath()).toString()
            if (msbDirs.contains(rel)) {
                agg.dependsOn(p.tasks.matching { it.name == "test" })
            }
        }
    }
}
"""

_GRADLE_FLAGS = (
    "--no-daemon --no-configuration-cache --no-build-cache --continue "
    "--console=plain --max-workers=4"
)

# Root build.gradle disables every Test task of these projects unless the Gradle
# property `python-ci` is set (the repo's Python CI runs `pythonCheck -Ppython-ci`).
_PYTHON_TEST_DIRS = {
    "inject-python",
    "inject-python-test",
    "context-python",
    "context-python-netty",
    "test-suite-dev-python",
}

_WRAPPER_PROPS = "gradle/wrapper/gradle-wrapper.properties"

_WRAPPER_URL_RE = re.compile(r"^\+distributionUrl=(\S+)$", re.M)

_MSB_RESULT_RE = re.compile(
    r"^MSB_TEST (?P<status>SUCCESS|FAILURE|SKIPPED) (?P<project>\S+) (?P<name>.+?)\s*$"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _jdk_major(pr: PullRequest) -> int:
    m = re.match(r"^(\d+)\.", pr.base.ref or "")
    if m and int(m.group(1)) < 5:
        return 17
    return 25


def _test_targets(pr: PullRequest) -> tuple[list[str], list[str]]:
    dirs: list[str] = []
    classes: list[str] = []
    for path in _DIFF_HEADER_RE.findall(pr.test_patch or ""):
        m = _TEST_SOURCE_RE.match(path)
        if not m:
            continue
        if m.group("dir") not in dirs:
            dirs.append(m.group("dir"))
        fqcn = m.group("rel").replace("/", ".")
        if fqcn not in classes:
            classes.append(fqcn)
    return dirs, classes


def _gradle_flags(pr: PullRequest) -> str:
    dirs, _ = _test_targets(pr)
    if any(d in _PYTHON_TEST_DIRS for d in dirs):
        return f"{_GRADLE_FLAGS} -Ppython-ci"
    return _GRADLE_FLAGS


def _patched_wrapper_urls(pr: PullRequest) -> list[str]:
    """Gradle distributions a patch switches the wrapper to (pre-fetched at build time)."""
    urls: list[str] = []
    for patch in (pr.test_patch or "", pr.fix_patch or ""):
        for section in re.split(r"(?=^diff --git )", patch, flags=re.M):
            if section.startswith(f"diff --git a/{_WRAPPER_PROPS} "):
                for url in _WRAPPER_URL_RE.findall(section):
                    if url not in urls:
                        urls.append(url)
    return urls


def _wrapper_prefetch(pr: PullRequest) -> str:
    lines = []
    for url in _patched_wrapper_urls(pr):
        lines.append(
            f"sed -i '/^distributionUrl=/d' {_WRAPPER_PROPS}\n"
            f"printf '%s\\n' 'distributionUrl={url}' >> {_WRAPPER_PROPS}\n"
            "for attempt in 1 2 3 4 5 6 7 8 9 10; do\n"
            "    if ./gradlew --no-daemon --version; then\n"
            "        break\n"
            "    fi\n"
            "    sleep $((attempt * 15))\n"
            "done\n"
            "./gradlew --no-daemon --version\n"
        )
    return "".join(lines)


def _script_env(pr: PullRequest) -> str:
    dirs, classes = _test_targets(pr)
    return (
        "export CI=true\n"
        "export TZ=UTC\n"
        "export LANG=C.UTF-8\n"
        f"export MSB_TEST_DIRS=\"{','.join(dirs)}\"\n"
        f"export MSB_TEST_CLASSES=\"{','.join(classes)}\""
    )


class MicronautCoreImageBase(Image):
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
        return _JDK_IMAGES[_jdk_major(self.pr)]

    def image_tag(self) -> str:
        return f"base-jdk{_jdk_major(self.pr)}"

    def workdir(self) -> str:
        return f"base-jdk{_jdk_major(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class MicronautCoreImageDefault(Image):
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
        return MicronautCoreImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        env = _script_env(self.pr)
        flags = _gradle_flags(self.pr)
        prefetch = _wrapper_prefetch(self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "GIT_TREE_CLEAN"
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail
{env}

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then
    git fetch --no-tags origin {sha}
fi
git checkout --detach {sha}
bash /home/check_git_changes.sh

mkdir -p "${{GRADLE_USER_HOME}}/init.d"
cat > "${{GRADLE_USER_HOME}}/init.d/mswebench-tests.gradle" <<'GRADLE_INIT'
{_INIT_SCRIPT}GRADLE_INIT

cat > "${{GRADLE_USER_HOME}}/gradle.properties" <<'GRADLE_PROPS'
systemProp.org.gradle.internal.http.connectionTimeout=120000
systemProp.org.gradle.internal.http.socketTimeout=120000
systemProp.org.gradle.internal.repository.max.retries=10
systemProp.org.gradle.internal.repository.initial.backoff=2000
GRADLE_PROPS

chmod +x ./gradlew
sed -i 's/^networkTimeout=.*/networkTimeout=120000/' gradle/wrapper/gradle-wrapper.properties
grep -q '^networkTimeout=' gradle/wrapper/gradle-wrapper.properties || echo 'networkTimeout=120000' >> gradle/wrapper/gradle-wrapper.properties
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if ./gradlew --no-daemon --version; then
        break
    fi
    sleep $((attempt * 15))
done
./gradlew --no-daemon --version
{prefetch}git checkout -- gradle/wrapper/gradle-wrapper.properties

for attempt in 1 2 3; do
    if MSB_TEST_CLASSES="mswebench.prepare.NoSuchTest" ./gradlew {flags} msbTest; then
        break
    fi
    sleep $((attempt * 60))
done
MSB_TEST_CLASSES="mswebench.prepare.NoSuchTest" ./gradlew {flags} msbTest

test -f "${{GRADLE_USER_HOME}}/init.d/mswebench-tests.gradle"
for d in ${{MSB_TEST_DIRS//,/ }}; do
    if [ -d "$d/src/test/java" ] || [ -d "$d/src/test/groovy" ] || [ -d "$d/src/test/kotlin" ]; then
        test -n "$(find "$d/build/classes" -path '*/test/*' -name '*.class' -print -quit 2>/dev/null)"
    fi
done
echo "DEPS_OK"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{env}

cd /home/{repo}

./gradlew {flags} msbTest 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

./gradlew {flags} msbTest 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{env}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

./gradlew {flags} msbTest 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_HARDENING_BLOCK}

{_SUBMODULE_SCRUB_BLOCK}
"""


@Instance.register(_ORG, _REPO)
class MicronautCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MicronautCoreImageDefault(self.pr, self._config)

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
        latest: dict[str, str] = {}

        for raw_line in test_log.splitlines():
            line = _ANSI_RE.sub("", raw_line).rstrip()
            m = _MSB_RESULT_RE.match(line)
            if not m:
                continue
            test_id = f"{m.group('project')} {m.group('name')}"
            latest[test_id] = m.group("status")

        passed_tests = {t for t, s in latest.items() if s == "SUCCESS"}
        failed_tests = {t for t, s in latest.items() if s == "FAILURE"}
        skipped_tests = {t for t, s in latest.items() if s == "SKIPPED"}

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
