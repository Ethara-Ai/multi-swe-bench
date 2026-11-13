
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "apache"
_INTERVAL = "beam_39203_to_39203"
_BASE_IMAGE = "gradle:8.14.3-jdk17"
_REPO_DIR = "beam"
_GRADLE_FLAGS = "-PdisableSpotlessCheck=true -PdisableCheckStyle=true"

_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""

_GRADLE_ENV = r"""export GRADLE_USER_HOME=/opt/gradle-home
mkdir -p "$GRADLE_USER_HOME/init.d"
cat > "$GRADLE_USER_HOME/gradle.properties" <<'PROPEOF'
org.gradle.daemon=false
org.gradle.console=plain
org.gradle.parallel=false
org.gradle.jvmargs=-Xmx3g -Xss10240k -XX:+UseParallelGC -Dfile.encoding=UTF-8
org.gradle.workers.max=4
org.gradle.caching=false
org.gradle.vfs.watch=false
org.gradle.java.installations.auto-download=false
PROPEOF
cat > "$GRADLE_USER_HOME/init.d/msb-tests.gradle" <<'INITEOF'
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
        maxParallelForks = 1
        maxHeapSize = "2g"
    }
}
INITEOF
"""

_GO_ENV = r"""export PATH=/opt/go/bin:$PATH
export GOPATH=/opt/go-path
export GOMODCACHE=/opt/go-path/pkg/mod
export GOCACHE=/opt/go-cache
export GOTOOLCHAIN=local
"""

_COLLECT = r"""declare -A FILTERS
declare -A GO_PKGS
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    sdks/go/*_test.go)
      d="${f%/*}"
      if [ -d "$d" ]; then
        GO_PKGS[$d]=1
      fi
      ;;
    */src/test/java/*.java)
      mod="${f%%/src/test/java/*}"
      cls="${f#*/src/test/java/}"
      cls="${cls%.java}"
      FILTERS[$mod]="${FILTERS[$mod]} --tests ${MSB_TEST_FILTER:-${cls//\//.}}"
      ;;
  esac
done
TASKS=""
for mod in "${!FILTERS[@]}"; do
  if [ -d "$mod" ]; then
    TASKS="$TASKS :${mod//\//:}:test${FILTERS[$mod]}"
  fi
done
"""

