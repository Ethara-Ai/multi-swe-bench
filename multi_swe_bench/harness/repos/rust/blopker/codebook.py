import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CodebookImageBase(Image):
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
        # Cargo.toml at base.sha declares edition = "2024" and there is no
        # rust-toolchain.toml, so the toolchain must be >= 1.85. Pinned to the
        # exact version that was observed resolving and compiling the whole
        # workspace in this image (rustc 1.98.0 / cargo 1.98.0); a floating
        # `latest` builds today but is not reproducible.
        return "rust:1.98"

    def image_tag(self) -> str:
        # DockerfileEnhancer bakes `git checkout ${BASE_COMMIT}` into this image
        # and prunes every other ref, so the artefact is commit-specific. A
        # constant "base" tag would dedupe several PRs onto one image_full_name()
        # and read BASE_COMMIT off whichever Image object happened to survive,
        # silently grading the others against the wrong tree.
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

        # git2 0.20 builds libgit2 from C sources and the tree-sitter grammars
        # each compile a generated C parser, so the C toolchain and pkg-config
        # are load-bearing here, not decoration.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    build-essential \
    cmake \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class CodebookImageDefault(Image):
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
        return CodebookImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _apply_patches(self, *patches: str) -> str:
        paths = " ".join(patches)
        # The cache-warming `cargo test --no-run` in prepare.sh can rewrite the
        # tracked Cargo.lock (rust:latest ships a newer cargo than the lockfile
        # was written by), and this PR's fix.patch carries a context hunk against
        # Cargo.lock. git apply is atomic, so one stale hunk rejects the whole
        # patch and the stage emits no test output at all. Restore the tree
        # first; the --3way fallback covers a recorded pre-image blob that is not
        # the one at base.sha.
        return f"""git checkout -- .
git clean -fxq -- '*Cargo.lock'
git apply --whitespace=nowarn {paths} || git apply --whitespace=nowarn --3way {paths}"""

    def _test_command(self) -> str:
        # One method feeding all three scripts, so the test command cannot drift
        # between stages (R3). The crate list is the workspace `members` array
        # from Cargo.toml at base.sha 6853b6ad, written out rather than
        # discovered with `cargo metadata`: a discovery command that fails yields
        # an empty loop -- zero tests, exit 0, no error -- identically in all
        # three stages, which reads as a parse_log bug instead of a broken
        # environment. The marker names the crate directory, because cargo's own
        # "Running <path>" line is relative to the PACKAGE root and parse_log
        # needs both halves to rebuild a repo-relative id.
        #
        # Every target is invoked SEPARATELY (--lib, --bins, then one --test per
        # tests/*.rs). A plain `cargo test` builds the whole package first, so a
        # single test target that does not compile takes the entire package's
        # results down with it -- and --no-fail-fast does not help, because it
        # governs test failures, not compilation failures. That is not
        # hypothetical here: at the test stage tests/test_odin.rs references
        # LanguageType::Odin, which only fix.patch introduces, so the package
        # failed to compile and all 118 of crates/codebook's tests vanished from
        # the baseline (179 -> 61) even though 27 other test targets were fine.
        # Per-target invocation costs only cargo start-up and confines the loss
        # to the one target that genuinely cannot build.
        return """rc=0
for crate in crates/codebook crates/codebook-config crates/codebook-lsp crates/downloader crates/dictionary-builder; do
    [ -f "$crate/Cargo.toml" ] || continue
    echo "=== MSB_CRATE: $crate ==="
    (cd "$crate" && cargo test --lib --no-fail-fast 2>&1) || rc=$?
    (cd "$crate" && cargo test --bins --no-fail-fast 2>&1) || rc=$?
    for target in "$crate"/tests/*.rs; do
        [ -f "$target" ] || continue
        (cd "$crate" && cargo test --test "$(basename "$target" .rs)" --no-fail-fast 2>&1) || rc=$?
    done
done
exit $rc"""

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
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the POST-FIX crate graph first: tree-sitter-odin-codebook enters the graph
# only through fix.patch, so without this the graded fix stage would have to reach
# crates.io. `git checkout -- .` then reverts the tracked files and `git clean
# -fdq` drops the files the patches added, while target/ survives because it is
# gitignored and -x is not passed.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
cargo fetch || true
cargo test --workspace --no-run || true
git checkout -- .
git clean -fdq

# Warm the BASELINE graph LAST, so the fingerprints cargo leaves in target/ match
# the unpatched tree that run.sh and test-run.sh actually see. Doing this in the
# other order leaves the cache keyed to the patched Cargo.lock, and both baseline
# stages then recompile the entire dependency tree from scratch.
cargo fetch || true
cargo test --workspace --no-run || true
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=self._test_command()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{apply}
{test_cmd}

""".format(
                    pr=self.pr,
                    apply=self._apply_patches("/home/test.patch"),
                    test_cmd=self._test_command(),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{apply}
{test_cmd}

""".format(
                    pr=self.pr,
                    apply=self._apply_patches("/home/test.patch", "/home/fix.patch"),
                    test_cmd=self._test_command(),
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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("blopker", "codebook")
class Codebook(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CodebookImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_crate_marker = re.compile(r"^=== MSB_CRATE: (\S+) ===$")
        re_target = re.compile(r"^Running (?:unittests )?(\S+) \(")
        re_doctests = re.compile(r"^Doc-tests\b")
        re_result = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)")
        re_doc_name = re.compile(r"^(\S+\.rs) - (.+)$")

        crate = ""
        target = ""

        for line in clean_log.splitlines():
            line = line.strip()

            marker = re_crate_marker.match(line)
            if marker:
                crate = marker.group(1)
                target = ""
                continue

            match = re_target.match(line)
            if match:
                target = match.group(1)
                continue

            if re_doctests.match(line):
                target = ""
                continue

            match = re_result.match(line)
            if not match:
                continue

            name = match.group(1)
            status = match.group(2)

            # Doctests carry their own package-relative source path inside the
            # name: "src/lib.rs - codebook::foo (line 12)".
            doc = re_doc_name.match(name)
            if doc:
                path = doc.group(1)
                name = doc.group(2)
            else:
                path = target

            # report.py splits an id on "::" and compares the head to a
            # repo-relative path taken from the patch (_test_name_matches_files),
            # so the crate directory and the package-relative target path are
            # joined here -> "crates/codebook/tests/test_odin.rs::test_odin_location".
            if crate and path:
                path = f"{crate}/{path}"

            full_name = f"{path}::{name}" if path else name

            if status == "ok":
                passed_tests.add(full_name)
            elif status == "FAILED":
                failed_tests.add(full_name)
            else:
                skipped_tests.add(full_name)

        # Deduplicate -- worst result wins. The sets must be disjoint or
        # TestResult.__post_init__ raises and takes the whole run down.
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
