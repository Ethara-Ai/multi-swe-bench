"""apache/druid PR era 7113..6221 (druid 0.12.3 - 0.14.0-incubating, JDK 8 + Maven)."""

import re
import textwrap
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_MIN = 6221
_ERA_MAX = 7113

_PR_DOCKERFILE = r"""FROM __BASE__

ARG BASE_COMMIT="__SHA__"

__COPY__
RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN bash /home/prepare.sh

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM ubuntu:22.04

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    MAVEN_OPTS=-Xmx4g \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    python3 \
    sudo \
    wget \
    openjdk-8-jdk \
    maven \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""



_NON_MODULE_DIRS = frozenset({
    ".mvn", ".github", ".gitignore", ".gitattributes", ".git",
    "codestyle", "dev", "docs", "licenses", "publications",
    "website", "hooks", ".editorconfig", ".licenserc.yaml",
})

_GROUPING_DIRS = frozenset({
    "cloud",
    "extensions",
    "extensions-contrib",
    "extensions-core",
})

def _extract_modules_from_patch(patch_text: str) -> set[str]:
    """Extract Maven module paths from a unified diff.

    For files under grouping directories (e.g. extensions-core/hdfs-storage/src/...),
    returns the two-segment module path (extensions-core/hdfs-storage) matching how
    the root pom.xml declares them.

    For files under direct reactor modules (e.g. processing/src/...),
    returns the single-segment name.

    Filters out non-module directories (.github, docs, codestyle, etc.).
    """
    modules = set()
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                path = parts[2].lstrip("a/")
                segments = path.split("/")
                if len(segments) < 2:
                    continue
                top = segments[0]
                if top in _NON_MODULE_DIRS:
                    continue
                if top in _GROUPING_DIRS:
                    if len(segments) >= 3:
                        modules.add(f"{segments[0]}/{segments[1]}")
                    continue
                modules.add(top)
    return modules


def _build_pl_flag(pr) -> str:
    all_modules = _extract_modules_from_patch(pr.fix_patch) | _extract_modules_from_patch(pr.test_patch)
    all_modules.discard("pom.xml")
    all_modules.discard("")
    if not all_modules:
        return ""
    return "-pl " + ",".join(sorted(all_modules)) + " -am"


def _extract_test_classes_from_patch(patch_text: str) -> set[str]:
    """Extract JUnit test class names from a unified diff.

    Returns bare class names (e.g. HdfsDataSegmentKillerTest) suitable for
    surefire -Dtest=, taken from any *.java file under a src/test/ tree.
    """
    classes = set()
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                if "/src/test/" not in path or not path.endswith(".java"):
                    continue
                classes.add(path.rsplit("/", 1)[-1][: -len(".java")])
    return classes


def _build_test_flag(pr) -> str:
    """Scope surefire to only the test classes this PR actually touches.

    Druid ships ~1704 test classes across the modules a typical patch pulls in
    via -am, and running all of them takes hours per pass -- four passes and two
    architectures makes a full-scope multi-arch build take days. Measured on this
    repo: compiling 2506 main + 1166 test sources takes ~1.7 min, while the tests
    take 20+ min for just 66 of 1066 classes in one module. Compilation is
    therefore cheap and is deliberately left at full scope (every module still
    compiles, so a patch that breaks compilation elsewhere is still caught);
    only test EXECUTION is narrowed.

    Trade-off, stated plainly: this shrinks the p2p baseline to the touched test
    classes. It does not affect f2p/n2p, which come from exactly these classes.
    Pairs with -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false so modules holding none of them do not fail.
    """
    classes = _extract_test_classes_from_patch(pr.test_patch)
    if not classes:
        return ""
    return "-Dtest=" + ",".join(f"{c}*" for c in sorted(classes))


class Druid7113To6221ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config
    def dependency(self) -> str | Image:
        return "ubuntu:22.04"
    def image_tag(self) -> str:
        return f"base-{_ERA_MAX}-to-{_ERA_MIN}"

    def workdir(self) -> str:
        return f"base-{_ERA_MAX}-to-{_ERA_MIN}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return (
            _BASE_DOCKERFILE
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class Druid7113To6221ImageDefault(Image):
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
        return Druid7113To6221ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pl_flag = _build_pl_flag(self.pr)
        test_flag = _build_test_flag(self.pr)
        suffix = " ".join(x for x in (pl_flag, test_flag) if x)
        mvn_prepare_base = "mvn clean test -fn -Dremoteresources.skip=true -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        mvn_prepare_cmd = f"{mvn_prepare_base} {suffix}" if suffix else mvn_prepare_base
        mvn_run_base = "mvn test -o -fn -Dremoteresources.skip=true -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        mvn_run_cmd = f"{mvn_run_base} {suffix}" if suffix else mvn_run_base
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

if [ ! -e /usr/lib/jvm/java-8-openjdk ]; then
  ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk
fi
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/{repo}
bash /home/check_git_changes.sh

{mvn_cmd} || true
""".format(repo=self.pr.repo, mvn_cmd=mvn_prepare_cmd),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

