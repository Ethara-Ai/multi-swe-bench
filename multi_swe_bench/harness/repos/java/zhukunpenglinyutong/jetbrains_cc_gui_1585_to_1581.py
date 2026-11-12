from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "jetbrains-cc-gui"
GRADLE = "./gradlew --no-daemon"
WARM_TEST = "com.github.claudecodegui.util.TokenUsageUtilsTest"

TESTS_PY = r"""import json
import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO_DIR]]"
JAVA_PREFIX = "src/test/java/"
JAVA_NAME = re.compile(r"^(Test.*|.*Test|.*Tests|.*TestCase)\.java$")
NODE = re.compile(r"^ai-bridge/.+\.test\.m?js$")
VITEST = re.compile(r"^webview/src/.+\.test\.tsx?$")
BROKEN = re.compile(r"^(/\S+/src/test/java/\S+?\.java):\d+: error:")


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
    return sorted(p for p in paths if os.path.isfile(os.path.join(REPO, p)))


def java(patch_file):
    out = []
    for p in patch_paths(patch_file):
        if p.startswith(JAVA_PREFIX) and JAVA_NAME.match(os.path.basename(p)):
            out.append(p[len(JAVA_PREFIX):-len(".java")].replace("/", "."))
    print(" ".join(out))


def node(patch_file):
    print(" ".join(p[len("ai-bridge/"):] for p in patch_paths(patch_file) if NODE.match(p)))


def vitest(patch_file):
    print(" ".join(p[len("webview/"):] for p in patch_paths(patch_file) if VITEST.match(p)))


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.match(line.strip())
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def junit(_):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    rdir = os.path.join(REPO, "build", "test-results", "test")
    if os.path.isdir(rdir):
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
                name = re.sub(r"\(\)$", "", (tc.get("name") or "").strip())
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


def vitest_json(json_file):
    try:
        data = json.load(open(json_file, encoding="utf-8"))
    except (OSError, ValueError):
        return
    status_map = {"passed": "PASSED", "failed": "FAILED"}
    for suite in data.get("testResults", []):
        rel = os.path.relpath(suite.get("name", ""), REPO)
        for a in suite.get("assertionResults", []):
            parts = [rel] + [re.sub(r"\s+", " ", t).strip() for t in a.get("ancestorTitles", [])]
            parts.append(re.sub(r"\s+", " ", a.get("title", "")).strip())
            print("VITEST " + status_map.get(a.get("status"), "SKIPPED") + " " + " > ".join(parts))


if __name__ == "__main__":
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    {"java": java, "node": node, "vitest": vitest, "broken": broken, "junit": junit, "vitest_json": vitest_json}[cmd](arg)
""".replace("[[REPO_DIR]]", REPO_DIR)

NODE_REPORTER = """import path from 'node:path';

const ROOT = '/home/[[REPO_DIR]]';
const clean = (s) => String(s).replace(/\\s+/g, ' ').trim();

export default async function* reporter(source) {
    for await (const event of source) {
        if (event.type !== 'test:pass' && event.type !== 'test:fail') {
            continue;
        }
        const d = event.data || {};
        if (d.details && d.details.type === 'suite') {
            continue;
        }
        const file = d.file ? path.relative(ROOT, d.file) : 'unknown';
        let status = event.type === 'test:pass' ? 'PASSED' : 'FAILED';
        if (d.skip || d.todo) {
            status = 'SKIPPED';
        }
        yield `NODETEST ${status} ${file} > ${clean(d.name)}\\n`;
    }
}
""".replace("[[REPO_DIR]]", REPO_DIR)

TESTS_SH = r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
HELPER=/home/jcg_tests.py
GRADLE="[[GRADLE]]"

npm_setup() {
    local d
    for d in webview ai-bridge; do
        cd $REPO/$d
        for i in 1 2 3; do
            npm ci --no-audit --no-fund && break
            sleep 20
        done
    done
    cd $REPO
}

setup() {
    cd $REPO
    chmod +x gradlew
    npm_setup
    for i in 1 2 3; do
        $GRADLE compileTestJava && break
        sleep 30
    done
    $GRADLE test --tests [[WARM_TEST]] || true
    rm -rf build/test-results
}

