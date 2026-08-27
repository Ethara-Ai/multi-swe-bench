from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `--all-features`, and `--lib`. Both halves are load-bearing.
#
# --all-features
#   The two gold tests added by test.patch are `#[cfg(feature = "gzip")]` and
#   `#[cfg(feature = "brotli")]`, and those two features are ADDED BY the fix
#   patch (it appends `gzip = ["flate2"]` / `brotli = ["brotli-decompressor"]`
#   to [features] in Cargo.toml). Without the flag they compile out at every
#   stage and the PR demonstrates nothing.
#
#   Naming them explicitly (`--features gzip,brotli`) is NOT an option: at the
#   run and test stages the manifest does not declare them yet, so cargo aborts
#   with "Package `ureq` does not have these features" before a single test
#   binary is built. That would report zero tests for two of the three stages -
#   indistinguishable from a broken image - and would destroy the p2p pool.
#
#   The flag is byte-identical in all three stages, so it cannot manufacture a
#   transition on its own. At base it resolves to the features the base manifest
#   declares (tls/native-tls/native-certs/json/charset/cookies/socks-proxy); at
#   the fix stage the same flag additionally picks up gzip and brotli, which is
#   the fix's own doing. Expected shape: the gold tests are absent at run and at
#   test (cfg'd out, so NONE) and pass at fix - i.e. they land in **n2p**, not
#   f2p. That still qualifies the instance (`f2p or n2p`).
#
# --lib
#   The gold tests live in `src/test/body_read.rs`, which is reachable only
#   through `mod test` inside the crate - they call `test::set_handler(...)` and
#   request `test://host/...`, ureq's in-process fake transport. That is a lib
#   unit test by construction (an integration test under tests/ cannot see
#   `crate::test`), so --lib is guaranteed to contain them.
#
#   Scoping to the lib target also keeps every graded stage hermetic: the
#   `test://` scheme never opens a socket and never resolves a name, so the same
#   set of tests is observable with or without a network and behind the MITM
#   proxy. Widening to the whole suite (drop `--lib`) would pull in doc-tests
#   whose examples name real external URLs - a class of test that flips between
#   stages for reasons that have nothing to do with the patch, which is exactly
#   the noise that corrupts p2p. If a larger p2p pool is wanted later, dropping
#   `--lib` is a one-token change and parse_log already keys tests per target.
TEST_CMD = "cargo test --all-features --lib"

# The two dependencies the FIX patch introduces. Pre-fetched at image-build time
# (see prepare.sh) so the fix stage does not have to reach crates.io for them
# mid-run; the version reqs are copied verbatim from the fix patch's Cargo.toml
# hunk, so the pre-fetch resolves the same candidates the fix stage will.
FIX_PATCH_DEPS = [
    ("flate2", "1.0.22"),
    ("brotli-decompressor", "2.3.2"),
]



def _defining_source_file(module_path: str) -> str:
    """Map a cargo module path to the .rs file that DEFINES the test.

    Cargo prints `test <module path>::<fn> ... ok`, and for the lib target every
    one of those module paths is a file under src/ by Rust's own module rules:

        test::body_read::gzip_text   -> src/test/body_read.rs
        response::tests::header_100  -> src/response.rs   (inline `mod tests`)
        parse_url_shortcut           -> src/lib.rs        (inline at lib root)

    Getting this right is not cosmetic. `report.py`'s reward-hacking guard reads
    the file out of the test id (everything before `::`) and rejects the whole
    instance if the FIX patch touched that file -- "cannot credit the fix as
    unbiased". Keying every test to the target path `src/lib.rs` instead of its
    real file made that guard fire on this very PR: the fix patch adds two doc
    comment lines to src/lib.rs, so the gold tests in src/test/body_read.rs
    looked like tests the fix had edited. They are not; the fix patch never
    touches src/test/. Observed, not theorised -- it is what the first real run
    of this config reported.

    The trailing `tests` segment is dropped because `#[cfg(test)] mod tests` is
    the near-universal Rust convention for an inline test module: the module adds
    a path segment that the filesystem does not.
    """
    segments = [seg for seg in module_path.split("::") if seg]
    if segments and segments[-1] == "tests":
        segments.pop()
    if not segments:
        return "src/lib.rs"
    return "src/" + "/".join(segments) + ".rs"


class UreqImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # Pinned, and deliberately NOT a 2021-contemporary toolchain. The crate
        # is edition 2018 with `rustls = "0.20"`, `base64 = "0.13"` etc., but it
        # is a library with no committed lockfile and no `rust-version`, so cargo
        # resolves each `^` requirement to the newest semver-compatible release
        # at build time. The toolchain therefore has to satisfy TODAY'S point
        # releases, not the ones that existed at the base commit.
        #
        # 1.90 and not 1.82, and this is measured, not guessed: 1.82 fails to
        # even fetch, because `url ^2` now resolves through `idna` to
        # `idna_adapter 1.2.2`, whose manifest is edition 2024:
        #
        #     error: failed to download `idna_adapter v1.2.2`
        #     feature `edition2024` is required ... not stabilized in this
        #     version of Cargo (1.82.0)
        #
        # edition 2024 stabilised in 1.85, so anything below that cannot build
        # this crate's CURRENT dependency closure at all. 1.90 clears it with
        # margin and still compiles edition-2018 source unchanged.
        #
        # bookworm (not alpine/slim) because `--all-features` turns on
        # `native-tls`, which links against the system OpenSSL through
        # pkg-config - see the apt line below.
        return "rust:1.90.0-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = 'RUN git clone "${REPO_URL}" /home/' + self.pr.repo
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        org = self.pr.org
        repo = self.pr.repo
        # Verbatim, so the four integrity asserts and the submodule pass can
        # never quietly diverge from the harness's own definition.
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Rendered by the harness's own helper rather than written out here, so
        # the apt recipe has exactly one definition in the codebase. bookworm is
        # current, so this resolves to the plain (non-archived) branch.
        packages = ["git", "ca-certificates", "curl", "patch", "pkg-config", "libssl-dev"]
        apt_block = self._get_apt_update_command(" \\\n    ".join(packages), image_name)

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
# Supplied by the harness as a build arg, with NO default. A default would
# make the file build "successfully" when the arg is forgotten, pinning the
# tree from a literal baked into the Dockerfile instead of from the dataset --
# the reference base Dockerfile and DockerfileEnhancer both leave it unset so a
# missing arg fails loudly at `git checkout` instead.
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CARGO_TERM_COLOR=never \\
    NO_COLOR=1 \\
    CARGO_HOME=/usr/local/cargo \\
    CARGO_NET_RETRY=5 \\
    RUST_BACKTRACE=1 \\
    CI=true

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

# pkg-config + libssl-dev are NOT optional here: `--all-features` enables the
# `native-tls` feature, whose openssl-sys build script shells out to pkg-config
# and links libssl/libcrypto. Without them the build fails before any test runs.
# `patch` backs apply_patch.sh's last-resort fuzzy path.
{apt_block}

{fetch}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}

CMD ["/bin/bash"]
"""


class UreqImageDefault(Image):
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
        return UreqImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prefetch_deps = "\n".join(f'{name} = "{req}"' for name, req in FIX_PATCH_DEPS)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the Dockerfile's HEAD/refs asserts
# only prove WHICH commit is checked out -- a dirty tree satisfies all of them.
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "apply_patch.sh",
                r"""#!/bin/bash
# Apply one patch as completely as possible, then ALWAYS exit 0. The caller must
# reach cargo no matter how patching went: a stage that dies while patching
# reports zero tests, which the harness cannot tell apart from "the fix does not
# work". Whole-patch fast path first; per-file cascade only when something
# rejects, so one unappliable file cannot take the gold tests down with it.

patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            # From HEAD, not the index: `git apply --3way` stages what it merges,
            # so `git checkout -- <path>` would restore the half-applied version.
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    # Exiting 0 stays deliberate -- the caller must still reach cargo. But a
    # patch that did not fully apply must not be discoverable only by a human
    # reading the log. Drop a marker the run-scripts turn into a loud banner.
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

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

# Warm the registry + build cache for the BASE manifest into this image layer.
# `cargo fetch` alone is not enough: it resolves the manifest but does not build,
# so the graded stages would still pay the full compile of rustls/native-tls/
# serde/cookie_store. `--no-run` compiles every test binary and stops short of
# executing them.
cargo fetch || true
cargo test --all-features --lib --no-run || true

# `|| true` above is deliberate and required: both commands are pure cache
# warming, and a `--all-features` build pulls native-tls -> openssl-sys, which is
# exactly the class of native compile that fails on one architecture of a
# multi-arch build. A warm-up failure must not sink the image -- the graded
# stages simply compile from scratch. What the warm-up must NOT do is hide
# itself, so the outcome is stated in the log either way.
if [ -d target/debug ]; then
    echo "prepare: test binaries pre-built; stages start warm"
else
    echo "prepare: WARNING warm-up did not produce target/debug -"
    echo "prepare:         every stage will compile from scratch and may be slow."
fi

# Warm the two crates the FIX patch adds. Without this the fix stage is the only
# stage that has to talk to crates.io, and a transient network failure there
# reads as "the fix does not work" - the single most expensive false negative
# this instance can produce. A scratch crate is used rather than editing
# Cargo.toml in place so the repo tree is never touched; CARGO_HOME is shared,
# so the download lands in the same registry cache cargo consults later.
#
# Best-effort by design: if crates.io is unreachable at build time the image is
# still valid and the fix stage simply fetches live.
mkdir -p /tmp/fixdeps/src
: > /tmp/fixdeps/src/lib.rs
cat > /tmp/fixdeps/Cargo.toml <<'FIXDEPS_EOF'
[package]
name = "fixdeps"
version = "0.0.0"
edition = "2018"

[dependencies]
{prefetch_deps}
FIXDEPS_EOF
if (cd /tmp/fixdeps && cargo fetch); then
    echo "prepare: pre-fetched fix-patch dependencies"
else
    echo "prepare: WARNING could not pre-fetch fix-patch dependencies"
fi
rm -rf /tmp/fixdeps

cargo --version
rustc --version

# `cargo test` writes target/ and (for a library crate) Cargo.lock, both covered
# by .gitignore -- `git clean -fd` (deliberately WITHOUT -x) leaves them in
# place, so both survive into the graded stages while any stray tracked change is
# still undone.
#
# Keeping Cargo.lock is the point, not a side effect: it freezes dependency
# resolution, so the run/test/fix stages see a byte-identical dependency set.
# The fix patch adds two deps to Cargo.toml, and cargo answers that with a
# MINIMAL lock update - it appends flate2/brotli-decompressor and leaves every
# other pin alone. Without the lock, each stage would re-resolve independently
# and an upstream point release published mid-run could move a p2p test.
git reset --hard --quiet
git clean -fdq
if [ -f Cargo.lock ]; then
    echo "prepare: Cargo.lock retained; dependency resolution is frozen for all stages"
else
    echo "prepare: NOTE no Cargo.lock present after warm-up (it may be tracked and reset)"
fi
if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
    echo "prepare: working tree is pristine at base.sha"
else
    echo "prepare: WARNING tree still differs from base.sha after restore:"
    git status --porcelain | head -20