if [ ! -e /usr/lib/jvm/java-8-openjdk ]; then
  ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk
fi
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/{repo}
rc=0
{mvn_cmd} || rc=$?

find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true

if [ "$rc" -ne 0 ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
  echo "FATAL: maven exited $rc and produced no surefire reports" >&2
  exit "$rc"
fi
""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

if [ ! -e /usr/lib/jvm/java-8-openjdk ]; then
  ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk
fi
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
rc=0
{mvn_cmd} || rc=$?

find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true

if [ "$rc" -ne 0 ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
  echo "FATAL: maven exited $rc and produced no surefire reports" >&2
  exit "$rc"
fi
""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

if [ ! -e /usr/lib/jvm/java-8-openjdk ]; then
  ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk
fi
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk
export PATH="$JAVA_HOME/bin:$PATH"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rc=0
{mvn_cmd} || rc=$?

find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null || true

if [ "$rc" -ne 0 ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
  echo "FATAL: maven exited $rc and produced no surefire reports" >&2
  exit "$rc"
fi
""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(
            "COPY " + f.name + " /home/" + chr(10) for f in self.files()
        )
        return (
            _PR_DOCKERFILE
            .replace("__BASE__", f"{image.image_name()}:{image.image_tag()}")
            .replace("__SHA__", self.pr.base.sha)
            .replace("__COPY__", copy_commands)
        )


@Instance.register("apache", "druid")
@Instance.register("apache", "druid_7113_to_6221")
class DRUID_7113_TO_6221(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if not _ERA_MIN <= pr.number <= _ERA_MAX:
            raise ValueError(
                f"apache/druid PR #{pr.number} is outside era "
                f"{_ERA_MAX}..{_ERA_MIN}. It reached this config through the bare "
                f"'apache/druid' registration, which Instance.create() falls back to "
                f"when a dataset entry has no number_interval. Set number_interval on "
                f"the entry, or reorder imports in repos/java/apache/__init__.py so the "
                f"correct era owns the bare name."
            )
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Druid7113To6221ImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for m in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(m.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True
            name = f"{cls}.{meth}" if cls else meth
            body = m.group(3) or ""
            if "<failure" in body or "<error" in body:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif "<skipped" in body:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        if saw_xml:
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )
        re_pass_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            )
        ]
        re_fail_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*<<<\s*FAILURE!"
            )
        ]

        for re_fail_test in re_fail_tests:
            for m in re_fail_test.finditer(test_log):
                failed_tests.add(m.group(1))

        for re_pass_test in re_pass_tests:
            for m in re_pass_test.finditer(test_log):
                test_name = m.group(1)
                if test_name in failed_tests:
                    continue
                tests_run = int(m.group(2))
                failures = int(m.group(3))
                errors = int(m.group(4))
                skipped = int(m.group(5))
                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )