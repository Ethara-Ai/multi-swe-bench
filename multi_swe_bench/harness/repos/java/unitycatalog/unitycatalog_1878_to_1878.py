import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "unitycatalog"
_INTERVAL = "unitycatalog_1878_to_1878"
_BASE_IMAGE = "maven:3.9.11-eclipse-temurin-21"
_REPO_DIR = "unitycatalog"

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

_SBT_ENV = r"""export SBT_HOME_DIR=/opt/sbt-home
export COURSIER_CACHE=$SBT_HOME_DIR/coursier
mkdir -p "$SBT_HOME_DIR" "$COURSIER_CACHE"
SBT_VERSION=$(sed -n 's/^sbt\.version=//p' project/build.properties | tr -d '[:space:]')
SBT_LAUNCHER="$SBT_HOME_DIR/sbt-launch-$SBT_VERSION.jar"
SBT_SET_HEAP='set server / Test / javaOptions ++= Seq("-Xmx2g")'
SBT_CMD=(java -Xms512m -Xmx2g -Xss4m -XX:+UseParallelGC -Dfile.encoding=UTF-8
  -Dsbt.global.base="$SBT_HOME_DIR/global" -Dsbt.boot.directory="$SBT_HOME_DIR/boot"
  -Dsbt.ivy.home="$SBT_HOME_DIR/ivy2" -Dsbt.coursier.home="$COURSIER_CACHE"
  -Dsbt.override.build.repos=true -Dsbt.repository.config="$PWD/build/sbt-config/repositories"
  -Dsbt.server.forcestart=false -Dsbt.supershell=false -Dsbt.color=false -Dsbt.ci=true -Dsbt.log.noformat=true
  -jar "$SBT_LAUNCHER")
"""

_RUN_TESTS = r"""cd /home/[[REPO_DIR]]
[[SBT_ENV]]
CLASSES=""
for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
  case "$f" in
    server/src/test/java/*Test.java) ;;
    *) continue ;;
  esac
  cls="${f#server/src/test/java/}"
  cls="${cls%.java}"
  CLASSES="$CLASSES ${cls//\//.}"
done
echo "MSB_TEST_CLASSES=$CLASSES"
rm -rf server/target/test-reports
rc=0
if [ -n "$CLASSES" ]; then
  timeout -k 60 3600 "${SBT_CMD[@]}" "$SBT_SET_HEAP" "server/testOnly$CLASSES" < /dev/null || rc=$?
fi
echo "[[BEGIN]]"
find server/target/test-reports -name 'TEST-*.xml' 2>/dev/null | sort | while read -r report; do
  sed -e '/<system-out>/,/<\/system-out>/d' -e '/<system-err>/,/<\/system-err>/d' "$report"
done
echo "[[END]]"
exit "$rc"
"""


def _render(template: str) -> str:
    return (
        template.replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[SBT_ENV]]", _SBT_ENV)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Unitycatalog1878To1878ImageBase(Image):
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


class Unitycatalog1878To1878ImageDefault(Image):
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
        return Unitycatalog1878To1878ImageBase(self.pr, self._config)

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

[[SBT_ENV]]
if [ ! -s "$SBT_LAUNCHER" ]; then
  curl -4 --http1.1 -fL --retry 10 --retry-all-errors --connect-timeout 30 --speed-limit 10000 --speed-time 60 -o "$SBT_LAUNCHER.tmp" "https://repo1.maven.org/maven2/org/scala-sbt/sbt-launch/$SBT_VERSION/sbt-launch-$SBT_VERSION.jar"
  mv "$SBT_LAUNCHER.tmp" "$SBT_LAUNCHER"
fi
ok=0
for i in 1 2 3; do
  if timeout -k 60 3600 "${SBT_CMD[@]}" "$SBT_SET_HEAP" server/Test/compile "server/testOnly io.unitycatalog.MsbWarmupNoSuchTest" < /dev/null; then
    ok=1
    break
  fi
  sleep 20
done
[ "$ok" = 1 ] || { echo "prepare: sbt warm-up failed" >&2; exit 1; }
git status --porcelain
git checkout -- .
git clean -fdq -e target/ -e '**/target/'
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
class Unitycatalog1878To1878(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Unitycatalog1878To1878ImageDefault(self.pr, self._config)

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
