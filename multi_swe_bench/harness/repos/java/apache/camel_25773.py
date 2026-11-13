from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "camel"
MAVEN_VERSION = "3.9.9"
PLUGIN_MODULE = "tooling/maven/camel-package-maven-plugin"
MVN_FLAGS = "-B -ntp -Denforcer.skip=true -Dcheckstyle.skip=true -Dformatter.skip=true -Dimpsort.skip=true"

TESTS_PY = r"""import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO_DIR]]"
INCLUDE = re.compile(r"^(.*Test|.*Tests|.*TestCase)\.java$")
BROKEN = re.compile(r"\[ERROR\]\s+(/\S+?/src/test/java/\S+?\.java):\[\d+")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_paths(patch_file):
    paths = set()
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                paths.add(m.group(2))
    return paths


def test_files(patch_file):
    return sorted(p for p in patch_paths(patch_file) if "/src/test/java/" in p and p.endswith(".java"))


def modules(patch_file):
    mods = {p[:p.index("/src/test/")] for p in test_files(patch_file)}
    print(",".join(sorted(m for m in mods if os.path.isfile(os.path.join(REPO, m, "pom.xml")))))


def testlist(patch_file):
    out = set()
    for p in test_files(patch_file):
        mod, rest = p.split("/src/test/java/", 1)
        pkg = os.path.dirname(rest)
        d = os.path.join(REPO, mod, "src", "test", "java", pkg)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if INCLUDE.match(f) and os.path.isfile(os.path.join(d, f)):
                out.add(os.path.join(pkg, f))
    print(",".join(sorted(out)))


def module_of(path):
    d = os.path.dirname(path)
    while d:
        if os.path.isfile(os.path.join(REPO, d, "pom.xml")):
            return d
        d = os.path.dirname(d)
    return ""


def changed(test_mods):
    skip = set(filter(None, test_mods.split(",")))
    out = subprocess.run(["git", "-C", REPO, "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True).stdout
    mods = set()
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not path or "/src/test/" in path or "/target/" in path or path.startswith("target/"):
            continue
        mod = module_of(path)
        if mod and mod not in skip:
            mods.add(mod)
    print(",".join(sorted(mods)))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line)
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for mod in filter(None, mods.split(",")):
        rdir = os.path.join(REPO, mod, "target", "surefire-reports")
        if not os.path.isdir(rdir):
            continue
        for f in sorted(os.listdir(rdir)):
            if not (f.startswith("TEST-") and f.endswith(".xml")):
                continue
            try:
                root = ET.parse(os.path.join(rdir, f)).getroot()
            except ET.ParseError:
                continue
            for tc in root.iter():
                if local(tc.tag) != "testcase":
                    continue
                cls = (tc.get("classname") or "").strip()
                name = (tc.get("name") or "").strip()
                if name.endswith("()"):
                    name = name[:-2]
                tid = re.sub(r"\s+", " ", cls + "#" + name).strip()
                tags = {local(c.tag) for c in tc}
                if tags & {"failure", "error"}:
                    status = "FAILED"
                elif "skipped" in tags:
                    status = "SKIPPED"
                else:
                    status = "PASSED"
                if tid not in results or rank[status] > rank[results[tid]]:
                    results[tid] = status
    for tid in sorted(results):
        print("JUNIT " + results[tid] + " " + tid)


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {"modules": modules, "testlist": testlist, "changed": changed, "broken": broken, "junit": junit}[cmd](arg)
""".replace("[[REPO_DIR]]", REPO_DIR)

TESTS_SH = r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
HELPER=/home/camel_tests.py
MVN="mvn [[MVN_FLAGS]]"

setup() {
    local mods tests i
    cd $REPO
    mods=$(python3 $HELPER modules /home/test.patch)
    tests=$(python3 $HELPER testlist /home/test.patch)
    echo "CAMEL_SETUP_MODULES=$mods"
    for i in 1 2 3; do
        $MVN install -DskipTests -pl "[[PLUGIN_MODULE]],$mods" -am && break
        [ $i -eq 3 ] && return 1
        sleep 30
    done
    for i in 1 2 3; do
        $MVN -pl "$mods" test -Dtest="$tests" -Dsurefire.failIfNoSpecifiedTests=false && break
        sleep 30
    done
    return 0
}

