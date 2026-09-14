import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-328_to_328"

_PRUNE_BLOCK = """RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi
"""

_EMIT_RESULTS_PY = """import glob
import os
import sys
import xml.etree.ElementTree as ET

repo = sys.argv[1]
expected = sys.argv[2] if len(sys.argv) > 2 else ""
results = {}

for path in sorted(glob.glob(repo + "/**/test-results/test/TEST-*.xml", recursive=True)):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    for case in root.iter("testcase"):
        cls = (case.get("classname") or "").strip()
        name = (case.get("name") or "").strip()
        if not cls or not name:
            continue
        status = "PASSED"
        for child in case:
            tag = child.tag.split("}")[-1].lower()
            if tag in ("failure", "error"):
                status = "FAILED"
                break
            if tag == "skipped":
                status = "SKIPPED"
                break
        key = cls + " > " + name
        if status == "FAILED" or key not in results:
            results[key] = status

if expected and os.path.exists(expected):
    for line in open(expected):
        key = line.strip()
        if key and key not in results:
            results[key] = "FAILED"

for key, status in results.items():
    print("TESTCASE " + status + " " + key)
sys.stderr.write("emit_results: %d test cases\\n" % len(results))
"""

_FETCH_GRADLE_PY = """import sys
import time
import urllib.request

props = "gradle/wrapper/gradle-wrapper.properties"
dest = "/home/gradle-dist.zip"
sep = chr(92)

with open(props) as handle:
    lines = handle.read().splitlines()

url = ""
for line in lines:
    if line.startswith("distributionUrl="):
        url = line.split("=", 1)[1].strip().replace(sep + ":", ":")

if not url:
    raise SystemExit("distributionUrl not found in " + props)

if url.startswith("file:"):
    sys.stderr.write("fetch_gradle: already local, nothing to do" + chr(10))
    raise SystemExit(0)

error = None
for attempt in range(1, 6):
    try:
        with urllib.request.urlopen(url, timeout=600) as response:
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(1048576)
                    if not chunk:
                        break
                    out.write(chunk)
        error = None
        break
    except Exception as exc:
        error = exc
        sys.stderr.write("fetch_gradle: attempt %d/5 failed: %s%s" % (attempt, exc, chr(10)))
        time.sleep(attempt * 20)

if error is not None:
    raise SystemExit("could not download " + url + ": " + str(error))

out_lines = []
for line in lines:
    if line.startswith("distributionUrl="):
        out_lines.append("distributionUrl=file" + sep + ":///home/gradle-dist.zip")
    else:
        out_lines.append(line)

with open(props, "w") as handle:
    handle.write(chr(10).join(out_lines) + chr(10))

sys.stderr.write("fetch_gradle: " + url + " -> " + dest + chr(10))
"""

_PREPARE_SH = """#!/bin/bash
set -e

export JAVA_TOOL_OPTIONS="-Djava.net.preferIPv4Stack=true"

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

chmod +x gradlew
python3 /home/fetch_gradle.py
printf 'org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=768m\\norg.gradle.daemon=false\\n' > gradle.properties

for attempt in 1 2 3; do
    rm -rf build/test-results/test
    ./gradlew test --no-daemon --console=plain --continue || true
    if ls build/test-results/test/TEST-*.xml > /dev/null 2>&1; then break; fi
    sleep 30
done

python3 /home/emit_results.py /home/__REPO__ > /home/baseline-tests.txt
test -s /home/baseline-tests.txt
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

export JAVA_TOOL_OPTIONS="-Djava.net.preferIPv4Stack=true"

cd /home/__REPO__

EXPECTED=""
while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ -n "$(git status --porcelain -- "$f")" ]; then
        EXPECTED=/home/expected_tests.txt
        break
    fi
done < /home/patched_tests.txt
echo "===== test patch applied: $([ -n "$EXPECTED" ] && echo yes || echo no) ====="

rm -rf build/test-results/test
./gradlew test --no-daemon --console=plain --continue
echo "===== gradle exit: $? ====="

if [ -n "$EXPECTED" ] && ! ls build/test-results/test/TEST-*.xml > /dev/null 2>&1; then
    echo "===== no results, restoring baseline test sources ====="
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        git checkout HEAD -- "$f" 2>/dev/null || rm -f "$f"
    done < /home/patched_tests.txt
    rm -rf build/test-results/test
    ./gradlew test --no-daemon --console=plain --continue
    echo "===== gradle exit: $? ====="
fi

echo "===== test results ====="
python3 /home/emit_results.py /home/__REPO__ "$EXPECTED"
exit 0
"""

_RUN_SH = """#!/bin/bash
set -e
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/.+?"? "?b/(.+?)"?$')
_TEST_PATH_RE = re.compile(r"(?:^|/)src/test/java/(.+)\.java$")
_ANNOTATION_RE = re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest|TestTemplate)\b")
_METHOD_RE = re.compile(r"\bvoid\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")


def _test_patch_targets(pr: PullRequest) -> tuple[list[str], list[str]]:
    files: list[str] = []
    ids: list[str] = []
    path = None
    pending = False
    for line in (pr.test_patch or "").replace("\r", "").split("\n"):
        header = _DIFF_FILE_RE.match(line)
        if header:
            path = header.group(1) if _TEST_PATH_RE.search(header.group(1)) else None
            if path and path not in files:
                files.append(path)
            pending = False
        elif path and line.startswith("+"):
            body = line[1:]
            if _ANNOTATION_RE.match(body):
                pending = True
            elif pending:
                method = _METHOD_RE.search(body)
                if method:
                    fqcn = _TEST_PATH_RE.search(path).group(1).replace("/", ".")
                    ids.append(fqcn + " > " + method.group(1))
                    pending = False
    return files, ids


def _render(template: str, pr: PullRequest) -> str:
    return template.replace("__REPO__", pr.repo).replace("__BASE_SHA__", pr.base.sha)


class VonageJavaSdkImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git openjdk-8-jdk python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/lib/jvm/java-8-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-8-openjdk

ENV JAVA_HOME=/usr/lib/jvm/java-8-openjdk GRADLE_USER_HOME=/home/gradle-home
ENV PATH=/usr/lib/jvm/java-8-openjdk/bin:$PATH

RUN git config --global --add safe.directory '*'

RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}

CMD ["/bin/bash"]
"""


class VonageJavaSdkImageDefault(Image):
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
        return VonageJavaSdkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        files, ids = _test_patch_targets(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "patched_tests.txt", "".join(f + "\n" for f in files)),
            File(".", "expected_tests.txt", "".join(i + "\n" for i in ids)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "fetch_gradle.py", _FETCH_GRADLE_PY),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        prune = _PRUNE_BLOCK.format(repo=self.pr.repo, sha=self.pr.base.sha)

        return f"""FROM {base.image_name()}:{base.image_tag()}

{copy_commands}
RUN bash /home/prepare.sh

{prune}"""


@Instance.register("Vonage", "vonage-java-sdk")
class VONAGE_JAVA_SDK(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Union[str, Image]:
        return VonageJavaSdkImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in test_log.replace("\r", "").split("\n"):
            match = _TESTCASE_RE.match(line.rstrip())
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("Vonage", "vonage_java_sdk")(VONAGE_JAVA_SDK)
