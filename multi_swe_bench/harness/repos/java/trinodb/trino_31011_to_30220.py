import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "trinodb"
_INTERVAL = "trino_31011_to_30220"
_BASE_IMAGE = "maven:3.9.11-eclipse-temurin-25"
_REPO_DIR = "trino"

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

_MAVEN_ENV = r"""export MAVEN_OPTS="-Xmx3g -XX:+UseParallelGC -Dfile.encoding=UTF-8"
MSB_IDLE_LIMIT=1500
MVN_FLAGS="-B -ntp -Dstyle.color=never -Dmaven.wagon.http.retryHandler.count=5 -Dmaven.wagon.httpconnectionManager.ttlSeconds=25 -Daether.connector.http.retryHandler.count=5 -Daether.connector.requestTimeout=120000 -Daether.connector.connectTimeout=30000 -Daether.connector.http.connectionMaxTtl=60 -Dcheckstyle.skip=true -Denforcer.skip=true -Dlicense.skip=true -Dspotless.check.skip=true -Dspotless.apply.skip=true -Dmaven.javadoc.skip=true -Djacoco.skip=true -Dgpg.skip=true -Dsonar.skip=true -Dmaven.source.skip=true -Dair.check.skip-all=true -Dair.test.jvmsize=2g -Dmaven.gitcommitid.skip=true -Dskip.installbun=true -Dskip.bun=true -DforkCount=1"
if [ -x /opt/msb-maven/bin/mvn ]; then
  export PATH=/opt/msb-maven/bin:$PATH
fi
module_of() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    if [ -f "$d/pom.xml" ]; then
      echo "$d"
      return
    fi
    d=$(dirname "$d")
  done
}
patch_files() {
  grep -oE '^diff --git a/[^ ]+' "$@" | sed 's/^.*diff --git a\///' | sort -u
}
modules_of_files() {
  local f m
  for f in "$@"; do
    case "$f" in
      *.java|*.g4) ;;
      *) continue ;;
    esac
    case "$f" in
      */src/main/*|*/src/test/*) ;;
      *) continue ;;
    esac
    m=$(module_of "$f")
    [ -n "$m" ] && echo "$m"
  done | sort -u
}
test_classes() {
  local f c
  for f in $(patch_files /home/test.patch); do
    case "$f" in
      */src/test/java/*.java) ;;
      *) continue ;;
    esac
    c="${f#*/src/test/java/}"
    c="${c%.java}"
    echo "${c//\//.}"
  done | sort -u
}
module_tests() {
  local f c b
  for f in $(patch_files /home/test.patch); do
    case "$f" in
      "$1"/src/test/java/*.java) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    b=$(basename "$f" .java)
    case "$b" in
      Test*|*Test|*Tests) ;;
      *) continue ;;
    esac
    grep -q '@Test' "$f" || continue
    grep -qE 'abstract class' "$f" && continue
    c="${f#*/src/test/java/}"
    c="${c%.java}"
    echo "${c//\//.}"
  done | sort -u
}
join_by_comma() {
  local IFS=,
  echo "$*"
}
warm_pairs() {
  local m t c
  for m in $(echo "$1" | tr ',' ' '); do
    t=$(find "$m/src/test/java" \( -name 'Test*.java' -o -name '*Test.java' \) -size -8k -printf '%s %p\n' 2>/dev/null | sort -n | cut -d' ' -f2- | while read -r x; do
      if grep -q '@Test' "$x" && ! grep -qE 'abstract class|extends |@Disabled|@Ignore|QueryRunner|Container|Docker|Minio|Hms|Smoke' "$x"; then
        echo "$x"
        break
      fi
    done)
    if [ -n "$t" ]; then
      c="${t#*/src/test/java/}"
      c="${c%.java}"
      echo "$m ${c//\//.}"
    fi
  done | sort -u
}
msb_network_failure() {
  grep -qE 'Could not transfer artifact|Could not resolve dependencies|Failed to read artifact descriptor|Connection reset|Connection refused|timed out|Read timed out|UnknownHostException|Name or service not known|Temporary failure in name resolution|Remote host terminated|Premature end of Content-Length|mvn_watch: stalled' /tmp/msb-last.log 2>/dev/null
}
mvn_watch() {
  local log pid size last idle
  log=$(mktemp /tmp/msb-mvn.XXXXXX)
  "$@" > "$log" 2>&1 &
  pid=$!
  tail -n +1 -f --pid="$pid" "$log" &
  last=0
  idle=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    size=$(stat -c %s "$log")
    if [ "$size" = "$last" ]; then
      idle=$((idle + 15))
    else
      idle=0
      last=$size
    fi
    if [ "$idle" -ge "$MSB_IDLE_LIMIT" ]; then
      echo "mvn_watch: no output for ${idle}s, killing stalled build" >&2
      echo "mvn_watch: stalled" >> "$log"
      kill "$pid" 2>/dev/null
      sleep 5
      kill -9 "$pid" 2>/dev/null
    fi
  done
  wait "$pid"
  local rc=$?
  sleep 1
  cp "$log" /tmp/msb-last.log
  rm -f "$log"
  return $rc
}
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[MAVEN_ENV]]
TEST_MODULES=$(modules_of_files $(patch_files /home/test.patch))
CHANGED_MODULES=$(modules_of_files $( (git diff --name-only HEAD; git ls-files --others --exclude-standard) | sort -u))
MODULES=$(join_by_comma $(printf '%s\n' $TEST_MODULES $CHANGED_MODULES | sort -u))
TESTS=$(join_by_comma $(test_classes))
echo "MSB_MODULES=$MODULES"
echo "MSB_TESTS=$TESTS"
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
rc=0
if [ -n "$MODULES" ] && [ -n "$TESTS" ]; then
  timeout -k 60 3600 mvn -o $MVN_FLAGS install -fae -DskipTests -pl "$MODULES" || rc=$?
  for m in $TEST_MODULES; do
    MT=$(join_by_comma $(module_tests "$m"))
    echo "MSB_MODULE_TESTS[$m]=$MT"
    [ -n "$MT" ] || continue
    timeout -k 60 3600 mvn -o $MVN_FLAGS test -pl "$m" -Dtest="$MT" -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dsurefire.redirectTestOutputToFile=true || rc=$?
  done
fi
echo "[[BEGIN]]"
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' | sort | while read -r report; do
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' -e '/<properties>/,/<\/properties>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""

_PREPARE = r"""#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

