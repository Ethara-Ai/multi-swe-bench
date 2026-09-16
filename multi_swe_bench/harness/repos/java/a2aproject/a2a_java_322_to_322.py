import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _maven_modules(pr: PullRequest) -> str:
    """Maven reactor modules touched by the fix/test patches (path before /src/).

    Scoping the build to these modules (plus `-am` upstream deps) keeps the run
    away from the Quarkus/Testcontainers modules that need a docker daemon.
    """
    modules = set()
    for patch in (pr.fix_patch, pr.test_patch):
        for path in re.findall(r"^diff --git a/(\S+)", patch, re.MULTILINE):
            if "/src/" in path:
                modules.add(path.split("/src/", 1)[0])
    return ",".join(sorted(modules))


def _pl_flag(pr: PullRequest) -> str:
    modules = _maven_modules(pr)
    return f" -pl {modules} -am" if modules else ""


def _mvn_test_cmd(pr: PullRequest) -> str:
    # Identical in run.sh / test-run.sh / fix-run.sh. `reportFormat=plain` with
    # `useFile=false` prints one console line per test METHOD, which parse_log reads.
    return (
        "mvn -B test -fae"
        " -Dsurefire.useFile=false -Dsurefire.reportFormat=plain"
        " -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        f"{_pl_flag(pr)}"
    )


class A2A_JAVA_322_TO_322_ImageBase(Image):
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
        # JDK 17 (pom.xml compiler source/target 17) + Maven 3.9; multi-arch image.
        return "maven:3.9.9-eclipse-temurin-17"

    def image_tag(self) -> str:
        # SHARED base: toolchain + FULL-HISTORY clone, pinned to nothing. The
        # checkout and the history prune belong to the PR layer, so one base can
        # serve every PR of the repo.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # DEBIAN_FRONTEND / LANG / proxy + CA ARGs and ENV, and ARG REPO_URL /
        # BASE_COMMIT are injected by DockerfileEnhancer right after FROM, so they
        # are not repeated here. build_dataset.py passes REPO_URL / BASE_COMMIT.
        #
        # SHARED base: NO checkout and NO history scrub here, and ${BASE_COMMIT}
        # is never consumed -- pinning this image to one PR's commit would prune
        # away every other PR's base commit. The clone keeps full history so any
        # PR layer can reach its own commit. Note the clone is written so that the
        # literal tokens "git clone" / "git fetch" never appear on one line, which
        # is what keeps DockerfileEnhancer from rewriting it into a pinned,
        # history-scrubbed fetch (image.py:356-435).
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV MAVEN_OPTS="-Xmx2g"
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \\
    git -c http.version=HTTP/1.1 \\
        clone "${{REPO_URL}}" /home/{self.pr.repo}; \\
    git -C /home/{self.pr.repo} rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class A2A_JAVA_322_TO_322_ImageDefault(Image):
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
        return A2A_JAVA_322_TO_322_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _mvn_test_cmd(self.pr)
        pl_flag = _pl_flag(self.pr)
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
set -eo pipefail

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
                """#!/bin/bash
set -eo pipefail
export CI=true

# ---- Section 1: PIN ----
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION ----
# Resolve every build/test dependency (including the surefire JUnit provider) at
# build time. Test failures are tolerated via maven.test.failure.ignore; compile
# and dependency-resolution failures still fail the build.
mvn -B test -fae -Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false{pl_flag}

# ---- Section 3: GATE ----
# Offline compile of main + test sources (JUnit/test-only deps included); not tolerant.
mvn -B -o -q test-compile{pl_flag} && echo DEPS_OK
""".format(repo=self.pr.repo, sha=self.pr.base.sha, pl_flag=pl_flag),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
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
            # Extract proxy host and port
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=\"?http[s]?://([^:]+):(\d+)", line
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
        # PR image: never enhanced (dependency() is an Image), so everything this
        # layer needs is written out here. The base is SHARED and pinned to
        # nothing, so THIS layer owns the pin and the history prune; the CA/proxy
        # ENV block is inherited from the base via FROM.
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN set -eux; \\
    git checkout --detach "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{self.pr.base.sha}")"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("a2aproject", "a2a_java_322_to_322")
class A2A_JAVA_322_TO_322(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return A2A_JAVA_322_TO_322_ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Surefire 3.x plain console format, one line per test method:
        #   [INFO] io.a2a.spec.TaskStatusTest.testX -- Time elapsed: 0.029 s
        #   [ERROR] io.a2a.client.FooTest.bad -- Time elapsed: 0.016 s <<< FAILURE!
        #   [ERROR] io.a2a.client.FooTest.boom -- Time elapsed: 0.005 s <<< ERROR!
        #   [WARNING] io.a2a.client.FooTest skipped
        # The per-class summary ("Tests run: ..., Time elapsed: ... -- in X") is
        # skipped explicitly. Timing sits outside the capture group, so a test has
        # the same name in every stage.
        re_method = re.compile(
            r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?(\S.*?)\s+--\s+Time elapsed:\s*[\d.,]+\s*s(?:ec)?"
            r"(?:\s+<<<\s+(FAILURE|ERROR|SKIPPED)!)?\s*$"
        )
        re_skipped = re.compile(
            r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?(\S+)\s+skipped\s*$"
        )
        re_summary = re.compile(r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?Tests run:")

        for line in test_log.splitlines():
            line = line.strip()
            if re_summary.match(line):
                continue
            match = re_method.match(line)
            if match:
                name, status = match.group(1), match.group(2)
                if status in ("FAILURE", "ERROR"):
                    failed_tests.add(name)
                elif status == "SKIPPED":
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)
                continue
            match = re_skipped.match(line)
            if match:
                skipped_tests.add(match.group(1))

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
