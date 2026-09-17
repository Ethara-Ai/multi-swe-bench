import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ERA_LO = 8236
_ERA_HI = 11597

_JDK_IMAGE = "eclipse-temurin:17.0.20_8-jdk"

_MAVEN_VERSION = "3.9.9"
_MAVEN_SHA512 = (
    "a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556"
    "bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b"
)

_YQ_VERSION = "4.44.3"
_YQ_SHA256_AMD64 = "a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"
_YQ_SHA256_ARM64 = "0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156"

_BASE_APT = "ca-certificates curl build-essential git gnupg make python3 sudo wget unzip"

_MODULES = {
    "api",
    "certificate-manager",
    "cluster-operator",
    "config-model",
    "config-model-generator",
    "crd-annotations",
    "crd-generator",
    "kafka-agent",
    "kafka-init",
    "mirror-maker-agent",
    "mockkube",
    "operator-common",
    "test",
    "test-container",
    "topic-operator",
    "tracing-agent",
    "user-operator",
}

_EXCLUDED_MODULES = ("systemtest",)

_DEFAULT_MODULES = ["cluster-operator"]

_CONFIG_MODEL_MODULE = "config-model-generator"
_NEEDS_CONFIG_MODEL = "cluster-operator"

_MVN_FLAGS = (
    "-B -ntp -fae "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dcheckstyle.skip=true "
    "-Dspotbugs.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.gitcommitid.skip=true "
    "-Daether.connector.http.retryHandler.count=5 "
    "-Daether.connector.http.retryHandler.requestSentEnabled=true "
    "-Djunit.jupiter.execution.parallel.config.strategy=fixed "
    "-Djunit.jupiter.execution.parallel.config.fixed.parallelism=1 "
    "-Djunit.jupiter.execution.parallel.config.fixed.max-pool-size=1"
)

_NO_TARGET_FILTER = "StrimziNoSelectedTestPlaceholder"

_BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
_END_MARKER = "===== END TEST DETAIL ====="

_TARGETS_FILE = "/home/test_targets.tsv"
_BUILD_LOG = "/home/stage_build.log"

_RETRY_SHELL = (
    "with_retry() {\n"
    "  local attempt\n"
    "  for attempt in 1 2 3; do\n"
    '    if "$@"; then\n'
    "      return 0\n"
    "    fi\n"
    '    echo "prepare: attempt ${attempt} of 3 failed: $1" >&2\n'
    "    sleep 20\n"
    "  done\n"
    "  return 1\n"
    "}\n"
)

_UNSET_PROXY = "unset http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY\n"

_NEW_PATH_RE = re.compile(r"^\+\+\+ b/(\S+)", re.M)

_SELECT_TESTS_PY = r'''import os
import re
import sys

INCLUDE = re.compile(r"^(Test\w*|\w+Test|\w+Tests|\w+TestCase)\.java$")
NEW_PATH = re.compile(r"^\+\+\+ b/(\S+)", re.M)
ENVIRONMENT_BOUND = re.compile(
    r"MockKube3|@Testcontainers|GenericContainer|KubeClusterResource|createKubernetesClient\("
)

excluded = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 else "").split(",")))
targets = {}


def patch_paths(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return NEW_PATH.findall(fh.read())
    except OSError:
        return []


def split_source(path, kind):
    marker = "/src/" + kind + "/"
    if marker not in path:
        return None
    module, rest = path.split(marker, 1)
    if not module or module in excluded:
        return None
    return module, rest


def add(module, rel):
    if not INCLUDE.match(os.path.basename(rel)):
        return
    source = os.path.join(module, "src", "test", "java", rel)
    if not os.path.isfile(source):
        return
    with open(source, encoding="utf-8", errors="replace") as fh:
        if ENVIRONMENT_BOUND.search(fh.read()):
            return
    targets[rel[: -len(".java")].replace("/", ".")] = module


def add_package(module, package_dir):
    source_dir = os.path.join(module, "src", "test", "java", package_dir)
    if os.path.isdir(source_dir):
        for entry in sorted(os.listdir(source_dir)):
            add(module, "/".join(filter(None, [package_dir, entry])))


def changed_main_classes():
    found = []
    for path in patch_paths("/home/fix.patch"):
        split = split_source(path, "main")
        if split and split[1].startswith("java/") and split[1].endswith(".java"):
            found.append((split[0], split[1][len("java/"): -len(".java")]))
    return found


for path in patch_paths("/home/test.patch"):
    found = split_source(path, "test")
    if not found:
        continue
    module, rest = found
    if rest.startswith("java/") and rest.endswith(".java"):
        add(module, rest[len("java/"):])
    elif rest.startswith("resources/"):
        add_package(module, os.path.dirname(rest[len("resources/"):]))

if not targets:
    for module, class_path in changed_main_classes():
        add(module, class_path + "Test.java")

if not targets:
    for module, class_path in changed_main_classes():
        add_package(module, os.path.dirname(class_path))

for name, module in sorted(targets.items()):
    print(module + "\t" + name)
'''

