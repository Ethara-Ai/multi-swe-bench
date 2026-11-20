import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_MAVEN_IMAGE = "maven:3.9.11-eclipse-temurin-11"
_BASE_TAG = "base-6524_to_6524"

_TEST_FILE_RE = re.compile(r"^\+\+\+ b/(\S*src/test/java/\S+\.java)$", re.MULTILINE)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch)))


def _modules(pr: PullRequest) -> str:
    return ",".join(sorted({path.split("/src/test/java/", 1)[0] for path in _test_files(pr)}))


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
test -z "$(git status --porcelain)"
git checkout --detach __SHA__
test -z "$(git status --porcelain)"

mvn -q -pl __MODULES__ -am install -DskipTests

mvn -o -q -pl __MODULES__ test-compile
"""

_PATCH_STEP = r"""
git apply --whitespace=nowarn __PATCHES__

while ! compile_log="$(mvn -q -pl __MODULES__ test-compile 2>&1)"; do
    broken="$(printf '%s\n' "$compile_log" | sed -n 's|^\[ERROR\] \(/home/__REPO__/[^:]*src/test/java/[^:]*\.java\):.*|\1|p' | sort -u)"
    if [ -z "$broken" ]; then
        printf '%s\n' "$compile_log"
        exit 1
    fi
    for file in $broken; do
        mv "$file" "$file.uncompilable"
        echo "compile_guard: set aside $file"
    done
done
"""

_PLAN_PY = r"""
import fnmatch
import os
import re
import sys


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def class_tree(root):
    children = {}
    for folder, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".java"):
                continue
            body = read(os.path.join(folder, name))
            for match in re.finditer(r"(\babstract\s+)?\bclass\s+(\w+)[^{;]*?\bextends\s+(\w+)", body):
                children.setdefault(match.group(3), []).append((match.group(2), bool(match.group(1))))
    return children


def concrete_subclasses(children, name, seen):
    found = []
    for child, abstract in children.get(name, []):
        if child in seen:
            continue
        seen.add(child)
        if not abstract:
            found.append(child)
        found += concrete_subclasses(children, child, seen)
    return found


classes = []
executions = []
trees = {}
for gold in sys.argv[1:]:
    if not os.path.isfile(gold) or "@Test" not in read(gold):
        continue
    module, rel = gold.split("src/test/java/", 1)
    failsafe = re.search(r"<artifactId>maven-failsafe-plugin</artifactId>(.*?)</plugin>", read(module + "pom.xml"), re.S)
    ids = []
    if failsafe:
        for execution in re.findall(r"<execution>(.*?)</execution>", failsafe.group(1), re.S):
            includes = re.findall(r"<include>\s*(.*?)\s*</include>", execution)
            if any(fnmatch.fnmatch(rel, pattern) for pattern in includes):
                ids.append(re.search(r"<id>\s*(.*?)\s*</id>", execution).group(1))
    if ids:
        executions += [i for i in ids if i not in executions]
        continue
    name = rel[:-len(".java")].rsplit("/", 1)[-1]
    if re.search(r"\babstract\s+class\s+" + name + r"\b", read(gold)):
        if module not in trees:
            trees[module] = class_tree(module + "src/test/java")
        classes += concrete_subclasses(trees[module], name, {name})
    else:
        classes.append(name)

if classes:
    print("surefire:test")
    print("-Dtest=" + ",".join(sorted(set(classes))))
for execution in executions:
    print("failsafe:integration-test@" + execution)
"""

_EMIT_PY = r"""
import glob
import json
import xml.etree.ElementTree as ET

results = []
for report in sorted(glob.glob("**/target/*-reports/TEST-*.xml", recursive=True)):
    prefix = report.split("target/", 1)[0]
    for case in ET.parse(report).getroot().iter("testcase"):
        outer, _, inner = case.get("classname").partition("$")
        name = (inner.replace("$", ".") + "." if inner else "") + case.get("name")
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            status = "FAILED"
        elif "skipped" in tags:
            status = "SKIPPED"
        else:
            status = "PASSED"
        path = prefix + "src/test/java/" + outer.replace(".", "/") + ".java"
        results.append(["junit::" + path + "::" + name, status])
print("MSWB_RESULTS " + json.dumps(results))
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__
__PATCH_STEP__
plan="$(python3 - __TEST_FILES__ <<'MSWB_PLAN'
__PLAN_PY__
MSWB_PLAN
)"
echo "plan: $plan"
rc=0
if [ -n "$plan" ]; then
    mapfile -t goals <<< "$plan"
    mvn -pl __MODULES__ test-compile "${goals[@]}" \
        -Dsurefire.rerunFailingTestsCount=2 -Dfailsafe.rerunFailingTestsCount=2 \
        -Dmaven.test.failure.ignore=true || rc=$?
fi
echo "mvn exit $rc"
python3 - <<'MSWB_EMIT'
__EMIT_PY__
MSWB_EMIT
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _VertxImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class VertX6524To6524ImageBase(_VertxImage):
    def dependency(self) -> str | Image:
        return _MAVEN_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    MAVEN_ARGS="-B -ntp -nsu"

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

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends python3 && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class VertX6524To6524ImageDefault(_VertxImage):
    def dependency(self) -> Image:
        return VertX6524To6524ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run(self, name: str, *patches: str) -> File:
        values = {
            "__PATCH_STEP__": _PATCH_STEP if patches else "",
            "__PATCHES__": " ".join(f"/home/{p}" for p in patches),
            "__PLAN_PY__": _PLAN_PY.strip("\n"),
            "__EMIT_PY__": _EMIT_PY.strip("\n"),
            "__TEST_FILES__": " ".join(_test_files(self.pr)),
            "__MODULES__": _modules(self.pr),
            "__REPO__": self.pr.repo,
        }
        return File(".", name, _render(_RUN_SH, values))

    def files(self) -> list[File]:
        prepare = _render(
            _PREPARE_SH,
            {"__REPO__": self.pr.repo, "__SHA__": self.pr.base.sha, "__MODULES__": _modules(self.pr)},
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            self._run("run.sh"),
            self._run("test-run.sh", "test.patch"),
            self._run("fix-run.sh", "test.patch", "fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{base_commit_arg}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^MSWB_RESULTS (\[.*\])$", re.MULTILINE)


@Instance.register("eclipse-vertx", "vert_x_6524_to_6524")
class VERT_X_6524_TO_6524(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return VertX6524To6524ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        text = _ANSI_RE.sub("", test_log).replace("\r", "")
        for block in _RESULTS_RE.findall(text):
            try:
                results = json.loads(block)
            except ValueError:
                continue
            for test_id, status in results:
                found[status].add(test_id)

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
