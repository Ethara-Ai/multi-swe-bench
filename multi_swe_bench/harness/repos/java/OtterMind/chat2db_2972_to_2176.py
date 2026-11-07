from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "Chat2DB"
MAVEN_VERSION = "3.9.9"
NODE_VERSION = "22.22.2"
YARN_VERSION = "1.22.22"
MVN_FLAGS = "-B -ntp -Dmaven.test.skip=false -DskipTests=false"

TESTS_PY = r"""import os
import re
import sys
import xml.etree.ElementTree as ET

REPO = "/home/[[REPO_DIR]]"
SERVER = os.path.join(REPO, "chat2db-community-server")
SERVER_PREFIX = "chat2db-community-server/"
CLIENT_PREFIX = "chat2db-community-client/"
INCLUDE = re.compile(r"^(Test.*|.*Test|.*Tests|.*TestCase)\.java$")
BROKEN = re.compile(r"\[ERROR\]\s+(/\S+?/src/test/java/\S+?\.java):\[\d+")


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


def reactor(rel=""):
    pom = os.path.join(SERVER, rel, "pom.xml")
    try:
        root = ET.parse(pom).getroot()
    except (OSError, ET.ParseError):
        return set()
    found = {rel} if rel else set()
    for child in root:
        if local(child.tag) != "modules":
            continue
        for mod in child:
            if local(mod.tag) == "module" and mod.text and mod.text.strip():
                found |= reactor(os.path.normpath(os.path.join(rel, mod.text.strip())))
    return found


def modules(patch_file):
    mods = set()
    for p in patch_paths(patch_file):
        if p.startswith(SERVER_PREFIX) and "/src/test/" in p:
            mods.add(p[len(SERVER_PREFIX):p.index("/src/test/")])
    react = reactor()
    print(",".join(sorted(m for m in mods if m in react)))


def client(patch_file):
    print("1" if any(p.startswith(CLIENT_PREFIX) for p in patch_paths(patch_file)) else "0")


def broken(log_file):
    seen = []
    with open(log_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BROKEN.search(line)
            if m and m.group(1) not in seen and os.path.isfile(m.group(1)):
                seen.append(m.group(1))
    for f in seen:
        print(f)


def testlist(mods):
    out = set()
    for mod in filter(None, mods.split(",")):
        root = os.path.join(SERVER, mod, "src", "test", "java")
        for d, _, files in os.walk(root):
            for f in files:
                if INCLUDE.match(f):
                    out.add(os.path.relpath(os.path.join(d, f), root))
    print(",".join(sorted(out)))


def junit(mods):
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    results = {}
    for mod in filter(None, mods.split(",")):
        rdir = os.path.join(SERVER, mod, "target", "surefire-reports")
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
    {"modules": modules, "client": client, "broken": broken, "testlist": testlist, "junit": junit}[cmd](arg)
""".replace("[[REPO_DIR]]", REPO_DIR)

TESTS_SH = r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
SERVER=$REPO/chat2db-community-server
CLIENT=$REPO/chat2db-community-client
HELPER=/home/chat2db_tests.py
MVN="mvn [[MVN_FLAGS]]"
TSX_JOBS=3

client_setup() {
    cd $CLIENT
    for i in 1 2 3; do
        yarn install --frozen-lockfile --network-timeout 600000 && break
        sleep 30
    done
    yarn run postinstall
    test -x node_modules/.bin/tsx
}

server_setup() {
    cd $SERVER
    for i in 1 2 3; do
        $MVN test -Dtest=Chat2dbPrepareNoTests -Dsurefire.failIfNoSpecifiedTests=false && return 0
        sleep 30
    done
    $MVN -fn test -Dtest=Chat2dbPrepareNoTests -Dsurefire.failIfNoSpecifiedTests=false
}

setup() {
    set -e
    client_setup
    server_setup
}