java_run() {
    local mods="$1" tests changed f i
    cd $REPO
    for m in ${mods//,/ }; do
        rm -rf "$m/target/test-classes" "$m/target/surefire-reports" "$m/target/maven-status/maven-compiler-plugin/testCompile"
    done
    changed=$(python3 $HELPER changed "$mods")
    echo "CAMEL_CHANGED_MODULES=$changed"
    if [ -n "$changed" ]; then
        if ! $MVN install -DskipTests -pl "$changed" > /tmp/camel_install.log 2>&1; then
            grep -E '^\[ERROR\]' /tmp/camel_install.log | head -n 200
            echo "CAMEL_CHANGED_MODULE_BUILD_FAILED"
            return 0
        fi
    fi
    for i in $(seq 1 15); do
        $MVN -fn -pl "$mods" test-compile > /tmp/camel_compile.log 2>&1
        grep -E '^\[ERROR\]' /tmp/camel_compile.log | head -n 200
        python3 $HELPER broken /tmp/camel_compile.log > /tmp/camel_broken
        [ -s /tmp/camel_broken ] || break
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            rm -f "$f"
        done < /tmp/camel_broken
    done
    tests=$(python3 $HELPER testlist /home/test.patch)
    if [ -n "$tests" ]; then
        $MVN -fn -pl "$mods" test -Dtest="$tests" -Dsurefire.failIfNoSpecifiedTests=false 2>&1
    fi
    python3 $HELPER junit "$mods"
}

run_tests() {
    local mods
    mods=$(python3 $HELPER modules /home/test.patch)
    echo "CAMEL_TEST_MODULES=$mods"
    if [ -n "$mods" ]; then
        java_run "$mods"
    fi
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR).replace("[[MVN_FLAGS]]", MVN_FLAGS).replace("[[PLUGIN_MODULE]]", PLUGIN_MODULE)


class Camel_25773_ImageBase(Image):
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
        return "eclipse-temurin:17-jdk"

    def image_tag(self) -> str:
        return "base-jdk17"

    def workdir(self) -> str:
        return "base-jdk17"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "untar.py",
                """import sys
import tarfile

dest = sys.argv[1]
strip = int(sys.argv[2]) if len(sys.argv) > 2 else 0
archive = tarfile.open(fileobj=sys.stdin.buffer, mode="r|*")


def members():
    for member in archive:
        parts = member.name.split("/")[strip:]
        if not parts or not parts[0]:
            continue
        member.name = "/".join(parts)
        if member.islnk():
            member.linkname = "/".join(member.linkname.split("/")[strip:])
        yield member


archive.extractall(dest, members=members(), filter="tar")
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {REPO_DIR} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*

COPY untar.py /tmp/untar.py

RUN (curl -fsSL --retry 5 -o /tmp/maven.tar.gz https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{MAVEN_VERSION}/apache-maven-{MAVEN_VERSION}-bin.tar.gz || curl -fsSL --retry 5 -o /tmp/maven.tar.gz https://archive.apache.org/dist/maven/maven-3/{MAVEN_VERSION}/binaries/apache-maven-{MAVEN_VERSION}-bin.tar.gz) && python3 /tmp/untar.py /opt < /tmp/maven.tar.gz && rm -f /tmp/untar.py /tmp/maven.tar.gz && ln -s /opt/apache-maven-{MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn && mvn -v

WORKDIR /home/

{code}

{self.clear_env}

"""


class Camel_25773_ImageDefault(Image):
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
        return Camel_25773_ImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "camel_tests.py",
                TESTS_PY,
            ),
            File(
                ".",
                "camel_tests.sh",
                TESTS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/camel_tests.sh setup

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/camel_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/camel_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/camel_tests.sh run

""".format(repo_dir=REPO_DIR),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("apache", "camel_25773")
class Camel_25773(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Camel_25773_ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        re_result = re.compile(r"^JUNIT\s+(PASSED|FAILED|SKIPPED)\s+(.+?)\s*$")

        status_map = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).rstrip()
            m = re_result.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("apache", "camel")(Camel_25773)
