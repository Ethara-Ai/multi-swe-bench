import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        # Single layer, deliberately. On this machine, `docker_util._get_container_builder()`
        # routes any build where a `platform` is set (even a single-arch one) through the
        # docker-container buildx driver, which cannot see images `--load`ed into the local
        # daemon. A two-layer split (base image + PR image doing `FROM <own base>`) would build
        # fine right up until the multi-arch export step, then fail with "pull access denied" -
        # the worst point to discover it. Returning a str also keeps DockerfileEnhancer engaged,
        # which performs the BASE_COMMIT checkout and history scrub.
        return "rust:latest"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # `cargo test` alone picks up the in-module unit tests, the integration tests under
        # tests/, and the doc-tests - no separate invocation needed for any of them.
        #
        # --no-fail-fast is load bearing. Without it cargo stops after the FIRST test binary
        # that fails, so at the test stage (where db_test::clear_recover_sealed fails in the
        # lib target) the tests/keyspace_clear.rs target would never run at all. The stage
        # would then report a truncated suite, which reads downstream as "those tests do not
        # exist" and fabricates the f2p/n2p signal. Measured: with it, 25 result lines and
        # 3 real failures; without it, the run stops early.
        #
        # `|| true` so a non-zero exit (expected at the test stage) does not kill the script
        # before the log is captured. A genuinely broken build cannot hide here, because the
        # image refuses to seal unless `cargo build --tests` succeeded at BASE_COMMIT.
        cmd = "cargo test --workspace --all-features --no-fail-fast || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
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
cd /home/{pr.repo}
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

# cargo draws progress bars and colourised diagnostics with non-ASCII characters. The harness
# decodes buildx output with the platform default codec (cp1252 on Windows), where those bytes
# are undefined and abort the build with "'charmap' codec can't decode byte ...".
ENV CARGO_TERM_COLOR=never
ENV CARGO_TERM_PROGRESS_WHEN=never
ENV TERM=dumb

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

# fjall at BASE_COMMIT requires `lsm-tree = "~3.0.2"`, and crates.io has since YANKED every
# version in that range (3.0.2, 3.0.3, 3.0.4), so a live resolve fails outright with
# "failed to select a version ... is yanked". fjall is a library and gitignores Cargo.lock,
# so there is no committed lockfile to fall back on.
#
# Yanking blocks RESOLUTION, not distribution - the .crate tarball is still served (verified
# HTTP 200). So the exact published source is vendored and substituted through
# [patch.crates-io], which bypasses the registry's yank check because the dependency no
# longer comes from the registry.
#
# 3.0.2 specifically, not 3.0.3/3.0.4: lsm-tree 3.0.2 was published 2026-02-15T16:50Z and
# this PR's base commit is 2026-02-15T16:52Z - two minutes later. It was the only version in
# range at authoring time, so it is exactly what cargo would have resolved. Not a guess.
#
# The patch lives in $CARGO_HOME, NOT in the repo. Editing the repo's Cargo.toml would modify
# the code under test - the fix patch touches src/journal/ and src/recovery.rs, and swapping
# the storage engine across a minor version could change behaviour in those very paths, making
# any f2p transition unattributable. This way the tracked tree stays byte-identical to
# BASE_COMMIT and `git apply` of the patches cannot conflict.
RUN mkdir -p /vendor \\
    && curl -fsSL -o /tmp/lsm-tree.crate https://static.crates.io/crates/lsm-tree/lsm-tree-3.0.2.crate \\
    && tar xzf /tmp/lsm-tree.crate -C /vendor \\
    && mv /vendor/lsm-tree-3.0.2 /vendor/lsm-tree \\
    && rm /tmp/lsm-tree.crate \\
    && printf '\\n[patch.crates-io]\\nlsm-tree = {{ path = "/vendor/lsm-tree" }}\\n' >> ${{CARGO_HOME}}/config.toml

{code}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset --hard and
# checkout BASE_COMMIT, then the history-scrub block whose assertions fail the build unless HEAD
# is exactly BASE_COMMIT. Repeating any of that here would be dead code. The WORKDIR is kept so
# the cargo steps below do not depend silently on the enhancer's line ordering.
WORKDIR /home/{self.pr.repo}

# Gate: the crate must actually compile at BASE_COMMIT before any patch is applied. Without this,
# an environment issue (missing system dep, MSRV mismatch) surfaces only much later as all three
# graded stages reporting zero tests, which reads downstream as "these tests do not exist" rather
# than as a broken image. Also warms the registry/target cache so the three graded stages do not
# each resolve dependencies over the network.
RUN cargo build --workspace --all-features --tests

WORKDIR /home/

{copy_commands}
{self.clear_env}

"""


@Instance.register("fjall-rs", "fjall")
class Fjall(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", test_log)

        # Test names are prefixed with the target that produced them. Integration tests carry
        # NO module path (`test clear_recover ... ok` from tests/keyspace_clear.rs), so two
        # different files can each define `basic` and collapse into one entry without the
        # prefix - one failing would then mask the other's pass. Cargo announces each target
        # first:
        #     Running unittests src/lib.rs (target/debug/deps/fjall-1a2b3c)
        #     Running tests/keyspace_clear.rs (target/debug/deps/keyspace_clear-4d5e6f)
        #   Doc-tests fjall
        re_running = re.compile(r"^Running (?:unittests )?(\S+)")
        re_doctests = re.compile(r"^Doc-tests\s+\S+")

        # One regex for all three statuses, and deliberately NOT anchored at the end: cargo
        # appends the ignore REASON after the status, e.g.
        #     test db_test::whitebox_db_drop ... ignored, restore
        #     test keyspace::test::keyspace_ingest ... ignored, flimsy because of the ...
        # An `ignored$` anchor silently drops every one of those, which understates the suite
        # and can invent a transition when a test moves between ignored and run.
        #
        # `.+?` rather than `\S+` because doc-test names contain spaces:
        #     test src/db.rs - Keyspace::clear (line 123) ... ok
        # A `\S+` pattern fails to match those at all, dropping every doc-test silently.
        re_test = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)")

        target = ""
        for raw in test_log.splitlines():
            line = raw.strip()

            m = re_running.match(line)
            if m:
                target = m.group(1)
                continue

            if re_doctests.match(line):
                target = "doc"
                continue

            # "test result: ok. 80 passed; ..." cannot match re_test - it has no " ... ".
            m = re_test.match(line)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            test_id = f"{target}::{name}" if target else name

            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # A retried test can be reported twice; enforce one bucket each, or the stage comparison
        # double-counts and invents transitions.
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
