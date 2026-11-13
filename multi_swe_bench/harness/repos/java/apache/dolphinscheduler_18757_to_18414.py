import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "apache"
_INTERVAL = "dolphinscheduler_18757_to_18414"
_BASE_IMAGE = "maven:3.8.4-eclipse-temurin-8"
_REPO_DIR = "dolphinscheduler"

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

_MVN_ENV = r"""export MAVEN_OPTS="-Xmx2g -XX:MaxMetaspaceSize=768m"
MVN="mvn -B -ntp -Dmaven.repo.local=/opt/m2repo -Dmaven.wagon.http.retryHandler.count=5 -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Daether.connector.http.retryHandler.count=5"
MVN_FLAGS="-Dmaven.build.cache.enabled=false -Dcheckstyle.skip=true -Ddisable.checks=true -Dspring-javaformat.skip=true -Dspotless.skip=true -Denforcer.skip=true -Drat.skip=true -Dlicense.skip=true -Djacoco.skip=true -Dmaven.javadoc.skip=true -Dmaven.source.skip=true -Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false -DskipITs=true -DsurefireArgLine=-Xmx1g"
MODS=""
CLASSES=""
add_mod() {
  local d
  d="$(dirname "$1")"
  while [ "$d" != "." ] && [ ! -f "$d/pom.xml" ]; do
    d="$(dirname "$d")"
  done
  [ "$d" = "." ] && return 0
  case ",$MODS," in
    *",$d,"*) ;;
    *) MODS="${MODS:+$MODS,}$d" ;;
  esac
}
for f in $(grep -ohE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    *IT.java) continue ;;
    */src/test/java/*.java) ;;
    *) continue ;;
  esac
  add_mod "$f"
  c="$(basename "$f" .java)"
  CLASSES="${CLASSES:+$CLASSES,}$c"
done
for f in $(grep -ohE '^diff --git a/[^ ]+' /home/fix.patch | cut -c14- | sort -u); do
  case "$f" in
    */src/main/java/*|*/src/main/resources/*|*/pom.xml) ;;
    *) continue ;;
  esac
  [ -n "$CLASSES" ] || continue
  add_mod "$f"
done
echo "MSB_MODULES=$MODS"
echo "MSB_TEST_CLASSES=$CLASSES"
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
getent hosts "$(hostname)" > /dev/null 2>&1 || echo "127.0.0.1 $(hostname)" >> /etc/hosts 2>/dev/null || true
[[MVN_ENV]]
find . -path '*/target/surefire-reports' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rc=0
if [ -n "$MODS" ] && [ -n "$CLASSES" ]; then
  timeout -k 60 3600 $MVN -o test -pl "$MODS" $MVN_FLAGS -Dtest="$CLASSES" || rc=$?
fi
echo "[[BEGIN]]"
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' | sort | while read -r report; do
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[MVN_ENV]]", _MVN_ENV)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Dolphinscheduler18757To18414ImageBase(Image):
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


class Dolphinscheduler18757To18414ImageDefault(Image):
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
        return Dolphinscheduler18757To18414ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = _render(
            r"""#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

[[MVN_ENV]]
[ -n "$MODS" ] || { echo "prepare: no test modules derived" >&2; exit 1; }
retry() {
  local i
  for i in 1 2 3; do
    if "$@"; then
      return 0
    fi
    sleep 20
  done
  return 1
}
BOMS="$(for d in *-bom; do [ -f "$d/pom.xml" ] && printf ',%s' "$d"; done; true)"
retry $MVN install -pl "$MODS$BOMS" -am -DskipTests $MVN_FLAGS || { echo "prepare: maven install warm-up failed" >&2; exit 1; }
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
retry $MVN test -pl "$MODS" $MVN_FLAGS -Dtest="$CLASSES" || { echo "prepare: maven test warm-up failed" >&2; exit 1; }
find . -path '*/target/surefire-reports' -type d -prune -exec rm -rf {} +
$MVN -o test -pl "$MODS" $MVN_FLAGS -Dtest="$CLASSES" || { echo "prepare: offline maven check failed" >&2; exit 1; }
[ -n "$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' | head -n 1)" ] || { echo "prepare: offline run produced no test reports" >&2; exit 1; }
find . -path '*/target/surefire-reports' -type d -prune -exec rm -rf {} +
git reset --hard
git clean -fd
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
class Dolphinscheduler18757To18414(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Dolphinscheduler18757To18414ImageDefault(self.pr, self._config)

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
