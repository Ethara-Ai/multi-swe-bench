import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_VERSION = "22.12.0"
GRADLE = "./gradlew --console=plain --no-build-cache"
WEBVIEW = "webview"
BRIDGE = "ai-bridge"
JS_RESULTS = "/tmp/js-test-results"
JAVA_RESULTS = "build/test-results/test"


def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+)", patch, re.MULTILINE)


def _java_tests(pr: PullRequest) -> list[str]:
    tests = set()
    for path in _patch_paths(pr.test_patch):
        match = re.match(r"src/test/java/(.+)\.java$", path)
        if match and re.search(r"(Tests?|IT)$", match.group(1)):
            tests.add(match.group(1).replace("/", "."))
    return sorted(tests)


def _webview_tests(pr: PullRequest) -> list[str]:
    return sorted(
        path[len(WEBVIEW) + 1 :]
        for path in _patch_paths(pr.test_patch)
        if path.startswith(WEBVIEW + "/") and re.search(r"\.test\.tsx?$", path)
    )


def _bridge_tests(pr: PullRequest) -> list[str]:
    return sorted(
        path
        for path in _patch_paths(pr.test_patch)
        if path.startswith(BRIDGE + "/") and re.search(r"\.test\.m?js$", path)
    )


class JETBRAINS_CC_GUI_1732_TO_1727_ImageBase(Image):
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
        return "eclipse-temurin:21-jdk-noble"

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

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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
    git ca-certificates curl xz-utils openjdk-17-jdk-headless \\
    && rm -rf /var/lib/apt/lists/*

RUN case "${{TARGETARCH:-$(dpkg --print-architecture)}}" in \\
        amd64) NODE_ARCH=x64 ;; \\
        arm64) NODE_ARCH=arm64 ;; \\
        *) echo "unsupported architecture" >&2; exit 1 ;; \\
    esac && \\
    curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-${{NODE_ARCH}}.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1 && \\
    node --version && npm --version

RUN git config --global --add safe.directory '*'

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class JETBRAINS_CC_GUI_1732_TO_1727_ImageDefault(Image):
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
        return JETBRAINS_CC_GUI_1732_TO_1727_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _provision(self) -> str:
        repo = self.pr.repo
        java = bool(_java_tests(self.pr))
        steps = []
        if java or _webview_tests(self.pr):
            steps.append(
                f"cd /home/{repo}/{WEBVIEW}\n"
                "npm ci || true\n"
                "npm run prebuild || true\n"
                f"cd /home/{repo}"
            )
        if java or _bridge_tests(self.pr):
            steps.append(f"cd /home/{repo}/{BRIDGE}\nnpm ci || true\ncd /home/{repo}")
        if java:
            steps.append(
                textwrap.dedent(
                    f"""\
                    for attempt in 1 2 3 4 5; do
                        if {GRADLE} testClasses; then
                            break
                        fi
                        if [ "$attempt" -eq 5 ]; then
                            break
                        fi
                        sleep 30
                    done || true"""
                )
            )
        return "\n".join(steps)

    def _gate(self) -> str:
        checks = []
        if _webview_tests(self.pr):
            checks.append(f"(cd {WEBVIEW} && npx --no-install vitest --version)")
        if _bridge_tests(self.pr):
            checks.append(f"test -d {BRIDGE}/node_modules")
        if _java_tests(self.pr):
            checks.append(f"{GRADLE} testClasses")
        return " && ".join(checks + ["echo DEPS_OK"])

    def _run_tests(self) -> str:
        repo = self.pr.repo
        parts = [f"status=0\nrm -rf {JS_RESULTS} {JAVA_RESULTS}\nmkdir -p {JS_RESULTS}"]
        webview_tests = _webview_tests(self.pr)
        if webview_tests:
            parts.append(
                f"cd /home/{repo}/{WEBVIEW}\n"
                f"npx --no-install vitest run --reporter=default --reporter=junit "
                f"--outputFile.junit={JS_RESULTS}/TEST-{WEBVIEW}.xml {' '.join(webview_tests)} || status=$?\n"
                f"cd /home/{repo}"
            )
        for test_file in _bridge_tests(self.pr):
            report = re.sub(r"[^A-Za-z0-9_.-]", "_", test_file)
            parts.append(
                f"node --test --test-reporter=spec --test-reporter-destination=stdout "
                f"--test-reporter=junit --test-reporter-destination={JS_RESULTS}/TEST-{report}.xml "
                f"{test_file} || status=$?"
            )
        java_tests = _java_tests(self.pr)
        if java_tests:
            parts.append(
                textwrap.dedent(
                    r"""
                    for attempt in 1 2 3 4 5 6 7 8 9 10; do
                        if [[GRADLE]] testClasses > /tmp/compile_test.log 2>&1; then
                            break
                        fi
                        cat /tmp/compile_test.log
                        broken=$(sed -nE 's#^(/home/[[REPO]]/src/test/java/.+\.java):[0-9]+: error:.*#\1#p' /tmp/compile_test.log | sort -u)
                        if [ -z "$broken" ]; then
                            exit 1
                        fi
                        for file in $broken; do
                            echo "EXCLUDED_UNCOMPILABLE_TEST $file"
                            rm -f "$file"
                        done
                    done

                    [[GRADLE]] --continue test [[TESTS]] || status=$?"""
                )
                .replace("[[GRADLE]]", GRADLE)
                .replace("[[REPO]]", repo)
                .replace("[[TESTS]]", " ".join(f"--tests {fqcn}" for fqcn in java_tests))
            )
        parts.append(f"java /home/TestReport.java {JAVA_RESULTS} {JS_RESULTS}\nexit $status")
        return "#!/bin/bash\nset -eo pipefail\n\ncd /home/{repo}\n{body}\n".format(
            repo=repo, body="\n".join(parts)
        )

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
                    String prefix = testCase.getAttribute("classname");
                    if (prefix.isEmpty() || prefix.equals("test")) {
                        prefix = report.getName().replaceAll("^TEST-|\\\\.xml$", "");
                    }
                    String name = (prefix + "#" + testCase.getAttribute("name")).replaceAll("\\\\s+", " ").trim();
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
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{provision}

{gate}
""".format(
                    repo=repo,
                    sha=self.pr.base.sha,
                    provision=self._provision(),
                    gate=self._gate(),
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                self._run_tests(),
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

        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}
{clear_block}"""


@Instance.register("zhukunpenglinyutong", "jetbrains_cc_gui_1732_to_1727")
class JETBRAINS_CC_GUI_1732_TO_1727(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return JETBRAINS_CC_GUI_1732_TO_1727_ImageDefault(self.pr, self._config)

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


Instance.register("zhukunpenglinyutong", "jetbrains-cc-gui")(JETBRAINS_CC_GUI_1732_TO_1727)
