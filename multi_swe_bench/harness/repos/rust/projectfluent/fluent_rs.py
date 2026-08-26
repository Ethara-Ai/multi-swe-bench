import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FluentRsImageBase(Image):
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
        # fluent-bundle (edition 2021) has no rust-toolchain.toml pin and no
        # committed Cargo.lock, so cargo resolves against today's crates.io
        # index. Verified in Docker: rust:1.76-bookworm fails because a
        # transitive dep (rustc-hash v2.1.3) needs rustc >=1.77; 1.82-bookworm
        # builds and runs the full fluent-bundle suite clean (35/35 passed).
        return "rust:1.82-bookworm"

    def image_tag(self) -> str:
        # Per-PR: the hardening block detaches at one BASE_COMMIT and prunes
        # every other ref, so a shared tag would let whichever PR built first
        # pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Reimplements Image.dockerfile() rather than calling super(), for one
        # reason only: the base class hardcodes its own
        # "ENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8" here, and
        # DockerfileEnhancer._ENV_BLOCK (injected into every rendered
        # Dockerfile, this one included) already sets both - plus TZ and the
        # proxy/CA vars - earlier in the same file. The duplicate was a
        # harmless no-op (same value, set twice), but this repo's QC flagged
        # it in kubestone.py first, and fixing the shared base class would
        # touch every other default-template repo, not just this one. So:
        # everything below is byte-for-byte identical to Image.dockerfile()
        # except that one ENV pair is dropped and WORKDIR /home/ is kept on
        # its own line.
        base_img = self.dependency()
        if isinstance(base_img, Image):
            raise NotImplementedError(
                "Subclass must override dockerfile() or return a string from dependency()"
            )

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]

        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        clone_section = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")

        sections.append(apt_command)
        sections.append(clone_section)
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class FluentRsImageDefault(Image):
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
        return FluentRsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

""",
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

# Warm the cargo registry cache + target/ build cache so the three graded
# runs do not each pay a full fetch+compile. `|| true` because a warm-up
# hiccup must not fail the image build -- the graded runs decide pass/fail.
cargo test -p fluent-bundle --all-features || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
# `cargo test -p fluent-bundle` as ONE invocation would fail closed here: cargo
# compiles every test target before running any of them, so a compile error in
# tests/bundle.rs (this PR's bug is a lifetime error, not a runtime failure --
# see fix.patch) aborts the whole run and 0 tests execute, even the 4 targets
# below that never touch the broken code. Splitting into one invocation per
# target -- same fixed list, same order, at run/test-run/fix-run alike -- lets
# each target's own compile+run result stand on its own, so a break in one
# target doesn't erase real signal from the others. `|| true` per command
# because the whole point is to keep going past a target that fails to build;
# parse_log reads the concatenated output and only cares about `test ... ok/
# FAILED` lines, so a missing block just means 0 tests from that target.
cargo test -p fluent-bundle --all-features --lib || true
cargo test -p fluent-bundle --all-features --doc || true
cargo test -p fluent-bundle --all-features --test custom_types || true
cargo test -p fluent-bundle --all-features --test resolver_fixtures || true
cargo test -p fluent-bundle --all-features --test types_test || true
cargo test -p fluent-bundle --all-features --test bundle || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Same fixed target list/order as run.sh and fix-run.sh -- see run.sh for why
# this is split per-target instead of one `cargo test` call. Verified in
# Docker: with only test.patch applied, --test bundle fails to compile
# (E0597) and contributes 0, while the other 5 targets are unaffected and
# still report their real pass counts (35 total here).
cargo test -p fluent-bundle --all-features --lib || true
cargo test -p fluent-bundle --all-features --doc || true
cargo test -p fluent-bundle --all-features --test custom_types || true
cargo test -p fluent-bundle --all-features --test resolver_fixtures || true
cargo test -p fluent-bundle --all-features --test types_test || true
cargo test -p fluent-bundle --all-features --test bundle || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Same fixed target list/order as run.sh and test-run.sh -- see run.sh for
# why this is split per-target. With fix.patch applied the lifetime bug is
# resolved, so --test bundle compiles again and reports its 3 tests (1
# baseline + 2 new from test.patch) alongside the other 5 targets.
cargo test -p fluent-bundle --all-features --lib || true
cargo test -p fluent-bundle --all-features --doc || true
cargo test -p fluent-bundle --all-features --test custom_types || true
cargo test -p fluent-bundle --all-features --test resolver_fixtures || true
cargo test -p fluent-bundle --all-features --test types_test || true
cargo test -p fluent-bundle --all-features --test bundle || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("projectfluent", "fluent-rs")
class FluentRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FluentRsImageDefault(self.pr, self._config)

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
        # Matches cargo test's `test <name> ... <ok|FAILED|ignored>` lines,
        # captured verbatim from this repo at df81ab8bd772 (base) and after
        # applying test.patch+fix.patch. `(.+)` (not `\S+`) because doc-test
        # names contain spaces, e.g.:
        #   test fluent-bundle/src/bundle.rs - bundle::FluentBundle<R,M>::new (line 567) ... ok
        #
        # This PR's bug is a compile-time lifetime error (E0597), not a
        # runtime assertion: with only test.patch applied, `cargo test`
        # aborts before running anything (exit 101, zero "test ... ok/FAILED"
        # lines anywhere in the output) -- ./bundle.rs fails to even compile.
        # That naturally yields zero matches here, which the harness reports
        # as NONE for the new tests at the test-only stage; with fix.patch
        # also applied it compiles and both new tests pass. Do not "fix" this
        # by broadening the regex to catch compiler errors as failing tests --
        # a compile error genuinely has no test name to attach to.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass = re.compile(r"^test (.+) \.\.\. ok$")
        re_fail = re.compile(r"^test (.+) \.\.\. FAILED$")
        re_skip = re.compile(r"^test (.+) \.\.\. ignored$")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )