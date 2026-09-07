from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Dockerfile layout contract (matches repos/python/ros2/launch.py)
# ---------------------------------------------------------------------------
# BASE  : stops at `git clone` and then CMD ["/bin/bash"].  No checkout, no
#         history scrub.
# PR    : owns the commit pin AND the git stripping/hardening, with the scrub
#         placed LAST, after prepare.sh.
# prepare.sh : contains NO hardening and NO checkout.
#
# WHY THIS MATTERS HERE -- the previous layout had a real bug, not just a style
# difference.  image_tag() returns a single shared "base" for the whole repo,
# but the scrub was rendered INTO that base by
# DockerfileEnhancer._standardize_repo_fetch.  The scrub detaches at one
# ${BASE_COMMIT}, deletes every other ref and gc-prunes unreachable objects.
# Because Image.__hash__/__eq__ key on image_full_name(), all 5 PRs collapse to
# one base build -- so the shared base was pinned to whichever PR was built
# FIRST, and the other four PRs' base commits were pruned out of the object
# store.  Their `git checkout <sha>` would then fail against that base.
# Moving the scrub into the PR layer removes the hazard and makes the shared
# base correct.
#
# HOW THE LAYOUT IS ENFORCED WITHOUT TOUCHING image.py
# ----------------------------------------------------
# DockerfileEnhancer.enhance() has two early-outs:
#     if not isinstance(dep, str):      -> PR layer returned verbatim
#     if cls.SYNTAX_DIRECTIVE in raw:   -> base returned verbatim
# The BASE therefore emits `# syntax=docker/dockerfile:1.6` itself, and supplies
# the infrastructure block by reusing image.py's own constants so the proxy/CA
# wiring stays identical to every other repo.
#
# BUILD-ARG CONSEQUENCE
# ---------------------
# build_dataset.py passes REPO_URL/BASE_COMMIT only when dependency() is a str,
# i.e. only to the BASE.  The PR layer declares `ARG BASE_COMMIT="<sha>"` with
# the SHA as a literal default so the shared _HARDENING_BLOCK, which references
# ${BASE_COMMIT}, resolves there.

_MVN_WARMUP = "mvn install -DskipTests -Dsurefire.toolchain.version=8 || true"
_MVN_TEST = (
    "mvn test -Dmaven.test.skip=false -DfailIfNoTests=false "
    "-Dsurefire.toolchain.version=8"
)


class GuavaImageBase(Image):
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
        repo = self.pr.repo
        org = self.pr.org
        repo_url = f"https://github.com/{org}/{repo}.git"

        # Reuse image.py's own infrastructure constants so the proxy/CA/locale
        # wiring is identical to every other repo and cannot drift out of sync.
        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )
        labels = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM ubuntu:22.04

{build_args}

{DockerfileEnhancer._ENV_BLOCK}

{labels}

{DockerfileEnhancer._CERT_SYMLINKS}

# Static, repo-independent env. Kept as a Dockerfile ENV rather than an export
# in prepare.sh so it is still set when the graded run scripts execute. The JVM
# needs it for locale-stable surefire output.
ENV LC_ALL=C.UTF-8

WORKDIR /home/

