import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_BODY = """\
QUARANTINED=/tmp/quarantined.txt
QUARANTINE_DIR=/tmp/quarantine
: > "$QUARANTINED"
rm -rf "$QUARANTINE_DIR"

attempt=0
while :; do
    attempt=$((attempt + 1))
    rm -rf sechub-pds/build/test-results sechub-pds/build/reports/tests

    set +e
    ./gradlew --no-daemon --console=plain --init-script /home/junit_names.gradle :sechub-pds:test 2>&1 | tee /tmp/gradle.out
    GRADLE_RC=$?
    set -e
    echo "NOTE: gradlew exited ${GRADLE_RC} (attempt ${attempt})"

    if ! grep -q "compileTestJava FAILED" /tmp/gradle.out; then
        break
    fi
    if [ "${attempt}" -ge 3 ]; then
        echo "NOTE: giving up after ${attempt} compileTestJava failures"
        break
    fi

    BAD=$(python3 /home/quarantine.py detect /tmp/gradle.out)
    if [ -z "${BAD}" ]; then
        break
    fi
    for bad in ${BAD}; do
        if git cat-file -e "HEAD:${bad}" 2>/dev/null; then
            git checkout HEAD -- "${bad}"
            echo "NOTE: reverted ${bad} to the base revision"
        else
            mkdir -p "${QUARANTINE_DIR}/$(dirname "${bad}")"
            mv "${bad}" "${QUARANTINE_DIR}/${bad}"
            echo "${bad}" >> "${QUARANTINED}"
            echo "NOTE: quarantined uncompilable new test source ${bad}"
        fi
    done
done

python3 /home/quarantine.py emit "${QUARANTINED}" "${QUARANTINE_DIR}"
python3 /home/collect_results.py

grep -qE "^BUILD (SUCCESSFUL|FAILED)" /tmp/gradle.out
"""


class SechubImageBase(Image):
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
        return "eclipse-temurin:11-jdk-jammy"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV GRADLE_USER_HOME=/root/.gradle

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git python3 unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.gradle \\
    && printf 'org.gradle.jvmargs=-Xmx4g\\n' > /root/.gradle/gradle.properties

{code}

{self.clear_env}

"""


class SechubImageDefault(Image):
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
        return SechubImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "junit_names.gradle",
                """allprojects {
    tasks.withType(Test).configureEach {
        systemProperty 'junit.jupiter.params.displayname.default', '{displayName} [{index}] {argumentsWithNames}'
    }
}
""",
            ),
            File(
                ".",
                "collect_results.py",
                '''#!/usr/bin/env python3
import glob
import os
import xml.etree.ElementTree as ET

REPO = "/home/{repo}"

found = 0
pattern = os.path.join(REPO, "*", "build", "test-results", "test", "*.xml")
for xml_path in sorted(glob.glob(pattern)):
    module = os.path.relpath(xml_path, REPO).split(os.sep)[0]
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        continue
    for case in root.iter("testcase"):
        classname = (case.get("classname") or "").strip()
        name = (case.get("name") or "").strip()
        if not classname or not name:
            continue
        if name.endswith("()"):
            name = name[:-2]
        if case.find("skipped") is not None:
            status = "SKIPPED"
        elif case.find("failure") is not None or case.find("error") is not None:
            status = "FAILED"
        else:
            status = "PASSED"
        outer = classname.split("$", 1)[0].replace(".", "/")
        src = "{{0}}/src/test/java/{{1}}.java".format(module, outer)
        print("TEST_RESULT|{{0}}|{{1}}::{{2}}#{{3}}".format(status, src, classname, name))
        found += 1

print("TEST_RESULT_TOTAL|{{0}}".format(found))
'''.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "quarantine.py",
                '''#!/usr/bin/env python3
import os
import re
import sys

REPO = "/home/{repo}"

_ERROR_LINE = re.compile(r"^(\\S+\\.java):[0-9]+: error:")
_PLAIN_TEST = re.compile(r"^@Test\\b")
_METHOD = re.compile(r"(\\w+)\\s*\\(")
_PACKAGE = re.compile(r"^\\s*package\\s+([\\w.]+)\\s*;", re.MULTILINE)
_UNRECONSTRUCTABLE = ("@ParameterizedTest", "@RepeatedTest", "@TestTemplate",
                      "@TestFactory", "@Nested")


def detect(gradle_log):
    found = []
    with open(gradle_log, errors="replace") as handle:
        for line in handle:
            match = _ERROR_LINE.match(line.strip())
            if not match:
                continue
            path = match.group(1)
            if os.path.isabs(path):
                if not path.startswith(REPO + os.sep):
                    continue
                path = os.path.relpath(path, REPO)
            if "/src/test/" not in path:
                continue
            if path not in found:
                found.append(path)
    return found


def _test_methods(text):
    lines = text.splitlines()
    names = []
    for index, line in enumerate(lines):
        if not _PLAIN_TEST.match(line.strip()):
            continue
        for probe in lines[index + 1:]:
            stripped = probe.strip()
            if not stripped or stripped.startswith("@") or stripped.startswith("//"):
                continue
            match = _METHOD.search(stripped)
            if match and match.group(1) not in names:
                names.append(match.group(1))
            break
    return names


def emit(list_file, quarantine_root):
    if not os.path.exists(list_file):
        return
    with open(list_file) as handle:
        for raw in handle:
            rel = raw.strip()
            if not rel:
                continue
            source = os.path.join(quarantine_root, rel)
            if not os.path.exists(source):
                continue
            text = open(source, errors="replace").read()
            if any(marker in text for marker in _UNRECONSTRUCTABLE):
                print("NOTE: not naming tests in {{0}} (unreconstructable ids)".format(rel))
                continue
            package = _PACKAGE.search(text)
            cls = os.path.basename(rel)[:-len(".java")]
            fqcn = "{{0}}.{{1}}".format(package.group(1), cls) if package else cls
            for method in _test_methods(text):
                print("TEST_RESULT|FAILED|{{0}}::{{1}}#{{2}}".format(rel, fqcn, method))


if __name__ == "__main__":
    if sys.argv[1:2] == ["detect"]:
        for path in detect(sys.argv[2]):
            print(path)
    elif sys.argv[1:2] == ["emit"]:
        emit(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("usage: quarantine.py detect <log> | emit <list> <dir>")
'''.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

git config --global --add safe.directory /home/{pr.repo}
chmod +x gradlew

./gradlew --no-daemon --console=plain --init-script /home/junit_names.gradle :sechub-pds:test || true

rm -rf sechub-pds/build/test-results sechub-pds/build/reports/tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RESULT_LINE = re.compile(r"^TEST_RESULT\|(PASSED|FAILED|SKIPPED)\|(.+)$")


def parse_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if not match:
            continue
        status, name = match.group(1), match.group(2).strip()
        if not name:
            continue
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


@Instance.register("mercedes-benz", "sechub")
class Sechub(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SechubImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_log(log)