[[MAVEN_ENV]]
TEST_MODULES=$(join_by_comma $(modules_of_files $(patch_files /home/test.patch)))
ALL_MODULES=$(join_by_comma $(modules_of_files $(patch_files /home/test.patch /home/fix.patch)))
echo "prepare: test modules=$TEST_MODULES all modules=$ALL_MODULES"
DIST_URL=$(sed -n 's/^distributionUrl=//p' .mvn/wrapper/maven-wrapper.properties | tr -d '\r')
DIST_URL="${DIST_URL%-bin.zip}-bin.tar.gz"
if [ ! -x /opt/msb-maven/bin/mvn ]; then
  mkdir -p /opt/msb-maven
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o /tmp/msb-maven.tar.gz "$DIST_URL"
  tar -xzf /tmp/msb-maven.tar.gz -C /opt/msb-maven --strip-components=1
  rm -f /tmp/msb-maven.tar.gz
fi
export PATH=/opt/msb-maven/bin:$PATH
mvn -v
[ -n "$TEST_MODULES" ] || { echo "prepare: no test modules" >&2; exit 1; }
WARM_PAIRS=$(warm_pairs "$ALL_MODULES")
WARM_MODULES=$(join_by_comma $(echo "$WARM_PAIRS" | awk 'NF==2{print $1}'))
WARM_TESTS=$(join_by_comma $(echo "$WARM_PAIRS" | awk 'NF==2{print $2}'))
CHECK_MODULES=$(join_by_comma $(for m in $(echo "$TEST_MODULES" | tr ',' ' '); do echo "$WARM_PAIRS" | awk -v m="$m" '$1==m{print $1}'; done))
echo "prepare: warm-up modules=$WARM_MODULES tests=$WARM_TESTS check modules=$CHECK_MODULES"
[ -n "$WARM_TESTS" ] || { echo "prepare: no warm-up tests" >&2; exit 1; }
[ -n "$CHECK_MODULES" ] || { echo "prepare: no test module has a warm-up test" >&2; exit 1; }
ok=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  if mvn_watch mvn $MVN_FLAGS install -DskipTests -pl "$ALL_MODULES" -am && mvn_watch mvn $MVN_FLAGS test -pl "$WARM_MODULES" -Dtest="$WARM_TESTS" -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true; then
    ok=1
    break
  fi
  if ! msb_network_failure; then
    echo "prepare: maven failed for a non-network reason, not retrying" >&2
    break
  fi
  sleep 20
done
[ "$ok" = 1 ] || { echo "prepare: maven warm-up failed" >&2; exit 1; }
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
mvn -o $MVN_FLAGS test -pl "$CHECK_MODULES" -Dtest="$WARM_TESTS" -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true || { echo "prepare: offline check failed" >&2; exit 1; }
[ -n "$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -print -quit)" ] || { echo "prepare: offline check produced no test reports" >&2; exit 1; }
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
KEEP_MODULES=" $(echo "$ALL_MODULES" | tr ',' ' ') "
find . -mindepth 2 -maxdepth 4 -type d -name target -prune | while read -r t; do
  m="${t#./}"
  m="${m%/target}"
  case "$KEEP_MODULES" in
    *" $m "*) ;;
    *) rm -rf "$t" ;;
  esac
done
rm -rf /tmp/msb-mvn.* /tmp/msb-last.log
bash /home/check_git_changes.sh

"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[MAVEN_ENV]]", _MAVEN_ENV)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Trino31011To30220ImageBase(Image):
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


class Trino31011To30220ImageDefault(Image):
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
        return Trino31011To30220ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = _render(_PREPARE).replace("[[SHA]]", self.pr.base.sha).replace("[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git")

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
class Trino31011To30220(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Trino31011To30220ImageDefault(self.pr, self._config)

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