_EMIT_RESULTS_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

begin, end, targets_file, build_log = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

COMPILE_ERROR = re.compile(r"^\[ERROR\] (/\S+?)/src/(?:main|test)/java/\S+\.java:\[\d+")

targets = []
if os.path.isfile(targets_file):
    with open(targets_file, encoding="utf-8") as fh:
        targets = [line.rstrip("\n").split("\t") for line in fh if line.strip()]

root_dir = os.getcwd().rstrip("/") + "/"
broken_modules = set()
if os.path.isfile(build_log):
    with open(build_log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = COMPILE_ERROR.match(line.strip())
            if match and match.group(1).startswith(root_dir):
                broken_modules.add(match.group(1)[len(root_dir):])

print(begin)
for module, name in targets:
    report = os.path.join(module, "target", "surefire-reports", "TEST-" + name + ".xml")
    compiled = os.path.join(module, "target", "test-classes", name.replace(".", "/") + ".class")
    status = None
    if os.path.isfile(report):
        try:
            root = ET.parse(report).getroot()
            total = int(root.get("tests", "0"))
            broken = int(root.get("failures", "0")) + int(root.get("errors", "0"))
            skipped = int(root.get("skipped", "0"))
        except (ET.ParseError, ValueError):
            status = "FAILED"
        else:
            if broken:
                status = "FAILED"
            elif total and skipped >= total:
                status = "SKIPPED"
            elif total:
                status = "PASSED"
    elif module in broken_modules or not os.path.isfile(compiled):
        status = "FAILED"
    if status:
        print("TESTCASE " + name + " " + status)
print(end)
'''


def _modules_for_pr(pr: PullRequest) -> list:
    text = (pr.fix_patch or "") + "\n" + (pr.test_patch or "")
    found = set()
    for path in _NEW_PATH_RE.findall(text):
        top = path.split("/", 1)[0]
        if top in _MODULES and top not in _EXCLUDED_MODULES:
            found.add(top)
    if not found:
        return list(_DEFAULT_MODULES)
    return sorted(found)


def _config_model_shell(selected: list) -> str:
    if _NEEDS_CONFIG_MODEL not in selected:
        return ""

    return (
        "export MVN_ARGS=\"-B -ntp -Dcheckstyle.skip=true -Dspotbugs.skip=true"
        ' -Dmaven.javadoc.skip=true"\n'
        f"with_retry mvn {_MVN_FLAGS} -pl config-model -am -DskipTests install\n"
        f"with_retry bash -c 'cd {_CONFIG_MODEL_MODULE} && bash ./build-config-models.sh build'\n"
        "_models=$(ls cluster-operator/src/main/resources/kafka-*-config-model.json 2>/dev/null | wc -l)\n"
        'if [ "${_models}" -lt 1 ]; then\n'
        '  echo "prepare: config-model generation produced no '
        'kafka-*-config-model.json files" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "prepare: generated ${_models} Kafka config model(s)"\n'
        "\n"
    )


def _select_targets_shell() -> str:
    excluded = ",".join(_EXCLUDED_MODULES)
    return (
        f"python3 /home/select_tests.py {excluded} > {_TARGETS_FILE}\n"
        f'TEST_FILTER=$(cut -f2 {_TARGETS_FILE} | paste -sd, -)\n'
        'if [ -z "$TEST_FILTER" ]; then\n'
        f"  TEST_FILTER={_NO_TARGET_FILTER}\n"
        "fi\n"
        'echo "selected test classes: $TEST_FILTER"\n'
    )


def _stage_body(pr: PullRequest) -> str:
    selected = _modules_for_pr(pr)
    mods = ",".join(selected)
    return (
        _select_targets_shell()
        + f"for _module in {' '.join(selected)} $(cut -f1 {_TARGETS_FILE} | sort -u); do\n"
        '  rm -rf "${_module}/target/classes" "${_module}/target/test-classes" \\\n'
        '    "${_module}/target/generated-sources" "${_module}/target/generated-test-sources" \\\n'
        '    "${_module}/target/maven-status" "${_module}/target/surefire-reports"\n'
        "done\n"
        "set +e\n"
        f'mvn {_MVN_FLAGS} -pl {mods} -am package -Dtest="$TEST_FILTER" -Dmaven.test.failure.ignore=true 2>&1 | tee {_BUILD_LOG}\n'
        "MVN_EXIT_CODE=${PIPESTATUS[0]}\n"
        "set -e\n"
        'echo "MVN_EXIT_CODE=${MVN_EXIT_CODE}"\n'
        f'python3 /home/emit_results.py "{_BEGIN_MARKER}" "{_END_MARKER}" {_TARGETS_FILE} {_BUILD_LOG}\n'
    )


def _stage_script(pr: PullRequest, apply_line: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        + _UNSET_PROXY
        + "\n"
        f"cd /home/{pr.repo}\n"
        "git reset --hard\n"
        "git clean -qfd\n"
        + apply_line
        + "\n"
        + _stage_body(pr)
    )


def _parse_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    case_re = re.compile(r"^TESTCASE (\S+) (PASSED|FAILED|SKIPPED)\s*$")

    in_detail = False
    for line in clean_log.splitlines():
        stripped = line.strip()
        if stripped == _BEGIN_MARKER:
            in_detail = True
            continue
        if stripped == _END_MARKER:
            in_detail = False
            continue
        if not in_detail:
            continue

        match = case_re.match(stripped)
        if not match:
            continue

        name, status = match.group(1), match.group(2)
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


class StrimziBase11597To8236(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return f"base-{_ERA_HI}_to_{_ERA_LO}"

    def workdir(self) -> str:
        return f"base-{_ERA_HI}_to_{_ERA_LO}"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"{self.clear_env}\n\n" if self.clear_env else ""

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            clone = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {_JDK_IMAGE}

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

ARG MAVEN_VERSION="{_MAVEN_VERSION}"
ARG MAVEN_SHA512="{_MAVEN_SHA512}"
ARG YQ_VERSION="{_YQ_VERSION}"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
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

ENV LC_ALL=C.UTF-8 \\
    CI=true \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    MAVEN_HOME="/opt/maven" \\
    MAVEN_OPTS="-Xmx3g -XX:MaxMetaspaceSize=1024m" \\
    PATH="/opt/maven/bin:${{PATH}}"

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
    {_BASE_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    case "${{TARGETARCH:-amd64}}" in \\
        amd64) _yqarch=amd64; _yqsum="{_YQ_SHA256_AMD64}" ;; \\
        arm64) _yqarch=arm64; _yqsum="{_YQ_SHA256_ARM64}" ;; \\
        *) echo "unsupported TARGETARCH ${{TARGETARCH}}" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /usr/local/bin/yq \\
        "https://github.com/mikefarah/yq/releases/download/v${{YQ_VERSION}}/yq_linux_${{_yqarch}}"; \\
    echo "${{_yqsum}}  /usr/local/bin/yq" | sha256sum -c -; \\
    chmod +x /usr/local/bin/yq; \\
    yq --version

RUN set -eux; \\
    curl -fsSL -o /tmp/maven.tar.gz \\
        "https://archive.apache.org/dist/maven/maven-3/${{MAVEN_VERSION}}/binaries/apache-maven-${{MAVEN_VERSION}}-bin.tar.gz"; \\
    echo "${{MAVEN_SHA512}}  /tmp/maven.tar.gz" | sha512sum -c -; \\
    mkdir -p /opt/maven; \\
    tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1; \\
    rm -f /tmp/maven.tar.gz; \\
    mvn -v

{clone}

{clear_block}CMD ["/bin/bash"]
"""


class StrimziPRImage11597To8236(Image):
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
        return StrimziBase11597To8236(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha
        selected = _modules_for_pr(self.pr)
        mods = ",".join(selected)

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            + _RETRY_SHELL
            + "\n"
            + _config_model_shell(selected)
            + _select_targets_shell()
            + "\n"
            f'with_retry mvn {_MVN_FLAGS} -pl {mods} -am package -Dtest="$TEST_FILTER" '
            "-Dmaven.test.failure.ignore=true\n"
            "\n"
            f"mvn -o -q {_MVN_FLAGS} -pl {mods} -am -DskipTests test-compile\n"
            'python3 -c "import xml.etree.ElementTree; print(\'python ok\')"\n'
            'echo "DEPS_OK"\n'
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "select_tests.py", _SELECT_TESTS_PY),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", _stage_script(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                _stage_script(
                    self.pr, "git apply --whitespace=nowarn /home/test.patch\n"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _stage_script(
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha
        env_block = f"{self.global_env}\n\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{env_block}ARG BASE_COMMIT="{sha}"

{copy_commands}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach ${{BASE_COMMIT}}

RUN bash /home/prepare.sh

{hardening}
{clear_block}"""


@Instance.register("strimzi", f"strimzi_kafka_operator_{_ERA_HI}_to_{_ERA_LO}")
class STRIMZI_KAFKA_OPERATOR_11597_TO_8236(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StrimziPRImage11597To8236(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