fi
""".format(pr=self.pr, prefetch_deps=prefetch_deps),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# `set -e` is safe here BECAUSE the test command is the last statement: cargo
# exits non-zero whenever a test fails -- the NORMAL outcome of the test stage --
# and by then the log, which is the deliverable, has already been written. Note
# there is no `|| true` on the test command: a cargo that cannot START (missing
# toolchain, unresolvable manifest) must surface as a stage that produced no
# tests, not as a silent success.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# See run.sh for why `set -e` is safe with the test command last.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# See run.sh for why `set -e` is safe with the test command last.
set -eo pipefail
export CI=true

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # `COPY <name> /home/<name>` -- the destination names the file, matching
        # the reference PR Dockerfile. `COPY x /home/` is equivalent to Docker but
        # not textually identical, and these artifacts are compared by eye.
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        # A PR layer is COPYs + one `RUN bash /home/prepare.sh`, nothing else --
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes/reachability after checkout.
        #
        # An extra assert block here would be pure duplication: prepare.sh runs
        # `check_git_changes.sh` under `set -e` both after `git reset --hard` and
        # after `git checkout <sha>`, so a drifted or dirty tree already fails the
        # build before this layer finishes.
        env_block = f"\n{self.global_env}\n" if self.global_env else ""
        clear_block = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {name}:{tag}
{env_block}
{copy_commands}RUN bash /home/prepare.sh
{clear_block}"""


@Instance.register("algesten", "ureq")
class Ureq(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UreqImageDefault(self.pr, self._config)

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
        # Defensive: CARGO_TERM_COLOR=never/NO_COLOR are set in the base image,
        # but a stage log can still pick up escapes from a dependency's build
        # script, and a stray escape inside a name would make the same test hash
        # differently across stages.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Key every test as `<source path>::<test path>` -- the `path::name` shape
        # the reference instance report uses (`fiasco/tests/test_x.py::test_y`).
        #
        # Key every test by its target as well as its name. `--lib` yields a
        # single target today, so the prefix is currently redundant - it is here
        # so that widening TEST_CMD later (dropping `--lib`) cannot silently
        # start merging same-named tests from different binaries, where one
        # target's pass would mask another's failure.
        #
        # The prefix is the SOURCE PATH, never the binary filename: cargo emits
        # `Running unittests src/lib.rs (target/debug/deps/ureq-67dd1f375a9c8793)`
        # and that hash changes between builds, so keying on it would make every
        # name differ across the run/test/fix stages and match nothing.
        target_re = re.compile(r"^\s*Running\s+(?:unittests\s+)?(\S+)\s+\(")
        doc_re = re.compile(r"^\s*Doc-tests\s+(\S+)")
        # `(.+?)` and not `(\S+)`: cargo appends " - should panic" to the name of
        # a #[should_panic] test, so a name can contain spaces. Requiring a
        # single token silently drops those lines, and a dropped line is
        # indistinguishable from a test that does not exist - i.e. it would be
        # scored as a NONE transition rather than the PASS/FAIL it really is.
        result_re = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\b")

        current_target = ""
        for raw in log.splitlines():
            line = raw.rstrip()

            m = target_re.match(line)
            if m:
                current_target = m.group(1)
                continue

            m = doc_re.match(line)
            if m:
                current_target = f"doc-tests {m.group(1)}"
                continue

            m = result_re.match(line)
            if not m:
                continue
            name, status = m.group(1), m.group(2)
            # For the lib target, cargo's module path names the defining file, so
            # resolve it. For any other target (an integration test under tests/,
            # a doc-test) the target IS the file and the name carries no module
            # nesting, so it is used as-is.
            if current_target == "src/lib.rs":
                head, _, leaf = name.rpartition("::")
                key = f"{_defining_source_file(head)}::{leaf or name}"
            elif current_target:
                key = f"{current_target}::{name}"
            else:
                key = name
            if status == "ok":
                passed_tests.add(key)
            elif status == "FAILED":
                failed_tests.add(key)
            else:
                skipped_tests.add(key)

        # A name may live in only one bucket. TestResult.__post_init__ rejects
        # overlapping sets outright, so a test that both passes and fails within
        # one stage log (a retry, a duplicated target) must be resolved here, and
        # it is resolved pessimistically: FAILED wins over ok.
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