# The JDK and maven MUST be installed in two separate steps, in this order.
#
# maven's dependency is `default-jre-headless | java8-runtime-headless`. In a
# single combined install apt picks the FIRST alternative, default-jre-headless,
# which on 22.04 resolves to openjdk-11-jre-headless -- so JDK 8 and JRE 11 end
# up configuring in the same dpkg transaction and the JRE 11 postinst fails:
#
#     Errors were encountered while processing:
#      openjdk-11-jre-headless:amd64
#     E: Sub-process /usr/bin/dpkg returned an error code (1)
#     -> apt-get ... returned a non-zero code: 100
#
# Installing openjdk-8-jdk first satisfies java8-runtime-headless, so the maven
# step then resolves the SECOND alternative and never pulls JDK 11 at all.
# Verified on ubuntu:22.04: openjdk-11 package count 0, mvn 3.6.3, java 1.8.0_502.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    openjdk-8-jdk \\
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \\
    maven \\
    && rm -rf /var/lib/apt/lists/*

# Arch-agnostic JAVA_HOME. The JDK lands in
# /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture), so a hardcoded
# -amd64 path would break the arm64 leg of a multi-arch build.
RUN ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) \\
    /usr/lib/jvm/java-8-openjdk
ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class GuavaImageDefault(Image):
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
        return GuavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

# NOTE: no checkout and no git stripping/hardening here by design.
#   * the reset + `git checkout ${{BASE_COMMIT}}` now run in the PR Dockerfile,
#     before this script;
#   * the scrub is the LAST thing the PR Dockerfile does, after this script.
#
# This also does NOT call check_git_changes.sh. That clean-tree assert runs in
# the PR Dockerfile immediately after the checkout, which is the only point
# where it is meaningful -- the maven warm-up below writes target/ trees into
# the working copy. What still matters is that the tree is parked on the right
# commit, so that is what gets asserted here.
cd /home/{pr.repo}
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
echo "prepare: HEAD pinned at {pr.base.sha}"

{warmup}
""".format(pr=self.pr, warmup=_MVN_WARMUP),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
mvn install -DskipTests -Dsurefire.toolchain.version=8 || true
mvn test -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.toolchain.version=8
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
mvn install -DskipTests -Dsurefire.toolchain.version=8 || true
mvn test -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.toolchain.version=8

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn install -DskipTests -Dsurefire.toolchain.version=8 || true
mvn test -Dmaven.test.skip=false -DfailIfNoTests=false -Dsurefire.toolchain.version=8

""".format(pr=self.pr),
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
        proxy_cleanup = ""

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
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        repo = self.pr.repo
        sha = self.pr.base.sha

        # check_git_changes.sh is COPY'd early and run right after the checkout,
        # so it is excluded from the bulk COPY to avoid a duplicate layer.
        copy_commands = "".join(
            f"COPY {f.name} /home/\n"
            for f in self.files()
            if f.name != "check_git_changes.sh"
        )

        # proxy_setup / proxy_cleanup render empty when no proxy is configured,
        # which is the normal case; they are kept so MITM-proxy builds still get
        # a ~/.m2/settings.xml. Stripped of blank padding so the layout matches
        # the reference structure exactly when they are empty.
        proxy_setup = proxy_setup.strip()
        proxy_cleanup = proxy_cleanup.strip()
        proxy_setup = f"\n{proxy_setup}\n" if proxy_setup else ""
        proxy_cleanup = f"\n{proxy_cleanup}\n" if proxy_cleanup else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

# Clean-tree assert, immediately after the checkout: this is the last moment the
# working tree is still pristine. prepare.sh below runs the maven warm-up, which
# writes target/ trees into the working copy, so running this any later risks
# reporting "Uncommitted changes" and failing the build.
COPY check_git_changes.sh /home/
RUN bash /home/check_git_changes.sh

WORKDIR /home/
{proxy_setup}
{copy_commands}
{prepare_commands}
{proxy_cleanup}
# Git stripping/hardening LAST, after prepare.sh.
#
# WORKDIR must be restored to the repo first: the block above left it at /home/,
# and every command in the scrub is a git operation that has to run inside the
# work tree.
#
# Running the scrub after prepare.sh is safe: none of its four assertions look
# at the working tree, only at git state (HEAD == BASE_COMMIT, no refs, no
# remotes, reachable-object count). Anything maven left in target/ is untracked
# and cannot affect them, nor block `git checkout --detach`.
WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("google", "guava")
class Guava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GuavaImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Handle both surefire 2.x and 3.x output formats:
        #   surefire 2.x: "Running <class>\nTests run: N, Failures: N, Errors: N, Skipped: N, Time elapsed: N.N sec"
        #   surefire 3.x: "[INFO] Running <class>\n[INFO] Tests run: N, Failures: N, Errors: N, Skipped: N, Time elapsed: N.N s -- in <class>"
        #   failure:      "... <<< FAILURE!"

        re_test_result = re.compile(
            r"(?:\[(?:INFO|ERROR)\]\s)?Running\s+(.+?)\s*\n"
            r"(?:(?!(?:\[(?:INFO|ERROR)\]\s)?Running\s).*\n)*?"
            r"(?:\[(?:INFO|ERROR)\]\s)?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            r",\s*Time elapsed:\s*[\d.]+\s*(?:sec|s)\b"
        )
        re_failure_marker = re.compile(
            r"(?:\[(?:INFO|ERROR)\]\s)?Running\s+(.+?)\s*\n"
            r"(?:(?!(?:\[(?:INFO|ERROR)\]\s)?Running\s).*\n)*?"
            r"(?:\[(?:INFO|ERROR)\]\s)?Tests run:\s*\d+,\s*Failures:\s*\d+,\s*Errors:\s*\d+,\s*Skipped:\s*\d+"
            r",\s*Time elapsed:\s*[\d.]+\s*(?:sec|s)\b[^\n]*<<<\s*FAILURE!"
        )

        # First pass: collect failures marked with <<< FAILURE!
        for match in re_failure_marker.finditer(test_log):
            test_name = match.group(1).strip()
            failed_tests.add(test_name)

        # Second pass: collect all test results
        for match in re_test_result.finditer(test_log):
            test_name = match.group(1).strip()
            tests_run = int(match.group(2))
            failures = int(match.group(3))
            errors = int(match.group(4))
            skipped = int(match.group(5))

            if test_name in failed_tests:
                continue

            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped != tests_run:
                passed_tests.add(test_name)
            elif skipped == tests_run and tests_run > 0:
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
