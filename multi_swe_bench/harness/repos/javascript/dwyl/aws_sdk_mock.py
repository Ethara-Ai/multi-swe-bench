import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AwsSdkMockImageBase(Image):
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
        # The repo's CI matrix at this era is node 12/14/16 (.github/workflows/
        # workflow.yml). 16 is the newest line it supports and matches the
        # tsd@0.19 / typescript@4.5 toolchain the gold fix patch introduces.
        # -bullseye is pinned over bare node:16 (Debian 10 buster, archived apt)
        # so apt stays serviceable and both amd64 and arm64 variants exist for
        # the multi-arch pass.
        return "node:16-bullseye"

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

        # The clone/COPY line stays LAST. DockerfileEnhancer expands it into
        # clone + WORKDIR + reset + checkout + history hardening + CMD, so
        # nothing may follow it. The syntax directive, TARGETARCH/REPO_URL/
        # BASE_COMMIT ARGs, proxy env, CA symlinks and OCI labels are injected
        # by the enhancer and are deliberately absent here.
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8

{code}

{self.clear_env}

"""


class AwsSdkMockImageDefault(Image):
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
        return AwsSdkMockImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "ensure-tsd-config.js",
                """\
// tsd locates its test files through the "tsd.directory" key in package.json;
// its CLI accepts only a project path, so there is no flag alternative.
// That key is introduced by the gold fix patch, which means at the run and
// test-patch stages tsd would abort with "test file does not exist" instead of
// type-checking at all - turning a real type failure into a config artifact.
//
// Adding the key here makes the identical assertions run in all three stages,
// so the fail -> pass transition reflects index.d.ts and nothing else. This
// runs AFTER git apply in each stage, so no patch ever sees a modified
// package.json. Idempotent: a no-op once the fix patch has supplied the key.
const fs = require('fs');

const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));

