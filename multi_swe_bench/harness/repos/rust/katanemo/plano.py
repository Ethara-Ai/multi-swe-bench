import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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


# apply_patch.sh -- plain `git apply` first, `--3way` only as a fallback, so
# the primary apply is what a failure must be counted from. Measured on this
# dataset: 0 binary hunks in either patch, so there is nothing to lift from
# git blobs before applying (unlike a repo that ships binary fixtures).
_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/plano
for patch in "$@"; do
  if ! git apply --whitespace=nowarn "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
"""


# cargo_test_report.py -- turn one `cargo test` run into per-test results.
#
# plano's `crates/` is a FIVE-member cargo workspace (llm_gateway,
# prompt_gateway, common, brightstaff, hermesllm). libtest's "Running" line
# only ever names the SOURCE path ("src/lib.rs", "tests/integration.rs"), and
# that path repeats across crates -- a naive per-path key would collide
# `common::src/lib.rs` tests with `hermesllm::src/lib.rs` tests. The compiled
# binary name in parentheses (e.g. `target/debug/deps/common-1a2b3c4d5e6f7890`)
# disambiguates the crate, so it is captured too; the trailing hash is
# stripped because it changes between compilations (each act rebuilds), and a
# name that is not byte-identical across acts breaks the F2P/P2P diff.
_CARGO_TEST_REPORT_PY = '''"""Turn one `cargo test` run into per-test results.

usage: cargo_test_report.py <cargo-output>

Emits one line per test, in the trailing-keyword form parse_log reads:

    cargo:common::src/lib.rs > tests::some_test PASSED
    cargo:integration::tests/integration.rs > llm_gateway_successful_request FAILED

