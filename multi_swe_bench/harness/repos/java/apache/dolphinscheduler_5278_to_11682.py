import re
from typing import Optional

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.apache.dolphinscheduler import (
    DolphinSchedulerImageBase,
    DolphinSchedulerImageDefault,
    _APPLY_FIX_PATCH,
    _APPLY_TEST_PATCH,
    _DERIVE_TARGETS,
    _ENABLE_SUREFIRE,
    _MVN_FLAGS,
    _REPO_DIR,
    _SERVICES_START,
    _script,
)


_LATE_ERA_PR_MIN = 5278
_LATE_ERA_PR_MAX = 11682
_LATE_ERA_KEY = "dolphinscheduler_5278_to_11682"

# Late-era plugin skips. jacoco 0.8.8:instrument double-instruments spi classes
# across the reactor (PR 11682 evidence). spotless/enforcer/dependency-check are
# pre-emptive: later-era pom.xml progressively adds these enforcement plugins
# that fail before tests get to run.
_LATE_ERA_MVN_FLAGS = (
    _MVN_FLAGS
    + " -Djacoco.skip=true"
    + " -Dspotless.check.skip=true"
    + " -Denforcer.skip=true"
    + " -Ddependency-check.skip=true"
    + " -Dsurefire.rerunFailingTestsCount=2"
    + " -Dmaven.wagon.http.retryHandler.count=3"
)

if not getattr(_pull_request.PullRequest, "_dolphin_late_era_patched", False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _dolphin_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == "apache"
            and pr.repo == "dolphinscheduler"
            and _LATE_ERA_PR_MIN <= pr.number <= _LATE_ERA_PR_MAX
            and not pr.number_interval
        ):
            pr.number_interval = _LATE_ERA_KEY
        return pr

    _pull_request.PullRequest.from_json = classmethod(_dolphin_from_json)
    _pull_request.PullRequest._dolphin_late_era_patched = True


def parse_surefire_xml(test_log: str) -> TestResult:
    """Parse the surefire JUnit XML echoed by the stage scripts.

    Ids are "<fqcn>#<method>". The '#' separator is deliberate: report.py's
    _file_hosts_test() splits on it to recover the class name and compare it
    against the test file's basename, which is what keeps the cheating guard
    accurate. A dotted "<fqcn>.<method>" id would defeat that check.
    """
    clean = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')

    for m in testcase_re.finditer(clean):
        attrs = m.group(1)
        nm = name_re.search(attrs)
        cn = classname_re.search(attrs)
        if not nm or not cn:
            continue
        test_id = "%s#%s" % (cn.group(1), nm.group(1))
        closing = m.group(2)
        inner = m.group(3) or ""
        if closing == "/>":
            passed.add(test_id)
        elif "<failure" in inner or "<error" in inner:
            failed.add(test_id)
        elif "<skipped" in inner:
            skipped.add(test_id)
        else:
            passed.add(test_id)

    # A rerun can emit the same id twice. Failure wins over pass, and pass wins
    # over skip, so one id never lands in two buckets.
    failed -= passed
    skipped -= passed
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class DolphinSchedulerLateEraImageBase(DolphinSchedulerImageBase):
    def image_tag(self) -> str:
        return "base-5278_to_11682"

    def workdir(self) -> str:
        return "base-5278_to_11682"

    def dockerfile(self) -> str:
        full = super().dockerfile()
        marker = "WORKDIR /home/%s" % self.pr.repo
        head, sep, _ = full.partition(marker)
        if not sep:
            return full
        tail = ['CMD ["/bin/bash"]']
        if self.clear_env:
            tail.insert(0, self.clear_env)
        return head.rstrip("\n") + "\n\n" + "\n\n".join(tail) + "\n"