if (!pkg.tsd || !pkg.tsd.directory) {
  pkg.tsd = Object.assign({}, pkg.tsd, { directory: 'test' });
  fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2) + '\\n');
  console.log('ensure-tsd-config: added tsd.directory=test');
} else {
  console.log('ensure-tsd-config: tsd.directory already present');
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

rm -rf node_modules
npm install --no-audit --no-fund || true

# tsd and typescript are added to devDependencies BY the gold fix patch, so
# they do not exist at base.sha. They are installed here with --no-save (which
# leaves package.json untouched) so the type suite runs identically in all
# three stages instead of only after the fix lands.
npm install --no-save --no-audit --no-fund tsd@0.19.0 typescript@4.5.2 || true

# Hard verification. The `|| true` above exists because native rebuilds can
# fail non-fatally on arm64, but it would equally hide a wholesale install
# failure and leave a hollow image that reports 0 tests in every stage. Fail
# loudly here instead.
node -e "require('aws-sdk'); require('sinon'); require('traverse')"
npx tap --version
npx tsd --version
npx tsc --version

# npm rewrites package-lock.json during install. The gold fix patch also
# patches package-lock.json, so leaving it dirty makes `git apply` fail in
# fix-run.sh and the fix stage would produce no results at all. Restore it and
# re-assert a clean tree so every stage starts from pristine base.sha content.
git checkout -- package-lock.json
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """\
#!/bin/bash
# Single definition of the test command. run.sh, test-run.sh and fix-run.sh all
# delegate here, so the three stages provably execute the same thing and the
# f2p comparison cannot be invalidated by command drift.
set -eo pipefail
export CI=true

cd /home/{pr.repo}

node /home/ensure-tsd-config.js

# 1) Runtime suite (node-tap). --reporter=tap emits nested TAP with a stable
# per-test node id. The repo's own `npm test` wraps tap in nyc and, at base.sha,
# globs only ./test/*.js - which silently skips the .ts type test entirely.
tap_rc=0
npx tap ./test/*.js --reporter=tap --no-coverage 2>&1 || tap_rc=$?

# 2) Type-definition suite (tsd). tsd prints nothing on success and has no
# per-assertion identifiers, so one stable marker line is emitted for parse_log.
# This is NOT `|| true`: the exit code is captured and re-raised below, so a
# failure is recorded rather than swallowed.
#
# The existence guard is load-bearing: tsd exits 0 when it finds no test file,
# which would make "the type suite passed" and "the type suite never ran"
# produce an identical marker. Without this, a fix stage that somehow lost the
# test file would still be credited with a passing f2p. Absent file is reported
# as a failure so a vacuous pass can never be counted as a real one.
tsd_rc=0
if [ -f test/index.test-d.ts ]; then
  tsd_out="$(npx tsd 2>&1)" || tsd_rc=$?
else
  tsd_rc=1
  tsd_out="test/index.test-d.ts is absent - the tsd type suite did not run"
fi
if [ "$tsd_rc" -eq 0 ]; then
  echo "TSD_RESULT ok - test/index.test-d.ts > tsd type definitions"
else
  echo "TSD_RESULT not ok - test/index.test-d.ts > tsd type definitions"
  echo "$tsd_out"
fi

if [ "$tap_rc" -ne 0 ] || [ "$tsd_rc" -ne 0 ]; then
  exit 1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch
bash /home/run-tests.sh
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY check_git_changes.sh /home/check_git_changes.sh
COPY ensure-tsd-config.js /home/ensure-tsd-config.js
COPY prepare.sh /home/prepare.sh
COPY run-tests.sh /home/run-tests.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("dwyl", "aws-sdk-mock")
class AwsSdkMock(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AwsSdkMockImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI first. node-tap emits none under --reporter=tap today, but
        # a reporter or TTY change would otherwise break every pattern below.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Marker emitted by run-tests.sh; tsd itself has no per-test ids.
        tsd_pattern = re.compile(r"^TSD_RESULT (ok|not ok) - (.+)$")

        # node-tap TAP output is nested: a `# Subtest: <name>` header opens a
        # level and a matching `ok N - <name> # time=...` closes it at the SAME
        # indent, while bare assertions sit one level deeper WITHOUT a time
        # directive. Both signals are used so leaf assertions ("should be
        # equal", repeated dozens of times) are never mistaken for test nodes.
        subtest_pattern = re.compile(r"^(\s*)# Subtest: (.+?)\s*$")
        result_pattern = re.compile(r"^(\s*)(ok|not ok)\s+\d+\s+-\s+(.*)$")

        # indent level -> enclosing subtest name, used to qualify a leaf with
        # its parents. Truncating to the leaf alone would collide across groups.
        open_subtests: dict[int, str] = {}

        for raw_line in test_log.split("\n"):
            tsd_match = tsd_pattern.match(raw_line.strip())
            if tsd_match:
                status, name = tsd_match.groups()
                name = name.strip()
                if status == "ok":
                    passed_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            subtest_match = subtest_pattern.match(raw_line)
            if subtest_match:
                indent = len(subtest_match.group(1))
                for deeper in [k for k in open_subtests if k >= indent]:
                    del open_subtests[deeper]
                open_subtests[indent] = subtest_match.group(2).strip()
                continue

            result_match = result_pattern.match(raw_line)
            if not result_match:
                continue

            indent = len(result_match.group(1))
            status = result_match.group(2)
            remainder = result_match.group(3)

            # Indent 0 is the file-level aggregate ("ok 1 - ./test/index.test.js
            # # time=..."), not a test case.
            if indent == 0:
                continue

            is_skipped = bool(re.search(r"#\s*(SKIP|TODO)\b", remainder, re.IGNORECASE))

            # Drop every trailing TAP directive - `# time=1.2ms`, `# SKIP why` -
            # and the `{` that opens a nested block. Timing varies run to run,
            # so leaving it in would give the same test a different name in each
            # stage and break the cross-stage union entirely.
            name = re.split(r"\s+#\s", remainder)[0].strip().rstrip("{").strip()
            if not name:
                continue

            # A real test node either carries a time directive or was announced
            # by a `# Subtest:` header at this same indent. Bare assertions have
            # neither, and must not become test names.
            if "# time=" not in remainder and open_subtests.get(indent) != name:
                continue

            ancestors = [open_subtests[k] for k in sorted(open_subtests) if k < indent]
            full_name = "/".join([*ancestors, name])

            if is_skipped:
                skipped_tests.add(full_name)
            elif status == "ok":
                passed_tests.add(full_name)
            else:
                failed_tests.add(full_name)

        # TestResult.__post_init__ raises ValueError if these sets intersect,
        # which would abort the whole build. A name reported both ok and not ok
        # (retry, duplicate title) resolves to failed.
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