java_run() {
    local mods="$1" tests f i
    cd $SERVER
    for m in ${mods//,/ }; do
        rm -rf "$m/target/test-classes" "$m/target/surefire-reports" "$m/target/maven-status/maven-compiler-plugin/testCompile"
    done
    for i in $(seq 1 15); do
        $MVN -fn -pl "$mods" -am test-compile > /tmp/c2d_compile.log 2>&1
        grep -E '^\[ERROR\]' /tmp/c2d_compile.log | head -n 200
        python3 $HELPER broken /tmp/c2d_compile.log > /tmp/c2d_broken
        [ -s /tmp/c2d_broken ] || break
        while IFS= read -r f; do
            echo "UNCOMPILABLE_TEST_SOURCE_REMOVED ${f#$REPO/}"
            rm -f "$f"
        done < /tmp/c2d_broken
    done
    tests=$(python3 $HELPER testlist "$mods")
    if [ -n "$tests" ]; then
        $MVN -fn -pl "$mods" -am test -Dtest="$tests" -Dsurefire.failIfNoSpecifiedTests=false 2>&1
    fi
    python3 $HELPER junit "$mods"
}

tsx_one() {
    local res="$1" n="$2" f="$3"
    cd $CLIENT
    timeout 600 node_modules/.bin/tsx "$f" > "$res/$n.log" 2>&1
    echo $? > "$res/$n.rc"
}

ts_run() {
    local res=/tmp/c2d_tsx n=0 f rc
    cd $CLIENT
    rm -rf $res
    mkdir -p $res
    find src -type f \( -name '*.test.ts' -o -name '*.test.tsx' \) -not -path '*/node_modules/*' -not -path 'src/.umi*' | sort > $res/files
    awk '{print NR" "$0}' $res/files | xargs -P $TSX_JOBS -L 1 bash /home/chat2db_tests.sh tsx-one $res
    while IFS= read -r f; do
        n=$((n+1))
        rc=$(cat "$res/$n.rc" 2>/dev/null || echo 1)
        if [ "$rc" = "0" ]; then
            echo "TSX PASSED chat2db-community-client/$f"
        else
            tail -n 60 "$res/$n.log" | sed 's/^/    /'
            echo "TSX FAILED chat2db-community-client/$f"
        fi
    done < $res/files
}

run_tests() {
    local mods client
    mods=$(python3 $HELPER modules /home/test.patch)
    client=$(python3 $HELPER client /home/test.patch)
    echo "C2D_JAVA_MODULES=$mods C2D_CLIENT=$client"
    if [ -n "$mods" ]; then
        java_run "$mods"
    fi
    if [ "$client" = "1" ]; then
        ts_run
    fi
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
    tsx-one) shift; tsx_one "$@" ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR).replace("[[MVN_FLAGS]]", MVN_FLAGS)


class Chat2DB_2972_to_2176_ImageBase(Image):
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
        return "base-jdk17-node22"

    def workdir(self) -> str:
        return "base-jdk17-node22"

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

RUN apt-get update && apt-get install -y --no-install-recommends git python3 curl ca-certificates xz-utils && rm -rf /var/lib/apt/lists/*

COPY untar.py /tmp/untar.py

RUN curl -fsSL --retry 5 https://archive.apache.org/dist/maven/maven-3/{MAVEN_VERSION}/binaries/apache-maven-{MAVEN_VERSION}-bin.tar.gz | python3 /tmp/untar.py /opt && ln -s /opt/apache-maven-{MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn && mvn -v

RUN set -e; case "$(dpkg --print-architecture)" in amd64) a=x64 ;; arm64) a=arm64 ;; *) exit 1 ;; esac; curl -fsSL --retry 5 https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$a.tar.xz | python3 /tmp/untar.py /usr/local 1; rm -f /tmp/untar.py; npm install -g yarn@{YARN_VERSION}; node --version; yarn --version

WORKDIR /home/

{code}

RUN cd /home/{REPO_DIR}/chat2db-community-client && (yarn install --frozen-lockfile --network-timeout 600000 || yarn install --frozen-lockfile --network-timeout 600000 || true)

RUN cd /home/{REPO_DIR}/chat2db-community-server && (mvn {MVN_FLAGS} -fn test -Dtest=Chat2dbPrepareNoTests -Dsurefire.failIfNoSpecifiedTests=false || true) && git -C /home/{REPO_DIR} clean -fdx -e node_modules

{self.clear_env}

"""


class Chat2DB_2972_to_2176_ImageDefault(Image):
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
        return Chat2DB_2972_to_2176_ImageBase(self.pr, self._config)

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
                "chat2db_tests.py",
                TESTS_PY,
            ),
            File(
                ".",
                "chat2db_tests.sh",
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

bash /home/chat2db_tests.sh setup

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/chat2db_tests.sh run

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
bash /home/chat2db_tests.sh run

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
bash /home/chat2db_tests.sh run

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


@Instance.register("OtterMind", "Chat2DB_2972_to_2176")
class Chat2DB_2972_to_2176(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Chat2DB_2972_to_2176_ImageDefault(self.pr, self._config)

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
        re_result = re.compile(r"^(?:JUNIT|TSX)\s+(PASSED|FAILED|SKIPPED)\s+(.+?)\s*$")

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


Instance.register("OtterMind", "Chat2DB")(Chat2DB_2972_to_2176)
