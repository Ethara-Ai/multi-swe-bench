from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "btrace"
JDK_IMAGES = {
    "8": "eclipse-temurin:8-jdk-noble",
    "11": "eclipse-temurin:11-jdk-noble",
    "17": "eclipse-temurin:17-jdk-noble",
    "24": "eclipse-temurin:24-jdk-noble",
}

GRADLE_PROPERTIES = """org.gradle.java.installations.auto-download=false
org.gradle.java.installations.auto-detect=true
org.gradle.java.installations.paths=/opt/jdks/8,/opt/jdks/11,/opt/jdks/17,/opt/java/openjdk,/opt/jdks/24
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8
org.gradle.daemon=false
"""

TESTS_PY = r"""import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO_DIR]]"
PLUGIN = "btrace-gradle-plugin"
INTEGRATION = "integration-tests"
LEGACY = {"btrace-services", "btrace-services-api", "btrace-statsd"}
INCLUDE = re.compile(r"^(Test.*|.*Test|.*Tests|.*TestCase)\.java$")
BROKEN = re.compile(r"^(/\S+?/src/test/java/\S+?\.java):\d+: error:")
METHOD = re.compile(r"^([^(\[]+)\([^)]*\)(.*)$")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def patch_paths(patch_file):
    paths = set()
    with open(patch_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            if m:
                paths.add(m.group(1))
                paths.add(m.group(2))
    return paths


def included(mod):
    if not os.path.isfile(os.path.join(REPO, mod, "build.gradle")):
        return False
    if mod == INTEGRATION:
        return True
    if "/" not in mod:
        return mod.startswith("btrace-") and mod not in LEGACY
    try:
        with open(os.path.join(REPO, "settings.gradle"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    gpath = mod.replace("/", ":")
    return ("'%s'" % gpath) in text or ('"%s"' % gpath) in text


def modules(patch_file):
    mods = set()
    for p in patch_paths(patch_file):
        if "/src/test/" not in p:
            continue
        mod = p[:p.index("/src/test/")]
        if mod == PLUGIN or included(mod):
            mods.add(mod)
    print(",".join(sorted(mods)))


def itclasses(patch_file):
    prefix = INTEGRATION + "/src/test/java/"
    out = set()
    for p in patch_paths(patch_file):
        if not (p.startswith(prefix) and p.endswith(".java")):
            continue
        base = os.path.basename(p)
        if not INCLUDE.match(base):
            continue
        full = os.path.join(REPO, p)
        if os.path.isfile(full):
            with open(full, encoding="utf-8", errors="replace") as fh:
                if re.search(r"\babstract\s+class\s+" + re.escape(base[:-5]) + r"\b", fh.read()):
                    continue
        out.add(p[len(prefix):-5].replace("/", "."))
    print(" ".join(sorted(out)))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line)
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def method_name(name):
    name = name.strip()
    m = METHOD.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for mod in filter(None, mods.split(",")):
        rdir = os.path.join(REPO, mod, "build", "test-results")
        if not os.path.isdir(rdir):
            continue
        for d, _, files in os.walk(rdir):
            for f in sorted(files):
                if not (f.startswith("TEST-") and f.endswith(".xml")):
                    continue
                try:
                    root = ET.parse(os.path.join(d, f)).getroot()
                except ET.ParseError:
                    continue
                for tc in root.iter():
                    if local(tc.tag) != "testcase":
                        continue
                    cls = (tc.get("classname") or "").strip()
                    name = method_name(tc.get("name") or "")
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
    {"modules": modules, "itclasses": itclasses, "broken": broken, "junit": junit}[cmd](arg)
""".replace("[[REPO_DIR]]", REPO_DIR)

TESTS_SH = r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
HELPER=/home/btrace_tests.py
GRADLE="./gradlew --no-daemon --console=plain --continue"
PLUGIN=btrace-gradle-plugin
INTEGRATION=integration-tests

has_mod() {
    case ",$1," in
        *",$2,"*) return 0 ;;
    esac
    return 1
}

