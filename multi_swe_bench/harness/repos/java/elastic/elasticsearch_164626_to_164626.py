import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ES_USER = "esuser"
TEST_SEED = "DEADBEEFCAFEBABE"
GRADLE = "./gradlew --console=plain"


def _test_sources(pr: PullRequest) -> list[tuple[str, str, str]]:
    sources = []
    for path in re.findall(r"^diff --git a/(\S+)", pr.test_patch, re.MULTILINE):
        match = re.match(r"(.+?)/src/(\w*[Tt]est\w*)/java/(.+)\.java$", path)
        if match and re.search(r"(Tests?|IT)$", match.group(3)):
            project = ":" + match.group(1).replace("/", ":")
            sources.append((project, match.group(2), match.group(3).replace("/", ".")))
    return sorted(set(sources))


def _compile_tasks(pr: PullRequest) -> str:
    tasks = set()
    for project, source_set, _ in _test_sources(pr):
        suffix = "testClasses" if source_set == "test" else f"{source_set}Classes"
        tasks.add(f"{project}:{suffix}")
    return " ".join(sorted(tasks))


def _test_tasks(pr: PullRequest) -> str:
    grouped: dict[str, list[str]] = {}
    for project, source_set, fqcn in _test_sources(pr):
        grouped.setdefault(f"{project}:{source_set}", []).append(fqcn)
    return " ".join(
        task + "".join(f" --tests {fqcn}" for fqcn in classes)
        for task, classes in sorted(grouped.items())
    )


def _result_dirs(pr: PullRequest) -> str:
    dirs = {
        f"{project[1:].replace(':', '/')}/build/test-results/{source_set}"
        for project, source_set, _ in _test_sources(pr)
    }
    return " ".join(sorted(dirs))


class ELASTICSEARCH_164626_TO_164626_ImageBase(Image):
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
        return "eclipse-temurin:21-jdk"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

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

ENV RUNTIME_JAVA_HOME=/opt/java/openjdk

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
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*'

RUN useradd -m -s /bin/bash {ES_USER} && \\
    mkdir -p /home/{repo} && \\
    chown {ES_USER}:{ES_USER} /home/{repo}

{clear_block}CMD ["/bin/bash"]
"""


class ELASTICSEARCH_164626_TO_164626_ImageDefault(Image):
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
        return ELASTICSEARCH_164626_TO_164626_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        compile_tasks = _compile_tasks(self.pr)
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
                "TestReport.java",
                """import java.io.File;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

public class TestReport {
    public static void main(String[] args) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        for (String dir : args) {
            File[] reports = new File(dir).listFiles((d, name) -> name.startsWith("TEST-") && name.endsWith(".xml"));
            if (reports == null) {
                continue;
            }
            for (File report : reports) {
                Document doc = factory.newDocumentBuilder().parse(report);
                NodeList cases = doc.getElementsByTagName("testcase");
                for (int i = 0; i < cases.getLength(); i++) {
                    Element testCase = (Element) cases.item(i);
                    String status = "PASSED";
                    if (testCase.getElementsByTagName("failure").getLength() > 0
                            || testCase.getElementsByTagName("error").getLength() > 0) {
                        status = "FAILED";
                    } else if (testCase.getElementsByTagName("skipped").getLength() > 0) {
                        status = "SKIPPED";
                    }
                    String name = (testCase.getAttribute("classname") + "#" + testCase.getAttribute("name")).replaceAll("\\\\s+", " ").trim();
                    System.out.println("JUNIT " + status + " " + name);
                }
            }
        }
    }
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e
export CI=true

cd /home/[[REPO]]
git init -q
git remote add origin https://github.com/[[ORG]]/[[REPO]].git
git fetch --depth 1 --no-tags origin [[SHA]]
git checkout --detach [[SHA]]
git reset --hard
bash /home/check_git_changes.sh

mkdir -p "$HOME/.gradle"
echo "org.gradle.jvmargs=-Xmx6g $(sed -n 's/^org.gradle.jvmargs=//p' gradle.properties)" >> "$HOME/.gradle/gradle.properties"
for attempt in 1 2 3; do
    if [[GRADLE]] [[COMPILE]]; then
        break
    fi
    if [ "$attempt" -eq 3 ]; then
        exit 1
    fi
    sleep 30
done

[[GRADLE]] --offline [[COMPILE]] && echo DEPS_OK
""".replace("[[REPO]]", repo)
                .replace("[[ORG]]", self.pr.org)
                .replace("[[SHA]]", self.pr.base.sha)
                .replace("[[GRADLE]]", GRADLE)
                .replace("[[COMPILE]]", compile_tasks),
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if [[GRADLE]] [[COMPILE]] > /tmp/compile_test.log 2>&1; then
        break
    fi
    cat /tmp/compile_test.log
    broken=$(sed -nE 's#^(/home/[[REPO]]/.+/src/[A-Za-z]*[Tt]est[A-Za-z]*/java/.+\.java):[0-9]+: error:.*#\1#p' /tmp/compile_test.log | sort -u)
    if [ -z "$broken" ]; then
        exit 1
    fi
    for file in $broken; do
        echo "EXCLUDED_UNCOMPILABLE_TEST $file"
        rm -f "$file"
    done
done

rm -rf [[RESULT_DIRS]]
status=0
[[GRADLE]] --continue -Dtests.seed=[[SEED]] [[TEST_TASKS]] || status=$?
java /home/TestReport.java [[RESULT_DIRS]]
exit $status
""".replace("[[REPO]]", repo)
                .replace("[[GRADLE]]", GRADLE)
                .replace("[[COMPILE]]", compile_tasks)
                .replace("[[RESULT_DIRS]]", _result_dirs(self.pr))
                .replace("[[SEED]]", TEST_SEED)
                .replace("[[TEST_TASKS]]", _test_tasks(self.pr)),
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
                RUN mkdir -p /home/{ES_USER}/.gradle && \\
                    printf 'systemProp.http.proxyHost={proxy_host}\\nsystemProp.http.proxyPort={proxy_port}\\nsystemProp.https.proxyHost={proxy_host}\\nsystemProp.https.proxyPort={proxy_port}\\n' >> /home/{ES_USER}/.gradle/gradle.properties && \\
                    chown -R {ES_USER}:{ES_USER} /home/{ES_USER}/.gradle
                """
                )

                proxy_cleanup = textwrap.dedent(
                    f"""
                    RUN sed -i '/^systemProp\\.https\\?\\.proxy/d' /home/{ES_USER}/.gradle/gradle.properties
                """
                )

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{self.pr.base.sha}"
{proxy_setup}
{copy_commands}
USER {ES_USER}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}
{proxy_cleanup}{clear_block}"""


@Instance.register("elastic", "elasticsearch_164626_to_164626")
class ELASTICSEARCH_164626_TO_164626(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ELASTICSEARCH_164626_TO_164626_ImageDefault(self.pr, self._config)

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

        re_junit = re.compile(r"^JUNIT\s+(PASSED|FAILED|SKIPPED)\s+(.+?)\s*$")

        status_map = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }

        for line in test_log.splitlines():
            match = re_junit.match(line.strip())
            if match:
                status_map[match.group(1)].add(match.group(2))

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


Instance.register("elastic", "elasticsearch")(ELASTICSEARCH_164626_TO_164626)
