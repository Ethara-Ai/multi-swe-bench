from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Rust toolchain. NOT the era-matched 1.45 this PR was written against, and not
# :latest either -- both fail, for opposite reasons:
#
#   * rust:1.45 cannot build at all. Cargo 1.45 clones the crates.io index over
#     git via libgit2, and today's index is far larger than it was in 2020;
#     the fetch dies with `error reading from the zlib stream; class=Zlib (5)`
#     before a single crate is compiled (verified). The cure is the sparse
#     registry protocol, which only exists from Cargo 1.68 (default in 1.70+).
#
#   * Rust 1.80 changed integer-literal type inference, which broke the `time`
#     crate versions this era pins. Staying below 1.80 avoids it.
#
# 1.75 is the compromise: new enough for a sparse index, old enough that the
# 2020 dependency graph still compiles. Cargo.lock IS committed at this commit,
# so the crate versions themselves remain exactly the 2020 ones regardless.
RUST_IMAGE = "rust:1.75-bookworm"

# `--locked` makes cargo refuse to silently rewrite Cargo.lock, so all three
# graded stages compile the exact same dependency graph. Without it a stage
# could pick up a newer patch release mid-run and move results for reasons
# unrelated to the fix.
TEST_CMD = "cargo test --locked"


class DiskonautImageBase(Image):
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
        return RUST_IMAGE

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
        sha = self.pr.base.sha

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT={sha}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

# CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse is the whole reason this image can
# resolve dependencies at all -- see RUST_IMAGE above. It is already the default
# on 1.75, but it is set explicitly so the build does not quietly regress to the
# git index if the base tag is ever moved.
#
# CARGO_TERM_COLOR=never keeps ANSI escapes out of the log the report parses.
# CI=true puts insta in CI mode: on a snapshot mismatch it fails the assertion
# WITHOUT writing a .snap.new beside the original, so a failing stage cannot
# leave untracked files behind and dirty the tree for the stage after it.
ENV CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \\
    CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    RUST_BACKTRACE=1 \\
    CI=true
WORKDIR /home/

# No apt-get on purpose. diskonaut is pure Rust -- no C dependencies -- and the
# rust:*-bookworm images are built on buildpack-deps, which already ships git,
# curl, ca-certificates, patch and coreutils. Asserting beats installing: a base
# image that ever loses one of these fails HERE, loudly, instead of halfway
# through a graded stage. csplit is used by apply_patch.sh, not by the build.
RUN set -eux; \\
    for t in git curl patch csplit cargo rustc; do \\
        command -v "$t" >/dev/null 2>&1 || {{ echo "missing required tool: $t"; exit 1; }}; \\
    done; \\
    rustc --version; \\
    cargo --version

{fetch}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

{self.clear_env}

CMD ["/bin/bash"]
"""


class DiskonautImageDefault(Image):
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
        return DiskonautImageBase(self.pr, self._config)

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
# reach cargo test no matter how patching went: a stage that dies while patching
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
    # Exiting 0 stays deliberate -- the caller must still reach cargo test. But a
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

# Warm the dependency graph at BUILD time so the three graded stages only ever
# recompile diskonaut itself. `cargo fetch --locked` populates $CARGO_HOME from
# the committed Cargo.lock; `cargo build --tests --locked` then compiles every
# dependency, dev-deps (insta) included, into target/.
#
# This is not merely a speed knob. Compiling the whole graph inside each stage
# would put a long network+compile step in front of the tests, and a transient
# crates.io failure there would surface as "0 tests ran" -- which the harness
# scores identically to a fix that does not work.
cargo fetch --locked
cargo build --tests --locked

cargo --version
rustc --version

# target/ is gitignored at this commit, so the build above does not dirty the
# tree -- but ASSERT that rather than assume it. A dirty tree here would
# silently move every graded stage off base.sha. `git clean -fdq` deliberately
# omits -x, so the warmed target/ and the fetched registry survive.
git reset --hard --quiet
git clean -fdq
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# No `set -e`: a non-zero cargo exit is the NORMAL outcome of a stage whose
# tests fail, and the log is the deliverable.
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
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
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
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
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # A PR layer is COPYs + one `RUN bash /home/prepare.sh`, nothing else --
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes/reachability after checkout.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("imsnif", "diskonaut")
class Diskonaut(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DiskonautImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # libtest prints one line per test: `test <name> ... ok`. The name is a
        # full module path (tests::cases::ui::two_large_files_one_small_file),
        # unique within the crate, so it is used verbatim.
        #
        # Deliberately NOT namespaced by test binary, the way a multi-crate
        # workspace would need. diskonaut builds a single test binary, and its
        # `Running` header carries a content hash that changes whenever the crate
        # is recompiled -- which happens between every graded stage, since each
        # applies different patches. Prefixing names with it would make one test
        # look like a different test in each stage, and nothing would ever
        # classify as fail-to-pass.
        #
        # `(.+?)` rather than `(\S+)`: libtest appends a suffix to the names of
        # `#[should_panic]` tests (`foo - should panic`), and `\S+` would
        # silently truncate those into a name no other stage reports.
        result_re = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\b")

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            name, status = m.group(1).strip(), m.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A name may live in only one bucket; failure wins.
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
