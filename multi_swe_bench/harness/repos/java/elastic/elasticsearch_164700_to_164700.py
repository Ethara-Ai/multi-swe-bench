
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "elastic"
_INTERVAL = "elasticsearch_164700_to_164700"
_BASE_IMAGE = "gradle:9.2.1-jdk21"
_REPO_DIR = "elasticsearch"

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
export RUNTIME_JAVA_HOME="$JAVA_HOME"
mkdir -p "$GRADLE_USER_HOME/init.d"
cat > "$GRADLE_USER_HOME/gradle.properties" <<'PROPEOF'
org.gradle.daemon=false
org.gradle.console=plain
org.gradle.parallel=false
org.gradle.jvmargs=-Xmx3g -XX:+UseParallelGC -Dfile.encoding=UTF-8 -Djava.net.preferIPv4Stack=true
org.gradle.java.installations.auto-download=false
systemProp.java.net.preferIPv4Stack=true
systemProp.org.gradle.internal.http.connectionTimeout=60000
systemProp.org.gradle.internal.http.socketTimeout=120000
systemProp.org.gradle.internal.repository.max.retries=10
systemProp.org.gradle.internal.repository.initial.backoff=2000
systemProp.tests.jvms=2
PROPEOF
MSB_JDKS=$(ls -d /opt/msb-jdk-* 2>/dev/null | paste -sd, -)
if [ -n "$MSB_JDKS" ]; then
  echo "org.gradle.java.installations.paths=$MSB_JDKS" >> "$GRADLE_USER_HOME/gradle.properties"
fi
cat > "$GRADLE_USER_HOME/init.d/msb-tests.gradle" <<'INITEOF'
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
        doFirst {
            ignoreFailures = true
            filter.failOnNoMatchingTests = false
        }
    }
}
INITEOF
git config --global --get-all safe.directory | grep -qx "\*" || git config --global --add safe.directory "*"
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[GRADLE_ENV]]
declare -A FILTERS
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    */src/*/java/*.java) ;;
    *) continue ;;
  esac
  mod="${f%%/src/*}"
  rest="${f#"$mod"/src/}"
  set="${rest%%/*}"
  case "$set" in
    test|internalClusterTest) ;;
    *) continue ;;
  esac
  cls="${rest#"$set"/java/}"
  cls="${cls%.java}"
  FILTERS[$mod@$set]="${FILTERS[$mod@$set]} --tests ${cls//\//.}"
done
TASKS=""
for key in "${!FILTERS[@]}"; do
  mod="${key%@*}"
  TASKS="$TASKS :${mod//\//:}:${key#*@}${FILTERS[$key]}"
done
echo "MSB_TEST_TASKS=$TASKS"
find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
rc=0
chown -R gradle:gradle "$PWD" "$GRADLE_USER_HOME"
if [ -n "$TASKS" ]; then
  timeout -k 60 3600 runuser -u gradle -- ./gradlew --continue --offline $TASKS || rc=$?
fi
echo "[[BEGIN]]"
find . -path '*/build/test-results/*' -name 'TEST-*.xml' | sort | while read -r report; do
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[GRADLE_ENV]]", _GRADLE_ENV)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Elasticsearch164700To164700ImageBase(Image):
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


class Elasticsearch164700To164700ImageDefault(Image):
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
        return Elasticsearch164700To164700ImageBase(self.pr, self._config)

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

case "$(uname -m)" in
  aarch64|arm64) JDK_ARCH=aarch64 ;;
  *) JDK_ARCH=x64 ;;
esac
JDK_VERSIONS=$(find . -maxdepth 6 -type d -path '*/src/main[0-9]*' | sed -E 's#.*/src/main([0-9]+)$#\\1#' | grep -E '^[0-9]+$' | sort -un | awk '$1 > 21')
echo "MSB_JDK_VERSIONS=$JDK_VERSIONS"
for v in $JDK_VERSIONS; do
  mkdir -p /opt/msb-jdk-$v
  for i in $(seq 1 60); do
    if curl -4 --http1.1 -fL -C - --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o /tmp/msb-jdk-$v.tar.gz "https://api.adoptium.net/v3/binary/latest/$v/ga/linux/$JDK_ARCH/jdk/hotspot/normal/eclipse"; then
      break
    fi
    sleep 5
  done
  tar -xzf /tmp/msb-jdk-$v.tar.gz -C /opt/msb-jdk-$v --strip-components=1
  rm -f /tmp/msb-jdk-$v.tar.gz
  /opt/msb-jdk-$v/bin/java -version
done

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
  for i in $(seq 1 60); do
    if curl -4 --http1.1 -fL -C - --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$DIST_DIR/$DIST_ZIP" "$DIST_URL"; then
      break
    fi
    sleep 5
  done
  (cd "$DIST_DIR" && unzip -q "$DIST_ZIP" && touch "$DIST_ZIP.ok")
fi
ok=0
chown -R gradle:gradle "$PWD" "$GRADLE_USER_HOME"
for i in 1 2 3 4 5; do
  if runuser -u gradle -- ./gradlew --continue :server:test --tests MsbWarmupNoSuchTest :server:internalClusterTest --tests MsbWarmupNoSuchTest; then
    ok=1
    break
  fi
  sleep 20
done
[ "$ok" = 1 ] || { echo "prepare: gradle warm-up failed" >&2; exit 1; }
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
class Elasticsearch164700To164700(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Elasticsearch164700To164700ImageDefault(self.pr, self._config)

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
