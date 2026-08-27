import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Both patches land entirely in the `pnpm` package of this monorepo:
#   fix : pnpm/src/pnpm.ts, pnpm/src/readWantedPnpmMajor.ts
#   test: pnpm/src/readWantedPnpmMajor.test.ts, pnpm/test/switchingVersions.test.ts
# That package's own script is `_test: jest`, so jest is run from there. Running the
# whole workspace instead would execute every package's suite - thousands of unrelated
# tests, and the root `test-pkgs-main` script also runs lint and spellcheck, which can
# fail the run for reasons that have nothing to do with the patch.
PACKAGE = "pnpm"

# From the root package.json "packageManager" field. Pinning the exact version matters:
# this repo's preinstall runs `only-allow pnpm`, its lockfile is pnpm-format, and a
# different major resolves the workspace differently.
PNPM_VERSION = "10.33.0"


class ImageDefault(Image):
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
        # node:22 - the repo's CI matrix at this commit is [18, 20, 22, 24, 25] and
        # package.json pins pnpm 10.33.0, which needs Node >=18. 22 is the LTS in the
        # middle of that matrix, so it is well covered by the project's own testing.
        #
        # Single layer, deliberately: docker_util._get_container_builder() routes any
        # build with a platform set through the docker-container buildx driver, which
        # cannot see images loaded into the local daemon, so a `FROM <our-own-base>`
        # split is unbuildable here. Returning a str also keeps DockerfileEnhancer
        # engaged, which performs the BASE_COMMIT checkout and history scrub.
        return "node:22"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # jest --json --outputFile gives structured results whose testResults[].name is
        # the absolute file path and whose assertionResults[].fullName is the full
        # describe chain. That yields file-qualified ids, which is what report.py's
        # _test_name_matches_files needs (it splits an id on "::" and matches the head
        # against the patch's file list) to credit a newly added test.
        #
        # Writing to a file rather than stdout matters: these tests spawn the pnpm CLI
        # and log, which would corrupt structured output on stdout.
        #
        # --runInBand avoids jest's worker pool. This suite shells out to a real CLI and
        # touches a shared temp dir, so parallel workers are a known source of
        # container-only flakiness.
        #
        # `|| true` so a non-zero exit (expected in the test stage) does not kill the
        # script before the log is captured. A genuinely broken image cannot hide behind
        # it, because the build-time gate below refuses to seal unless jest can discover
        # the suite.
        # SCOPED TO src/, not the whole package. pnpm/test/ holds full end-to-end suites
        # that boot a verdaccio registry, download alternate Node runtimes and link and
        # run a real pnpm CLI. Measured: the run stage alone ran 30+ minutes without
        # finishing, sitting at ~0% CPU waiting on network, with `pnpm
        # --use-node-version=20.16.0` and `pnpm link .` in flight. Three stages of that is
        # hours, and the result would depend on network conditions rather than the patch.
        #
        # Nothing is lost from the signal. The fix CREATES pnpm/src/readWantedPnpmMajor.ts
        # (absent at BASE_COMMIT) exporting readWantedPnpmMajor, shouldSkipNpmPassthrough
        # and readManagePackageManagerVersionsSetting - exactly the three symbols the new
        # pnpm/src/readWantedPnpmMajor.test.ts imports. So that unit test is NONE at run,
        # NONE at test (the module it imports does not exist yet) and PASS at fix: a clean
        # N2P entirely inside src/. The other file the test patch touches,
        # pnpm/test/switchingVersions.test.ts, is a MODIFIED existing e2e file rather than
        # a new one, so it was never the transition carrier.
        #
        # --runInBand is kept: these suites still touch a shared temp dir, and serialising
        # a handful of fast unit files costs nothing.
        cmd = (
            f"cd /home/{self.pr.repo}/{PACKAGE}\n"
            "pnpm exec jest --ci --runInBand --testPathPattern 'src/.*\\.test\\.ts$' "
            "--json --outputFile=/tmp/jest.json > /dev/null 2>&1 || true\n"
            "echo '--- JEST JSON ---'\n"
            "cat /tmp/jest.json 2>/dev/null || echo 'no jest json produced'"
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{cmd}
""".format(cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image}

{self.global_env}

# npm/pnpm draw progress bars and colour output with non-ASCII characters. The harness
# decodes buildx output with the platform default codec (cp1252 on Windows), where those
# bytes are undefined and abort the build with "'charmap' codec can't decode byte ...".
ENV NPM_CONFIG_PROGRESS=false
ENV NPM_CONFIG_COLOR=false
ENV NO_COLOR=1
ENV FORCE_COLOR=0
# Many JS suites change behaviour without this - skipping tests, adding watch mode, or
# prompting. jest also uses it to disable interactive output.
ENV CI=true
# pnpm writes its store under the home directory by default; pinning it keeps the store
# on a predictable path so the install layer caches instead of re-resolving.
ENV PNPM_HOME=/pnpm
ENV PATH=/pnpm:${{PATH}}

RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

# pnpm is installed by exact version rather than via corepack. corepack reads the
# packageManager field and would work, but recent Node ships a corepack that prompts on
# first use, and a prompt in a non-interactive build hangs the layer.
RUN npm install -g pnpm@{PNPM_VERSION} --no-audit --no-fund

WORKDIR /home/

{code}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset --hard
# and checkout BASE_COMMIT, then the history-scrub block whose assertions fail the build
# unless HEAD is exactly BASE_COMMIT. Repeating any of that here would be dead code. The
# WORKDIR is kept so the install steps below do not depend on the enhancer's line order.
WORKDIR /home/{self.pr.repo}

# pnpm, never npm: the repo ships pnpm-lock.yaml, declares pnpm workspaces, and its
# preinstall hook runs `only-allow pnpm`. npm would resolve a different tree, so the
# three graded stages could each get different dependency versions and the diff would be
# measuring dependency drift rather than the patch.
#
# NO --ignore-scripts here, deliberately, and this was a real failure. The repo's own CI
# runs a plain `pnpm install`, and lifecycle scripts are what produce some workspace
# artifacts. With --ignore-scripts every one of the 54 suites died at load with
#     Cannot find module '@pnpm/worker' from '__utils__/jest-config/setupFilesAfterEnv.js'
# because jest's global setup requires a workspace package whose "main" is lib/index.js -
# compiled output that never got built. `only-allow pnpm` is satisfied (we are pnpm) and
# husky's prepare is harmless inside a git checkout.
#
# --frozen-lockfile makes a lockfile that no longer matches the manifests a hard error
# rather than a silent re-resolve.
RUN pnpm install --frozen-lockfile

# Compile the workspace. Every package here declares "main": "lib/index.js" and builds it
# with `tsc --build`, so without this step any cross-package require() resolves to a file
# that does not exist. This is the repo's own pretest step:
#     pretest      -> pnpm run compile-only && ...
#     compile-only -> ts-node __utils__/scripts/src/typecheck-only.ts && pnpm -F pnpm compile
# Running it at image-build time means the three graded stages each start from compiled
# output instead of repeating a multi-minute TypeScript build.
RUN pnpm run compile-only

# Refuse to seal an image whose graded stages could not report anything. A missing jest,
# an unresolvable ts-jest transform, or a moved package directory all yield an empty log,
# which reads downstream as "these tests do not exist" rather than as a broken image -
# and the harness scores that as a valid n2p-only resolve. --listTests proves the suite
# is discoverable from inside the graded package.
# The @pnpm/worker check is the specific thing that broke the first build: jest's
# setupFilesAfterEnv requires it for EVERY suite, so if its compiled output is missing,
# all 54 suites fail to load and every stage reports zero passes. Asserting it resolves
# here turns that into a build failure instead of a silently empty result.
RUN cd {PACKAGE} \\
    && pnpm exec jest --version > /dev/null \\
    && node -e "require.resolve('@pnpm/worker')" > /dev/null \\
    && pnpm exec jest --listTests --testPathPattern 'src/.*\\.test\\.ts$' > /tmp/lt.txt 2>&1 \\
    && test -s /tmp/lt.txt \\
    && wc -l < /tmp/lt.txt

WORKDIR /home/

{copy_commands}
{self.clear_env}

"""


@Instance.register("pnpm", "pnpm")
class Pnpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # The run script prints "--- JEST JSON ---" then the contents of jest's
        # --outputFile. Take everything after the LAST marker so a retry or stray earlier
        # output cannot be parsed instead of the real result.
        marker = "--- JEST JSON ---"
        blob = log.rsplit(marker, 1)[-1] if marker in log else log

        def empty() -> TestResult:
            return TestResult(
                passed_count=0, failed_count=0, skipped_count=0,
                passed_tests=set(), failed_tests=set(), skipped_tests=set(),
            )

        start = blob.find("{")
        if start == -1:
            return empty()

        try:
            data = json.loads(blob[start:])
        except Exception:
            # A truncated or interleaved blob must not be read as "no tests" if it can
            # still be recovered: retry from the outermost brace pair.
            end = blob.rfind("}")
            if end <= start:
                return empty()
            try:
                data = json.loads(blob[start:end + 1])
            except Exception:
                return empty()

        repo_prefix = f"/home/{self.pr.repo}/"

        for suite in data.get("testResults") or []:
            # jest reports an absolute path; make it repo-relative so the id's head
            # matches the paths listed in the patches.
            path = suite.get("name") or ""
            if repo_prefix in path:
                path = path.split(repo_prefix, 1)[1]
            path = path.replace("\\", "/")

            assertions = suite.get("assertionResults") or []

            if not assertions:
                # A suite that failed to load reports zero assertions plus a message.
                # Recording it as a failure keeps a broken import visible instead of
                # letting the whole file silently vanish from the stage.
                if suite.get("status") == "failed" or suite.get("message"):
                    failed_tests.add(f"{path}::<suite failed to load>")
                continue

            for a in assertions:
                name = a.get("fullName") or a.get("title") or ""
                if not name:
                    continue
                test_id = f"{path}::{name}" if path else name
                status = (a.get("status") or "").lower()

                if status == "passed":
                    passed_tests.add(test_id)
                elif status == "failed":
                    failed_tests.add(test_id)
                else:
                    # pending / skipped / todo / disabled all mean "did not run to a
                    # pass" without being a failure.
                    skipped_tests.add(test_id)

        # A retried test can be reported twice; enforce one bucket each, or the stage
        # comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
