
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "supertokens"
_INTERVAL = "supertokens_core_1487_to_1487"
_BASE_IMAGE = "gradle:8.13-jdk21"
_REPO_DIR = "supertokens-core"
_ROOT_DIR = "/home/supertokens-root"

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
org.gradle.jvmargs=-Xmx2g -XX:+UseParallelGC -Dfile.encoding=UTF-8
org.gradle.java.installations.auto-download=false
systemProp.org.gradle.internal.http.connectionTimeout=120000
systemProp.org.gradle.internal.http.socketTimeout=120000
systemProp.org.gradle.internal.repository.max.retries=10
systemProp.org.gradle.internal.repository.initial.backoff=2000
PROPEOF
cat > "$GRADLE_USER_HOME/init.d/msb-tests.gradle" <<'INITEOF'
allprojects {
    tasks.withType(Test).configureEach {
        ignoreFailures = true
        filter.failOnNoMatchingTests = false
    }
}
INITEOF
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[GRADLE_ENV]]
declare -A FILTERS
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    src/test/java/*.java) mod="" ; cls="${f#src/test/java/}" ;;
    */src/test/java/*.java) mod="${f%%/src/test/java/*}" ; cls="${f#*/src/test/java/}" ;;
    *) continue ;;
  esac
  cls="${cls%.java}"
  FILTERS["x$mod"]="${FILTERS["x$mod"]} --tests ${cls//\//.}"
done
TASKS=""
for key in "${!FILTERS[@]}"; do
  mod="${key#x}"
  if [ -z "$mod" ]; then
    TASKS="$TASKS :supertokens-core:test${FILTERS[$key]}"
  else
    TASKS="$TASKS :supertokens-core:${mod//\//:}:test${FILTERS[$key]}"
  fi
done
echo "MSB_TEST_TASKS=$TASKS"
find /home/[[REPO_DIR]] -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true
rc=0
if [ -n "$TASKS" ]; then
  cd [[ROOT_DIR]]
  timeout -k 60 3600 gradle --offline --continue $TASKS || rc=$?
fi
echo "[[BEGIN]]"
find /home/[[REPO_DIR]] -path '*/build/test-results/*' -name 'TEST-*.xml' | sort | while read -r report; do
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[ROOT_DIR]]", _ROOT_DIR)
        .replace("[[GRADLE_ENV]]", _GRADLE_ENV)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class SupertokensCore1487To1487ImageBase(Image):
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


class SupertokensCore1487To1487ImageDefault(Image):
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
        return SupertokensCore1487To1487ImageBase(self.pr, self._config)

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
PI_DIR=[[ROOT_DIR]]/supertokens-plugin-interface
mkdir -p [[ROOT_DIR]]
if [ ! -d "$PI_DIR/.git" ]; then
  ok=0
  for i in 1 2 3 4 5; do
    rm -rf "$PI_DIR"
    if git clone https://github.com/supertokens/supertokens-plugin-interface.git "$PI_DIR"; then
      ok=1
      break
    fi
    sleep 20
  done
  [ "$ok" = 1 ] || { echo "prepare: plugin-interface clone failed" >&2; exit 1; }
fi
PI_BRANCH=$(grep -oE '"[0-9]+[.][0-9]+"' /home/[[REPO_DIR]]/pluginInterfaceSupported.json | tail -1 | tr -d '"')
CORE_DATE=$(git -C /home/[[REPO_DIR]] log -1 --format=%cI HEAD)
for cand in "integration/$PI_BRANCH-next" "$PI_BRANCH" master; do
  if git -C "$PI_DIR" rev-parse -q --verify "origin/$cand" >/dev/null; then
    PI_BRANCH="$cand"
    break
  fi
done
ln -sfn /home/[[REPO_DIR]] [[ROOT_DIR]]/supertokens-core
cat > [[ROOT_DIR]]/settings.gradle <<'SETTINGSEOF'
rootProject.name = 'supertokens-root'
include 'supertokens-core:cli'
include 'supertokens-core:downloader'
include 'supertokens-core:ee'
include 'supertokens-core'
include 'supertokens-plugin-interface'
SETTINGSEOF
: > [[ROOT_DIR]]/build.gradle
cd [[ROOT_DIR]]
PI_REF=""
for cand in $(git -C "$PI_DIR" rev-list --date-order --before="$CORE_DATE" "origin/$PI_BRANCH" | head -25); do
  git -C "$PI_DIR" checkout -q "$cand"
  for attempt in 1 2 3; do
    if gradle :supertokens-core:classes > /tmp/msb-probe.log 2>&1; then
      PI_REF="$cand"
      break
    fi
    grep -qE 'Could not (resolve|GET|get resource|download)|timed out|Connection reset' /tmp/msb-probe.log || break
    sleep 30
  done
  [ -n "$PI_REF" ] && break
  grep -E 'error' /tmp/msb-probe.log | head -3 || true
done
[ -n "$PI_REF" ] || { tail -40 /tmp/msb-probe.log; echo "prepare: no compatible plugin-interface commit on $PI_BRANCH before $CORE_DATE" >&2; exit 1; }
echo "MSB_PLUGIN_INTERFACE=$PI_BRANCH@$PI_REF"
git -C "$PI_DIR" checkout -q "$PI_REF"
ok=0
for i in 1 2 3 4 5; do
  if gradle --continue :supertokens-core:test --tests MsbWarmupNoSuchTest; then
    ok=1
    break
  fi
  sleep 60
done
[ "$ok" = 1 ] || { echo "prepare: gradle warm-up failed" >&2; exit 1; }
cd /home/[[REPO_DIR]]
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
class SupertokensCore1487To1487(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SupertokensCore1487To1487ImageDefault(self.pr, self._config)

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