_NET_RE = r"(Could not (resolve|GET|HEAD|get resource|download)|timed out|Connection reset|connection refused|TLS handshake|i/o timeout|unexpected EOF|dial tcp)"

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[GRADLE_ENV]]
[[GO_ENV]]
[[COLLECT]]
echo "MSB_TEST_TASKS=$TASKS"
echo "MSB_GO_PKGS=${!GO_PKGS[*]}"
find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
rm -f /tmp/msb-go.xml
rc=0
for d in "${!GO_PKGS[@]}"; do
  pkg="./${d#sdks/}"
  (cd sdks && GOPROXY=off timeout -k 30 1200 go test -count=1 -v "$pkg") > /tmp/msb-go.log 2>&1 || rc=$?
  cat /tmp/msb-go.log
  awk -v cls="$d" '
    function esc(s) { gsub(/&/, "\\&amp;", s); gsub(/</, "\\&lt;", s); gsub(/>/, "\\&gt;", s); gsub(/"/, "\\&quot;", s); return s }
    /^[ \t]*--- (PASS|FAIL|SKIP): / {
      line = $0
      sub(/^[ \t]*--- /, "", line)
      status = substr(line, 1, 4)
      name = substr(line, 7)
      sub(/ \([0-9.]+s\)$/, "", name)
      body = ""
      if (status == "FAIL") body = "<failure/>"
      if (status == "SKIP") body = "<skipped/>"
      printf "<testcase classname=\"%s\" name=\"%s\">%s</testcase>\n", esc(cls), esc(name), body
    }
  ' /tmp/msb-go.log >> /tmp/msb-go.xml
done
if [ -n "$TASKS" ]; then
  timeout -k 60 5400 ./gradlew --continue [[GRADLE_FLAGS]] $TASKS || rc=$?
fi
echo "[[BEGIN]]"
for report in /tmp/msb-go.xml $(find . -path '*/build/test-results/*' -name 'TEST-*.xml' | sort); do
  [ -f "$report" ] || continue
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[GRADLE_ENV]]", _GRADLE_ENV)
        .replace("[[GO_ENV]]", _GO_ENV)
        .replace("[[COLLECT]]", _COLLECT)
        .replace("[[NET_RE]]", _NET_RE)
        .replace("[[GRADLE_FLAGS]]", _GRADLE_FLAGS)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Beam39203To39203ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class Beam39203To39203ImageDefault(Image):
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
        return Beam39203To39203ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = _render(
            """#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

[[GRADLE_ENV]]
DIST_URL=$(sed -n 's/^distributionUrl=//p' gradle/wrapper/gradle-wrapper.properties | tr -d '\\\\')
DIST_ZIP=$(basename "$DIST_URL")
DIST_HASH=$(python3 -c '
import hashlib, sys
n = int(hashlib.md5(sys.argv[1].encode()).hexdigest(), 16)
d = "0123456789abcdefghijklmnopqrstuvwxyz"
s = ""
while n:
    n, r = divmod(n, 36)
    s = d[r] + s
print(s)
' "$DIST_URL")
DIST_DIR="$GRADLE_USER_HOME/wrapper/dists/${DIST_ZIP%.zip}/$DIST_HASH"
if [ ! -f "$DIST_DIR/$DIST_ZIP.ok" ]; then
  mkdir -p "$DIST_DIR"
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$DIST_DIR/$DIST_ZIP" "$DIST_URL"
  (cd "$DIST_DIR" && unzip -q "$DIST_ZIP" && touch "$DIST_ZIP.ok")
fi
[[GO_ENV]]
GO_VERSION=$(sed -n 's/^toolchain go//p' sdks/go.mod | head -1)
[ -n "$GO_VERSION" ] || GO_VERSION=$(sed -n 's/^go //p' sdks/go.mod | head -1)
if [ ! -x /opt/go/bin/go ]; then
  case "$(uname -m)" in
    aarch64|arm64) GO_ARCH=arm64 ;;
    *) GO_ARCH=amd64 ;;
  esac
  GO_TGZ=/tmp/go.tar.gz
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$GO_TGZ" "https://go.dev/dl/go$GO_VERSION.linux-$GO_ARCH.tar.gz"
  mkdir -p /opt/go
  tar -xzf "$GO_TGZ" -C /opt/go --strip-components=1
  rm -f "$GO_TGZ"
fi
go version
MSB_TEST_FILTER=MsbWarmupNoSuchTest
[[COLLECT]]
echo "MSB_WARMUP_TASKS=$TASKS"
echo "MSB_WARMUP_GO_PKGS=${!GO_PKGS[*]}"
msb_retry() {
  local i
  for i in 1 2 3 4 5; do
    if "$@" > /tmp/msb-warmup.log 2>&1; then
      tail -5 /tmp/msb-warmup.log
      return 0
    fi
    tail -60 /tmp/msb-warmup.log
    if ! grep -qiE '[[NET_RE]]' /tmp/msb-warmup.log; then
      echo "prepare: non-network failure, not retrying" >&2
      return 1
    fi
    sleep 60
  done
  return 1
}
for d in "${!GO_PKGS[@]}"; do
  msb_retry bash -c "cd sdks && go test -count=1 -run MsbWarmupNoSuchTest ./${d#sdks/}/" || { echo "prepare: go warm-up failed" >&2; exit 1; }
done
if [ -n "$TASKS" ]; then
  msb_retry ./gradlew --continue [[GRADLE_FLAGS]] $TASKS || { echo "prepare: gradle warm-up failed" >&2; exit 1; }
fi
git status --porcelain
bash /home/check_git_changes.sh

"""
        ).replace("[[SHA]]", self.pr.base.sha).replace("[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")

        apply_and_run = """#!/bin/bash

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", "#!/bin/bash\n\n" + _render(_RUN_TESTS)),
            File(
                ".",
                "test-run.sh",
                apply_and_run.format(repo=_REPO_DIR, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                apply_and_run.format(
                    repo=_REPO_DIR, patches="/home/test.patch /home/fix.patch"
                ),
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


@Instance.register(_ORG, _INTERVAL)
class Beam39203To39203(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Beam39203To39203ImageDefault(self.pr, self._config)

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

        start = test_log.find(_REPORTS_BEGIN)
        end = test_log.rfind(_REPORTS_END)
        if start == -1 or end == -1 or end < start:
            reports = ""
        else:
            reports = test_log[start + len(_REPORTS_BEGIN) : end]

        for m in _CASE_RE.finditer(reports):
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            name = f"{attrs.get('classname', '')}#{attrs.get('name', '')}"
            body = m.group(3) or ""
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
