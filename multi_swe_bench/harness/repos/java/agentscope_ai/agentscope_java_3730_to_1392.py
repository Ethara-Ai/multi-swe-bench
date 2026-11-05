import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "agentscope-ai"
_REPO = "agentscope-java"
_INTERVAL = "agentscope_java_3730_to_1392"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_MAVEN_VERSION = "3.9.9"

_MAVEN_SHA512 = (
    "a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556"
    "bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b"
)

_MAVEN_URL = (
    "https://archive.apache.org/dist/maven/maven-3/"
    f"{_MAVEN_VERSION}/binaries/apache-maven-{_MAVEN_VERSION}-bin.tar.gz"
)

_MVN = "mvn -B -ntp"

_MVN_FLAGS = (
    "-fae "
    "-Dspotless.check.skip=true "
    "-Dspotless.apply.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.source.skip=true "
    "-Dskip.installnodenpm "
    "-Dskip.npm "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

_RESULTS_MARKER = "##TEST_RESULTS##"

_ROUTING_FLAG = "_agentscope_java_3730_to_1392_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org.lower() == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)


_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{apply}log="$(mktemp)"
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    status=0
    {mvn} clean test-compile {flags} 2>&1 | tee "$log" || status=$?
    if [ "$status" -eq 0 ]; then
        break
    fi
    broken="$(grep -E '^\\[ERROR\\] /home/{repo}/[^ :]*/src/test/[^ :]*\\.java:\\[[0-9]+' "$log" \\
        | grep -oE '/home/{repo}/[^ :]*/src/test/[^ :]*\\.java' \\
        | sort -u || true)"
    if [ -z "$broken" ]; then
        break
    fi
    while read -r f; do
        echo "Excluding uncompilable test source: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done <<< "$broken"
done
status=0
{mvn} test {flags} 2>&1 | tee "$log" || status=$?
results="$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' | sort)"
if [ -z "$results" ]; then
    echo "No test results found (maven exit code $status)"
    exit 1
fi
while read -r f; do
    echo "{marker} $f"
    cat "$f"
done <<< "$results"
"""


class AgentscopeJava3730To1392ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-${{TARGETARCH}} \\
    MAVEN_HOME=/opt/apache-maven-{_MAVEN_VERSION} \\
    MAVEN_OPTS="-Xmx2g" \\
    CI=true \\
    PATH=/opt/apache-maven-{_MAVEN_VERSION}/bin:$PATH

LABEL org.opencontainers.image.title="{self.pr.org}/{repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{repo}" \\
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
    git ca-certificates curl openjdk-17-jdk \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    curl -fsSL -o /tmp/maven.tar.gz "{_MAVEN_URL}"; \\
    echo "{_MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c -; \\
    tar -xzf /tmp/maven.tar.gz -C /opt; \\
    rm -f /tmp/maven.tar.gz; \\
    mvn -v

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class AgentscopeJava3730To1392ImageDefault(Image):
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
        return AgentscopeJava3730To1392ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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

mvn_settings=""
proxy="${{https_proxy:-${{HTTPS_PROXY:-${{http_proxy:-${{HTTP_PROXY:-}}}}}}}}"
if [ -n "$proxy" ]; then
    hostport="${{proxy#*://}}"
    hostport="${{hostport##*@}}"
    hostport="${{hostport%%/*}}"
    proxy_host="${{hostport%:*}}"
    proxy_port="${{hostport##*:}}"
    if [ "$proxy_port" = "$hostport" ]; then
        proxy_port=80
    fi
    settings_file="$(mktemp)"
    trap 'rm -f "$settings_file"' EXIT
    printf '<settings><proxies><proxy><id>http-proxy</id><active>true</active><protocol>http</protocol><host>%s</host><port>%s</port><nonProxyHosts>localhost|127.0.0.1</nonProxyHosts></proxy><proxy><id>https-proxy</id><active>true</active><protocol>https</protocol><host>%s</host><port>%s</port><nonProxyHosts>localhost|127.0.0.1</nonProxyHosts></proxy></proxies></settings>\\n' \\
        "$proxy_host" "$proxy_port" "$proxy_host" "$proxy_port" > "$settings_file"
    mvn_settings="-s $settings_file"
fi

{mvn} $mvn_settings clean test-compile {flags}

{mvn} $mvn_settings test {flags} || true

{mvn} $mvn_settings -o test-compile {flags}
echo DEPS_OK
""".format(repo=repo, sha=self.pr.base.sha, mvn=_MVN, flags=_MVN_FLAGS),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    repo=repo,
                    apply="",
                    mvn=_MVN,
                    flags=_MVN_FLAGS,
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_TEMPLATE.format(
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch\n",
                    mvn=_MVN,
                    flags=_MVN_FLAGS,
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_TEMPLATE.format(
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                    mvn=_MVN,
                    flags=_MVN_FLAGS,
                    marker=_RESULTS_MARKER,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {name}:{tag}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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
"""


@Instance.register(_ORG, _INTERVAL)
class AGENTSCOPE_JAVA_3730_TO_1392(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AgentscopeJava3730To1392ImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        chunks = re.split(
            rf"^{re.escape(_RESULTS_MARKER)} (\S+)\s*$", test_log, flags=re.MULTILINE
        )

        testcase_pattern = re.compile(
            r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL
        )
        name_pattern = re.compile(r'\bname="([^"]*)"')
        classname_pattern = re.compile(r'\bclassname="([^"]*)"')

        statuses = {}
        for path, content in zip(chunks[1::2], chunks[2::2]):
            module_path = path.partition("/target/surefire-reports/")[0]
            module = module_path.removeprefix("./").removeprefix(".") or _REPO

            for match in testcase_pattern.finditer(content):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                classname = classname_pattern.search(attrs)
                if not name or not classname:
                    continue

                test_name = f"{module}:{classname.group(1)}#{name.group(1)}"
                body = match.group(3) or ""

                if "<failure" in body or "<error" in body:
                    status = "failed"
                elif "<skipped" in body:
                    status = "skipped"
                else:
                    status = "passed"
                statuses.setdefault(test_name, set()).add(status)

        for test_name, seen in statuses.items():
            if "failed" in seen:
                failed_tests.add(test_name)
            elif "passed" in seen:
                passed_tests.add(test_name)
            else:
                skipped_tests.add(test_name)

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
