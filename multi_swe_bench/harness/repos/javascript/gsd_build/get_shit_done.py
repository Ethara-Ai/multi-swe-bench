import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GsdBuildImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that
        # strips every other ref/commit so the fix can't be read out of git
        # history. DockerfileEnhancer then injects the build args
        # (REPO_URL/BASE_COMMIT), the base ENV block, the OCI labels, and the
        # final sanitize pass. None of that fires when dockerfile() is
        # overridden, which is why the previous two-stage build bypassed it.
        return "node:22"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. We stage the runtime helper scripts + patches into /home/ and
        # warm the npm install so the eval scripts run offline. The copied files
        # live outside /home/{repo}, so the hardening pass (which only operates
        # inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Warm the npm install at image-build time so the eval runs don't need
# network. The repo is already checked out at ${{BASE_COMMIT}} and hardened
# by Image.dockerfile(), so this script no longer performs any git checkout
# itself. `npm install` is allowed to fail (|| true) because its only purpose
# here is to populate node_modules; the real pass/fail signal comes from the
# run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
else
    node --test get-shit-done/bin/gsd-tools.test.js
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
else
    node --test get-shit-done/bin/gsd-tools.test.js
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
else
    node --test get-shit-done/bin/gsd-tools.test.js
fi

""".format(pr=self.pr),
            ),
        ]


@Instance.register("gsd-build", "get-shit-done")
class GsdBuildGetShitDone(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GsdBuildImageDefault(self.pr, self._config)

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
        """Parse TAP v13 from ``node --test``.  Subtests qualified as
        ``suiteName > testName`` to deduplicate across suites."""
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_suite: str = ""
        re_suite = re.compile(r"^# Subtest: (.+)$")
        re_subtest_pass = re.compile(r"^\s+ok \d+ - (.+?)(?:\s+#.*)?$")
        re_subtest_fail = re.compile(r"^\s+not ok \d+ - (.+?)(?:\s+#.*)?$")
        re_skip = re.compile(r"#\s*(?:SKIP|skip|TODO|todo)")

        for line in test_log.splitlines():
            suite_match = re_suite.match(line)
            if suite_match:
                current_suite = suite_match.group(1)
                continue

            m = re_subtest_pass.match(line)
            if m:
                name = m.group(1)
                qualified = f"{current_suite} > {name}" if current_suite else name
                if re_skip.search(line):
                    skipped_tests.add(qualified)
                else:
                    passed_tests.add(qualified)
                continue

            m = re_subtest_fail.match(line)
            if m:
                name = m.group(1)
                qualified = f"{current_suite} > {name}" if current_suite else name
                if re_skip.search(line):
                    skipped_tests.add(qualified)
                else:
                    failed_tests.add(qualified)
                continue

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


# Route bundled PRs that carry a dash-joined number_interval (the list of
# prs_in_bundle) to this config. Instance.create() looks up
# f"{org}/{number_interval}", so each bundle's interval string must be
# registered against this class.
_NUMBER_INTERVALS = [
    "1150-1152-1259-1261-1262-1264-1265-1266-1267-1268-1270-1271-1272-1274-1276-1277-1279-1282-1287-1288-1290-1291-1296-1297-1299-1302-1305-1306-1307-1311-1317-1318-1319-1320-1321-1322-1323",
    "1380-1386-1394-1397-1408-1417-1419-1425-1427-1429-1432-1434-1436-1437-1439-1442-1444-1445-1447-1454-1455-1456-1474-1477-1492-1500-1501-1502-1505-1508-1518-1519-1525-1529-1532-1540-1543-1544-1545",
]
for _interval in _NUMBER_INTERVALS:
    Instance.register("gsd-build", _interval)(GsdBuildGetShitDone)