root_tasks() {
    local mods="$1" task="$2" skip="$3" out="" m
    for m in ${mods//,/ }; do
        [ "$m" = "$PLUGIN" ] && continue
        [ -n "$skip" ] && [ "$m" = "$skip" ] && continue
        out="$out :${m//\//:}:$task"
    done
    echo $out
}

plugin_args() {
    if [ -f "$REPO/$PLUGIN/settings.gradle" ]; then
        echo "-p $PLUGIN $1"
    else
        echo ":$PLUGIN:$1"
    fi
}

apply_patches() {
    local p f skip
    cd $REPO
    if git apply --whitespace=nowarn --check "$@" 2>/dev/null; then
        git apply --whitespace=nowarn "$@"
        return $?
    fi
    for p in "$@"; do
        if git apply --whitespace=nowarn --check "$p" 2>/dev/null; then
            git apply --whitespace=nowarn "$p" || return 1
            continue
        fi
        skip=""
        for f in $(grep -oE '^diff --git a/[^ ]+' "$p" | cut -c14- | sort -u); do
            if git apply --whitespace=nowarn --check --include="$f" "$p" 2>/dev/null; then
                continue
            elif git apply --whitespace=nowarn -R --check --include="$f" "$p" 2>/dev/null; then
                echo "PATCH_ALREADY_APPLIED $f"
                skip="$skip --exclude=$f"
            else
                git apply --whitespace=nowarn --check --include="$f" "$p"
                return 1
            fi
        done
        git apply --whitespace=nowarn $skip "$p" || return 1
    done
    return 0
}

setup() {
    cd $REPO
    ./gradlew --no-daemon --console=plain --version || return 1
    for i in 1 2 3; do
        $GRADLE -Pintegration testClasses && break
        sleep 30
    done
    for i in 1 2 3; do
        $GRADLE $(plugin_args testClasses) && break
        sleep 30
    done
    return 0
}

compile_tests() {
    local mods="$1" tasks f i
    tasks=$(root_tasks "$mods" testClasses "")
    for i in $(seq 1 15); do
        : > /tmp/bt_compile.log
        if [ -n "$tasks" ]; then
            $GRADLE -Pintegration $tasks >> /tmp/bt_compile.log 2>&1
        fi
        if has_mod "$mods" $PLUGIN; then
            $GRADLE $(plugin_args testClasses) >> /tmp/bt_compile.log 2>&1
        fi
        grep -E ': error:|FAILED|BUILD ' /tmp/bt_compile.log | head -n 200
        python3 $HELPER broken /tmp/bt_compile.log > /tmp/bt_broken
        [ -s /tmp/bt_broken ] || break
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            rm -f "$f"
        done < /tmp/bt_broken
    done
}

run_tests() {
    local mods unit its args c m
    cd $REPO
    mods=$(python3 $HELPER modules /home/test.patch)
    echo "BTRACE_TEST_MODULES=$mods"
    [ -n "$mods" ] || return 0
    for m in ${mods//,/ }; do
        rm -rf "$REPO/$m/build/test-results"
    done
    compile_tests "$mods"
    unit=$(root_tasks "$mods" test "$INTEGRATION")
    if [ -n "$unit" ]; then
        timeout -k 60 3600 $GRADLE $unit > /tmp/bt_unit.log 2>&1
        tail -n 150 /tmp/bt_unit.log
    fi
    if has_mod "$mods" $PLUGIN; then
        timeout -k 60 1800 $GRADLE $(plugin_args test) > /tmp/bt_plugin.log 2>&1
        tail -n 150 /tmp/bt_plugin.log
    fi
    if has_mod "$mods" $INTEGRATION; then
        its=$(python3 $HELPER itclasses /home/test.patch)
        echo "BTRACE_INTEGRATION_CLASSES=$its"
        if [ -n "$its" ]; then
            args=""
            for c in $its; do
                args="$args --tests $c"
            done
            timeout -k 60 3600 $GRADLE -Pintegration :$INTEGRATION:test $args > /tmp/bt_integration.log 2>&1
            tail -n 150 /tmp/bt_integration.log
        fi
    fi
    python3 $HELPER junit "$mods"
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
    apply) shift; apply_patches "$@" ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR)


class Btrace_964_to_896_ImageBase(Image):
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
        return "eclipse-temurin:21-jdk-noble"

    def image_tag(self) -> str:
        return "base-temurin-8-24"

    def workdir(self) -> str:
        return "base-temurin-8-24"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "gradle.properties",
                GRADLE_PROPERTIES,
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

        jdk_copies = "\n".join(
            f"COPY --from={image} /opt/java/openjdk /opt/jdks/{version}"
            for version, image in JDK_IMAGES.items()
        )

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*

{jdk_copies}

COPY gradle.properties /root/.gradle/gradle.properties

RUN for v in 8 11 17 24; do /opt/jdks/$v/bin/java -version; done && java -version

WORKDIR /home/

{code}

RUN cd /home/{REPO_DIR} && (./gradlew --no-daemon --console=plain --continue -Pintegration testClasses || true) && (./gradlew --no-daemon --console=plain --continue -p btrace-gradle-plugin testClasses || true) && git -C /home/{REPO_DIR} clean -fdx

{self.clear_env}

"""


class Btrace_964_to_896_ImageDefault(Image):
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
        return Btrace_964_to_896_ImageBase(self.pr, self._config)

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
                "btrace_tests.py",
                TESTS_PY,
            ),
            File(
                ".",
                "btrace_tests.sh",
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

bash /home/btrace_tests.sh setup

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/btrace_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! bash /home/btrace_tests.sh apply /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/btrace_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! bash /home/btrace_tests.sh apply /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/btrace_tests.sh run

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


@Instance.register("btraceio", "btrace_964_to_896")
class Btrace_964_to_896(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Btrace_964_to_896_ImageDefault(self.pr, self._config)

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


Instance.register("btraceio", "btrace")(Btrace_964_to_896)