Names are qualified with BOTH the crate/binary name (read from the compiled
artifact's filename, hash stripped) and the source path, so a name is
byte-identical across the three acts and unique across the workspace's five
crates.

Exit status mirrors a test runner: 0 = everything passed, 1 = at least one test
failed, 2 = the run produced no tests AT ALL -- i.e. the runner never started.
"""
import re
import sys

ANSI = re.compile(r"\\x1b\\[[0-9;]*[a-zA-Z]")
RUNNING = re.compile(
    r"^\\s*Running (?:unittests )?(\\S+) \\((?:.*/)?([A-Za-z0-9_]+)-[0-9a-f]+\\)\\s*$"
)
DOC_TESTS = re.compile(r"^\\s*Doc-tests (\\S+)\\s*$")
RESULT = re.compile(r"^test (.+?) \\.\\.\\. (ok|FAILED|ignored)\\b")
DOC_LINE = re.compile(r"\\s+\\(line \\d+\\)$")

STATUS = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}


def main():
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as fh:
        log = ANSI.sub("", fh.read())

    target, seen, lines, failed = "unknown", set(), [], False
    for line in log.splitlines():
        m = RUNNING.match(line)
        if m:
            target = "%s::%s" % (m.group(2), m.group(1))
            continue
        m = DOC_TESTS.match(line)
        if m:
            target = "%s::doc" % m.group(1)
            continue
        m = RESULT.match(line)
        if not m:
            continue
        name = DOC_LINE.sub("", m.group(1))
        status = STATUS[m.group(2)]
        key = "cargo:%s > %s" % (target, name)
        if key in seen:
            continue
        seen.add(key)
        lines.append("%s %s" % (key, status))
        if status == "FAILED":
            failed = True

    if not lines:
        sys.stderr.write(
            "cargo_test_report: no test executed; the crate did not compile "
            "or the runner never started\\n")
        return 2

    for line in lines:
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


# run_tests.sh -- mirrors .github/workflows/rust_tests.yml exactly: build the
# wasm module for llm_gateway + prompt_gateway (crates/llm_gateway/tests/
# integration.rs loads `../target/wasm32-wasip1/release/llm_gateway.wasm` at
# runtime and asserts the file exists), then `cargo test --lib` and
# `cargo test --test integration`, both run from ./crates.
#
# The three steps are invoked separately, each capturing its own exit code
# rather than aborting the script, so a failure in one (e.g. the wasm build)
# does not discard whatever the other steps still managed to report -- the
# real verdict comes from cargo_test_report.py, which returns 2 when NOTHING
# ran anywhere.
_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/plano/crates
export CARGO_TERM_COLOR=never
OUT=/tmp/cargo_test.out
: > "$OUT"
rc=0

echo "----- cargo build --release --target=wasm32-wasip1 -p llm_gateway -p prompt_gateway -----" >> "$OUT"
cargo build --release --target=wasm32-wasip1 --locked -p llm_gateway -p prompt_gateway >> "$OUT" 2>&1 || rc=$?

run_target () {
  echo "----- cargo test $* -----" >> "$OUT"
  cargo test --locked --no-fail-fast "$@" >> "$OUT" 2>&1 || rc=$?
}

run_target --lib
run_target --test integration

cat "$OUT"
echo "cargo test last-nonzero exit=${rc}"
echo "----- per-test results -----"
python3 /home/cargo_test_report.py "$OUT"
"""


class PlanoImageBase(Image):
    """Per-PR base image (`base-pr-<N>`).

    Single-PR dataset (rule 5): the full history scrub lives here, pinned to
    this PR's own BASE_COMMIT. dockerfile() is deliberately NOT overridden --
    the harness's own Image.dockerfile() already emits clone -> checkout
    ${BASE_COMMIT} -> extra_setup -> hardening scrub, and DockerfileEnhancer
    leaves a str-dependency image alone besides prepending the standard infra
    block. build_dataset.py passes BASE_COMMIT and REPO_URL as build args
    because dependency() returns a str.

    rust:1.82 matches .github/workflows/rust_tests.yml exactly
    (`rustup toolchain install 1.82`) -- the repo carries no rust-toolchain.toml
    pinning a channel, so the image's own toolchain IS the one under test.
    """

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
        return "rust:1.82-slim-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # crates/brightstaff pulls in reqwest with its default TLS backend
        # (native-tls), which resolves to openssl-sys + native-tls in
        # Cargo.lock -- both need a system OpenSSL plus pkg-config to build on
        # Linux. Nothing else in the workspace's Cargo.lock needs a system lib
        # (zstd-sys, ittapi-sys etc. bundle/vendor their own C sources).
        return ["pkg-config", "libssl-dev"]

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}`, so Cargo.toml/Cargo.lock
        # are this PR's own. The workspace root is crates/, not the repo root
        # (there is no top-level Cargo.toml), so every cargo invocation here
        # and in the per-PR image cds into crates/ first.
        #
        # Building the release wasm module and warming `cargo test`'s
        # dependency graph here is a warm-up only -- it downloads and compiles
        # the (unpatched) dependency crates so the per-PR acts, which rebuild
        # against patched source, do not pay for that from a cold cache. NOT
        # `|| true`: a warm-up that fails quietly ships an image with an empty
        # registry cache and leaves every act at the mercy of the network.
        return (
            "RUN rustup target add wasm32-wasip1 && \\\n"
            "    cd crates && \\\n"
            "    rustc --version && \\\n"
            "    cargo --version && \\\n"
            "    cargo fetch --locked && \\\n"
            "    cargo build --release --target=wasm32-wasip1 --locked -p llm_gateway -p prompt_gateway && \\\n"
            "    cargo build --tests --locked"
        )


class PlanoImageDefault(Image):
    """Per-PR image: FROM the base, COPY the patches and the act scripts, run
    prepare.sh -- and nothing else. The clone, the checkout and the history
    scrub already happened in the base."""

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
        return PlanoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(".", "cargo_test_report.py", _CARGO_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
cd crates
cargo fetch --locked || true
rustc --version
cargo --version
cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


@Instance.register("katanemo", "plano")
class Plano(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PlanoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first: a coloured status keyword never matches an anchored regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Then narrow to the section the report script printed, when present.
        # The RAW runner output sits in the same log, and libtest's own failure
        # line -- `test <name> ... FAILED` -- also ends in FAILED, so a whole-log
        # scan would invent a second, bogus "test" for every real failure.
        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        # Trailing-keyword form, exactly what cargo_test_report.py prints. The
        # name is captured non-greedily BEFORE the keyword, so no duration or
        # count can leak into it and manufacture a false transition between acts.
        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        # TestResult requires the three sets to be disjoint, else it raises.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