run_tests() {
    local java_tests node_tests vitest_tests args t f i
    cd $REPO
    java_tests=$(python3 $HELPER java /home/test.patch)
    node_tests=$(python3 $HELPER node /home/test.patch)
    vitest_tests=$(python3 $HELPER vitest /home/test.patch)
    echo "JCG_JAVA=$java_tests"
    echo "JCG_NODE=$node_tests"
    echo "JCG_VITEST=$vitest_tests"
    if [ -n "$java_tests" ]; then
        rm -rf build/test-results/test
        for i in $(seq 1 15); do
            $GRADLE compileTestJava > /tmp/jcg_compile.log 2>&1
            grep -E ": error:" /tmp/jcg_compile.log | head -n 100
            python3 $HELPER broken /tmp/jcg_compile.log > /tmp/jcg_broken
            [ -s /tmp/jcg_broken ] || break
            while IFS= read -r f; do
                echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
                rm -f "$f"
            done < /tmp/jcg_broken
        done
        java_tests=$(python3 $HELPER java /home/test.patch)
        args=""
        for t in $java_tests; do
            args="$args --tests $t"
        done
        if [ -n "$args" ]; then
            $GRADLE --continue test $args > /tmp/jcg_gradle_test.log 2>&1
            tail -n 120 /tmp/jcg_gradle_test.log
        fi
        python3 $HELPER junit
    fi
    if [ -n "$node_tests" ]; then
        cd $REPO/ai-bridge
        node --test --test-reporter=/home/jcg_node_reporter.mjs $node_tests 2>&1
        cd $REPO
    fi
    if [ -n "$vitest_tests" ]; then
        cd $REPO/webview
        rm -f /tmp/jcg_vitest.json
        npx vitest run $vitest_tests --reporter=json --outputFile=/tmp/jcg_vitest.json > /tmp/jcg_vitest.log 2>&1
        tail -n 80 /tmp/jcg_vitest.log
        python3 $HELPER vitest_json /tmp/jcg_vitest.json
        cd $REPO
    fi
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR).replace("[[GRADLE]]", GRADLE).replace("[[WARM_TEST]]", WARM_TEST)


class JetbrainsCcGui_1585_to_1581_ImageBase(Image):
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
        return "eclipse-temurin:17-jdk-jammy"

    def image_tag(self) -> str:
        return "base-jdk17-node20"

    def workdir(self) -> str:
        return "base-jdk17-node20"

    def files(self) -> list[File]:
        return []

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
ENV GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.welcome=never"

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg git zip unzip python3 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/* && node --version && npm --version && java -version

WORKDIR /home/

{code}

RUN cd /home/{REPO_DIR}/webview && (npm ci --no-audit --no-fund || npm ci --no-audit --no-fund || true)

RUN cd /home/{REPO_DIR}/ai-bridge && (npm ci --no-audit --no-fund || npm ci --no-audit --no-fund || true)

RUN cd /home/{REPO_DIR} && chmod +x gradlew && ({GRADLE} compileTestJava || {GRADLE} compileTestJava || true) && ({GRADLE} test --tests {WARM_TEST} || true) && git -C /home/{REPO_DIR} clean -fdx -e node_modules

{self.clear_env}

"""


class JetbrainsCcGui_1585_to_1581_ImageDefault(Image):
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
        return JetbrainsCcGui_1585_to_1581_ImageBase(self.pr, self._config)

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
                "jcg_tests.py",
                TESTS_PY,
            ),
            File(
                ".",
                "jcg_node_reporter.mjs",
                NODE_REPORTER,
            ),
            File(
                ".",
                "jcg_tests.sh",
                TESTS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx -e node_modules
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/jcg_tests.sh setup
git clean -fdx -e node_modules -e build

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/jcg_tests.sh run

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
bash /home/jcg_tests.sh run

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
bash /home/jcg_tests.sh run

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

ENV GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.welcome=never"

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("zhukunpenglinyutong", "jetbrains_cc_gui_1585_to_1581")
class JetbrainsCcGui_1585_to_1581(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JetbrainsCcGui_1585_to_1581_ImageDefault(self.pr, self._config)

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
        re_result = re.compile(r"^(?:JUNIT|NODETEST|VITEST)\s+(PASSED|FAILED|SKIPPED)\s+(.+?)\s*$")

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


Instance.register("zhukunpenglinyutong", "jetbrains-cc-gui")(JetbrainsCcGui_1585_to_1581)