class DolphinSchedulerLateEraImageDefault(DolphinSchedulerImageDefault):
    def dependency(self) -> Image:
        return DolphinSchedulerLateEraImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).rstrip("\n")
        submodule_scrub = (
            "RUN if [ -f .gitmodules ]; then \\\n"
            "    git submodule foreach --recursive '\\\n"
            "        git checkout --detach HEAD; \\\n"
            "        git remote remove origin 2>/dev/null || true; \\\n"
            "        git for-each-ref --format=\"%(refname)\" refs/heads refs/remotes refs/tags refs/replace \\\n"
            "            | xargs -r -n1 git update-ref -d; \\\n"
            "        git reflog expire --expire=now --all; \\\n"
            "        git reflog expire --expire-unreachable=now --all; \\\n"
            "        git gc --prune=now --aggressive; \\\n"
            "        rm -f .git/objects/info/alternates; \\\n"
            "    '; \\\n"
            "fi"
        )
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            "FROM %s:%s\n\n"
            "WORKDIR /home/%s\n\n"
            "RUN git reset --hard\n\n"
            "RUN git checkout %s\n\n"
            "%s\n\n"
            "%s\n\n"
            "WORKDIR /home/\n\n"
            "%s\n\n"
            "RUN bash /home/prepare.sh\n"
        ) % (name, tag, self.pr.repo, sha, hardening, submodule_scrub, copy_commands)

    def files(self) -> list[File]:
        files = super().files()
        new_prepare = self._build_prepare()
        stage_scripts = ("run.sh", "test-run.sh", "fix-run.sh")
        result: list[File] = []
        for f in files:
            if f.name == "prepare.sh":
                result.append(File(".", "prepare.sh", new_prepare))
            elif f.name in stage_scripts:
                result.append(
                    File(".", f.name, f.content.replace(_MVN_FLAGS, _LATE_ERA_MVN_FLAGS))
                )
            else:
                result.append(f)
        return result

    def _build_prepare(self) -> str:
        template = """#!/bin/bash
set -e

STATUS_LOG=/home/dolphinscheduler/prepare-status.log
: > "$STATUS_LOG"

@@SERVICES_START@@

echo "CREATE ROLE test WITH LOGIN SUPERUSER PASSWORD 'test';" > /tmp/ds_role.sql
su postgres -c "psql -v ON_ERROR_STOP=1 -f /tmp/ds_role.sql" || true
su postgres -c "createdb -O test dolphinscheduler" || true
rm -f /tmp/ds_role.sql

cd @@REPO_DIR@@

if [ -f sql/dolphinscheduler-postgre.sql ]; then
    PGPASSWORD=test psql -h 127.0.0.1 -U test -d dolphinscheduler \\
        -f sql/dolphinscheduler-postgre.sql >/dev/null 2>&1 || true
fi

# Cat 3 diagnostic: log patch outcomes without erasing evidence. .rej files are
# listed then removed so the working tree stays clean for the stage scripts.
{
    echo "=== TEST_PATCH ==="
    if git apply --whitespace=nowarn /home/test.patch 2>&1; then
        echo "TEST_PATCH_STATUS=APPLIED_CLEAN"
    elif git apply --whitespace=nowarn --reject /home/test.patch 2>&1; then
        echo "TEST_PATCH_STATUS=APPLIED_WITH_REJECTS"
        find . -name '*.rej' -type f 2>/dev/null | head -50
    else
        echo "TEST_PATCH_STATUS=FAILED"
    fi
    find . -name '*.rej' -delete 2>/dev/null || true

    echo "=== FIX_PATCH ==="
    if git apply --whitespace=nowarn /home/fix.patch 2>&1; then
        echo "FIX_PATCH_STATUS=APPLIED_CLEAN"
    elif git apply --whitespace=nowarn --reject /home/fix.patch 2>&1; then
        echo "FIX_PATCH_STATUS=APPLIED_WITH_REJECTS"
        find . -name '*.rej' -type f 2>/dev/null | head -50
    else
        echo "FIX_PATCH_STATUS=FAILED"
    fi
    find . -name '*.rej' -delete 2>/dev/null || true
} >> "$STATUS_LOG" 2>&1

@@ENABLE_SUREFIRE@@

@@DERIVE_TARGETS@@

echo "PREPARE_MVN_SEL=$SEL" >> "$STATUS_LOG"
echo "PREPARE_MVN_CLASSES=$CLASSES" >> "$STATUS_LOG"

if [ -n "$SEL" ] && [ -n "$CLASSES" ]; then
    set +e
    @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ test -pl "$SEL" -am -Dtest="$CLASSES"
    echo "PREPARE_MVN_EXIT=$?" >> "$STATUS_LOG"
    set -e
else
    echo "PREPARE_MVN_EXIT=SKIPPED_EMPTY_TARGETS" >> "$STATUS_LOG"
fi

git reset --hard @@SHA@@ >> "$STATUS_LOG" 2>&1
git clean -fdx >> "$STATUS_LOG" 2>&1
if git diff --quiet HEAD; then
    echo "TREE_CLEAN=YES" >> "$STATUS_LOG"
else
    echo "TREE_CLEAN=NO" >> "$STATUS_LOG"
fi

find . -type d -name target -prune -exec rm -rf {} + 2>/dev/null || true

rm -rf ~/.m2/repository/org/apache/dolphinscheduler/ 2>/dev/null || true

ZOO_LOG_DIR=/var/log/zookeeper /opt/zookeeper/bin/zkServer.sh stop || true
pg_ctlcluster "$PGVER" main stop || true
"""
        body = (
            template.replace("@@REPO_DIR@@", _REPO_DIR)
            .replace("@@SERVICES_START@@", _SERVICES_START)
            .replace("@@ENABLE_SUREFIRE@@", _ENABLE_SUREFIRE)
            .replace("@@DERIVE_TARGETS@@", _DERIVE_TARGETS)
        )
        return _script(body, self.pr).replace(_MVN_FLAGS, _LATE_ERA_MVN_FLAGS)


@Instance.register("apache", "dolphinscheduler_5278_to_11682")
class DOLPHINSCHEDULER_5278_TO_11682(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DolphinSchedulerLateEraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_xml(test_log)
