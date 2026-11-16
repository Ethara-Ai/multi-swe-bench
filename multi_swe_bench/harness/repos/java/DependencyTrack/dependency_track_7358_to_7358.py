import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

MVN_FLAGS = "-B -Dmaven.build.cache.enabled=false -Dspotless.check.skip=true -Denforcer.skip=true"


def _modules(patch: str) -> set[str]:
    modules = set()
    for path in re.findall(r"^diff --git a/(\S+)", patch, re.MULTILINE):
        match = re.match(r"(.+?)/src/(?:main|test)/(?:java|resources|proto)/", path)
        if match:
            modules.add(match.group(1))
    return modules


def _build_modules(pr: PullRequest) -> str:
    return ",".join(sorted(_modules(pr.fix_patch) | _modules(pr.test_patch)))


def _test_modules(pr: PullRequest) -> str:
    return ",".join(sorted(_modules(pr.test_patch)))


def _test_classes(pr: PullRequest) -> str:
    classes = set()
    for path in re.findall(r"^diff --git a/(\S+)", pr.test_patch, re.MULTILINE):
        match = re.search(r"/src/test/java/(?:.+/)?(\w+)\.java$", path)
        if match:
            classes.add(match.group(1))
    return ",".join(sorted(classes))


def _build_cmd(pr: PullRequest) -> str:
    return f"mvn {MVN_FLAGS} install -DskipTests -pl {_build_modules(pr)} -am"


def _gate_cmd(pr: PullRequest) -> str:
    return f"mvn {MVN_FLAGS} -o -q test-compile -pl {_build_modules(pr)}"


def _test_cmd(pr: PullRequest) -> str:
    classes = _test_classes(pr)
    test_flag = f" -Dtest={classes}" if classes else ""
    return (
        f"mvn {MVN_FLAGS} test -fae"
        " -Dsurefire.useFile=false -Dsurefire.reportFormat=plain"
        " -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        f" -pl {_test_modules(pr)}{test_flag}"
    )


class DEPENDENCY_TRACK_7358_TO_7358_ImageBase(Image):
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
        return "maven:3.9.11-eclipse-temurin-25"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            clone = f"COPY {repo} /home/{repo}"

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"{self.clear_env}\n\n" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

{env_block}ARG TARGETARCH
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV MAVEN_OPTS="-Xmx4g"

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class DEPENDENCY_TRACK_7358_TO_7358_ImageDefault(Image):
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
        return DEPENDENCY_TRACK_7358_TO_7358_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        build_cmd = _build_cmd(self.pr)
        gate_cmd = _gate_cmd(self.pr)
        test_cmd = _test_cmd(self.pr)
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
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{build_cmd} || true

{gate_cmd} && echo DEPS_OK
""".format(
                    repo=repo,
                    sha=self.pr.base.sha,
                    build_cmd=build_cmd,
                    gate_cmd=gate_cmd,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if [[BUILD]] > /tmp/build.log 2>&1; then
        cat /tmp/build.log
        break
    fi
    cat /tmp/build.log
    broken=$(sed -nE 's#^\[ERROR\] (/home/[[REPO]]/.+/src/test/[^:]+\.java):\[[0-9]+,[0-9]+\].*#\1#p' /tmp/build.log | sort -u)
    if [ -z "$broken" ]; then
        exit 1
    fi
    for file in $broken; do
        echo "EXCLUDED_UNCOMPILABLE_TEST $file"
        rm -f "$file"
    done
done

[[TEST]]
""".replace("[[REPO]]", repo)
                .replace("[[BUILD]]", build_cmd)
                .replace("[[TEST]]", test_cmd),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
""".format(repo=repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""".format(repo=repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
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

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{self.pr.base.sha}"
{proxy_setup}
{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}
{proxy_cleanup}{clear_block}"""


@Instance.register("DependencyTrack", "dependency_track_7358_to_7358")
class DEPENDENCY_TRACK_7358_TO_7358(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return DEPENDENCY_TRACK_7358_TO_7358_ImageDefault(self.pr, self._config)

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

        re_method = re.compile(
            r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?(\S.*?)\s+--\s+Time elapsed:\s*[\d.,]+\s*s(?:ec)?"
            r"(?:\s+<<<\s+(FAILURE|ERROR|SKIPPED)!)?\s*$"
        )
        re_skipped = re.compile(
            r"^(?:\[(?:INFO|WARNING|ERROR)\]\s+)?(\S+\.\S+)\s+skipped\s*$"
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


Instance.register("DependencyTrack", "dependency-track")(DEPENDENCY_TRACK_7358_TO_7358)
