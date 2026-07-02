from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Anti-cheat hardening — LIGHT variant
# ---------------------------------------------------------------------------
# Same ref/commit stripping as the shared Image._HARDENING_BLOCK (detach at
# BASE_COMMIT, delete every other ref, expire reflogs, prune unreachable
# objects), so the future backport-fix commit is removed and the same
# assertions hold.  The ONLY difference: `git gc --prune=now` WITHOUT
# `--aggressive`, and no aggressive `git repack -a -d -l`.  --aggressive only
# re-deltas/recompresses objects; it does NOT change which objects are removed.
# Dropping it avoids a pathological multi-hour slowdown on the amd64 multi-arch
# leg, where `git gc --aggressive` / `git pack-objects` on wasmtime's large
# history run under qemu emulation.
_HARDENING_BLOCK_LIGHT = """\
RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


# ---------------------------------------------------------------------------
# Rust toolchain era selection
# ---------------------------------------------------------------------------
# wasmtime spans release lines 0.26 -> 42.0 (PR numbers 3 -> 12658), each with a
# different MSRV.  We pick a base `rust:<version>` image per release line so the
# committed Cargo.lock compiles with a contemporary toolchain.  Because every PR
# is built as its own self-contained image (dependency() returns a *string*, so
# the shared Image.dockerfile() clones, checks out ${BASE_COMMIT}, and then runs
# the _HARDENING_BLOCK that strips all other refs/commits), there is no shared
# base tag to collide across the many distinct base shas / toolchains.
_RELEASE_LINE_TO_RUST_VERSION: dict[str, str] = {
    # Edition 2018 / no edition — no MSRV specified
    "unknown": "1.60.0",
    "0.26":    "1.60.0",
    "0.32":    "1.60.0",
    "0.35":    "1.60.0",
    # Edition 2021 — no MSRV specified
    "0.38":    "1.65.0",
    "0.39":    "1.65.0",
    "0.40":    "1.65.0",
    "1.0":     "1.65.0",
    "2.0":     "1.65.0",
    # Explicit rust-version in Cargo.toml
    "8.0":     "1.66.0",
    "9.0":     "1.66.0",
    "10.0":    "1.66.0",
    "11.0":    "1.66.0",
    "12.0":    "1.66.0",
    "14.0":    "1.70.0",
    "17.0":    "1.73.0",
    "18.0":    "1.73.0",
    "19.0":    "1.73.0",
    "21.0":    "1.75.0",
    "22.0":    "1.76.0",
    "23.0":    "1.77.0",
    "24.0":    "1.78.0",
    "25.0":    "1.78.0",
    "28.0":    "1.81.0",
    "29.0":    "1.81.0",
    "30.0":    "1.82.0",
    "32.0":    "1.84.0",
    "33.0":    "1.84.0",
    "34.0":    "1.85.0",
    "36.0":    "1.86.0",
    "37.0":    "1.87.0",
    "38.0":    "1.88.0",
    "39.0":    "1.89.0",
    "40.0":    "1.89.0",
    "41.0":    "1.90.0",
    "42.0":    "1.91.0",
}

_DEFAULT_RUST_VERSION = "1.80.0"


def _get_release_line(pr: PullRequest) -> str:
    ref = pr.base.ref if pr.base else ""
    if ref.startswith("release-"):
        # "release-42.0.0" -> "42.0", "release-0.32.x" -> "0.32"
        ver = ref.replace("release-", "")
        parts = ver.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return ver
    if ref.startswith("stable-v"):
        # "stable-v0.26" -> "0.26"
        return ref.replace("stable-v", "")
    return "unknown"


def _resolve_rust_version(pr: PullRequest) -> str:
    rl = _get_release_line(pr)
    return _RELEASE_LINE_TO_RUST_VERSION.get(rl, _DEFAULT_RUST_VERSION)


# ---------------------------------------------------------------------------
# Targeted test selection
# ---------------------------------------------------------------------------
# A full `cargo test --workspace` on wasmtime is enormous and pulls in tests that
# need network / GPU (wasi-http, wasi-nn) which would create spurious F2P noise.
# Instead we derive a minimal set of cargo invocations from the files touched by
# the *test* patch and run only those:
#   - tests/<t>/<a>/<b>.rs  -> `--test <t> <a>::<b>`  (root integration target <t>
#                              restricted to the module that mirrors the file path,
#                              so we run only that file's tests, not all ~1000+)
#   - tests/<t>/...{.wast|.wat}, tests/misc_testsuite/*, tests/<t>/main.rs
#                           -> `--test <t>` unfiltered (data files / harness root;
#                              misc_testsuite is data consumed by the `all` target)
#   - tests/<t>.rs          -> `--test <t>`
#   - crates|cranelift/<x>/ -> `--manifest-path <top>/<x>/Cargo.toml`
# Non-cargo artifacts (ci/*.js, ci/*.py, *.cc C-API tests) yield no selector.
# `--manifest-path` is used instead of `-p <pkg>` so we never have to guess the
# crate's package name (which is rarely the same as its directory).

def _modified_files(patch: str) -> list[str]:
    return re.findall(r"^\+\+\+ b/(.+)$", patch or "", re.MULTILINE)


def _clif_files(pr: PullRequest) -> list[str]:
    """Cranelift `.clif` filetest files touched by the test patch.

    These are NOT run by `cargo test --manifest-path cranelift/filetests` (that
    only runs the crate's unit tests); they are executed by the `clif-util test`
    binary (cranelift-tools).  Handled separately in _cargo_test_block.
    """
    out: list[str] = []
    for f in _modified_files(pr.test_patch):
        if f.startswith("cranelift/filetests/filetests/") and f.endswith(".clif"):
            if f not in out:
                out.append(f)
    return out


def _test_selectors(pr: PullRequest) -> list[str]:
    """Ordered, de-duplicated list of `cargo test` argument strings."""
    order: list[str] = []                 # preserve first-seen target order
    whole: set[str] = set()               # integration targets to run unfiltered
    filtered: dict[str, list[str]] = {}   # target -> ordered module-path filters
    manifests: list[str] = []
    seen_manifest: set[str] = set()

    def note(target: str) -> None:
        if target not in order:
            order.append(target)

    for f in _modified_files(pr.test_patch):
        parts = f.split("/")
        if len(parts) < 2:
            continue
        top = parts[0]
        if top == "tests":
            target = parts[1]
            rest = parts[2:]
            if target.endswith(".rs"):     # tests/<t>.rs  -> whole target <t>
                t = target[:-3]
                whole.add(t); note(t)
                continue
            if target == "misc_testsuite" or not rest:
                whole.add("all"); note("all")
                continue
            leaf = rest[-1]
            if not leaf.endswith(".rs") or leaf in ("main.rs", "mod.rs"):
                # data files (.wast/.wat/...) or the harness root -> run whole
                whole.add(target); note(target)
                continue
            mod_path = "::".join([*rest[:-1], leaf[:-3]])
            filtered.setdefault(target, [])
            if mod_path not in filtered[target]:
                filtered[target].append(mod_path)
            note(target)
        elif top in ("crates", "cranelift"):
            if f.endswith(".cc"):          # C-API C++ tests are not cargo tests
                continue
            sel = f"--manifest-path {top}/{parts[1]}/Cargo.toml"
            if sel not in seen_manifest:
                seen_manifest.add(sel); manifests.append(sel)
        # ci/* and anything else -> no cargo selector

    selectors: list[str] = []
    for t in order:
        if t in whole:
            selectors.append(f"--test {t}")
        else:
            # libtest takes multiple name filters only AFTER `--`; the bare form
            # `cargo test --test all globals table` errors ("unexpected argument").
            selectors.append(f"--test {t} -- {' '.join(filtered[t])}")
    selectors.extend(manifests)
    return selectors


# How long any single cargo invocation may run before we give up (seconds).
_CARGO_TIMEOUT = 2400
_FALLBACK_TIMEOUT = 3600


def _cargo_test_block(pr: PullRequest) -> str:
    """Shell snippet that runs every targeted cargo test command.

    Each command is timeout-wrapped and `|| true`-guarded so that (a) a hung
    test cannot block forever and (b) an expected failure (F2P tests fail in the
    test-only stage) does not abort the remaining selectors.  parse_log reads the
    per-test lines from the combined stdout regardless of exit codes.
    """
    selectors = _test_selectors(pr)
    clif_files = _clif_files(pr)
    lines: list[str] = []

    if not selectors and not clif_files:
        # No test file maps to a cargo target (e.g. ci/*.js or C-API *.cc only).
        # Such instances are structurally unscorable; run the core integration
        # suite as a bounded default rather than the whole (huge) workspace.
        return f"timeout {_FALLBACK_TIMEOUT} cargo test --test all 2>&1 || true\n"

    lines += [
        f"timeout {_CARGO_TIMEOUT} cargo test {sel} 2>&1 || true" for sel in selectors
    ]

    if clif_files:
        # Cranelift .clif filetests are executed by `clif-util test` (the
        # cranelift-tools binary).  `cargo test --manifest-path
        # cranelift/filetests` only runs that crate's unit tests, so a new .clif
        # that should fail->pass never flips there.  Build the tool once, then
        # run each .clif individually and emit a libtest-style line so parse_log
        # computes F2P per file.  The `-f` guard skips files that don't exist
        # yet (baseline run, before the test patch adds them).
        flist = " ".join(f'"{c}"' for c in clif_files)
        lines.append(
            f"timeout {_CARGO_TIMEOUT} cargo build -q -p cranelift-tools "
            "--bin clif-util 2>&1 | tail -3 || true"
        )
        lines.append(
            f"for _clif in {flist}; do "
            '[ -f "$_clif" ] || continue; '
            "if timeout 600 cargo run -q -p cranelift-tools --bin clif-util "
            '-- test "$_clif" >/dev/null 2>&1; '
            'then echo "test $_clif ... ok"; '
            'else echo "test $_clif ... FAILED"; fi; '
            "done"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

class WasmtimeImageDefault(Image):
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
        # Returns a string base image so the build harness injects the
        # REPO_URL / BASE_COMMIT build-args.  Note: dockerfile() below is
        # overridden to emit the full, proxy-free Dockerfile itself (it does NOT
        # rely on the shared Image.dockerfile()), so the registry's output is
        # self-contained and independent of the image.py it is built with.
        return f"rust:{_resolve_rust_version(self.pr)}"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # ca-certificates/curl/build-essential/git/make/python3/etc. are already
        # baked by Image.dockerfile(); add only what wasmtime's native build deps
        # need (cmake for cc-built components, patch for the apply fallback,
        # pkg-config/libssl-dev/clang for openssl-/bindgen-backed crates).
        return ["cmake", "patch", "pkg-config", "libssl-dev", "clang"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block (CWD is /home/{repo}).  Helper scripts + patches are staged into
        # /home/ (outside the git tree) so the hardening pass leaves them
        # untouched.  We add the wasm targets the test-programs build scripts
        # compile against, initialise submodules (spec_testsuite etc.), then
        # warm the build cache via prepare.sh so the run/test/fix stages reuse
        # target/ baked into this image instead of recompiling 3x.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY check_git_changes.sh /home/check_git_changes.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN rustup target add wasm32-unknown-unknown 2>/dev/null || true; \\\n"
            "    rustup target add wasm32-wasi 2>/dev/null || true; \\\n"
            "    rustup target add wasm32-wasip1 2>/dev/null || true; \\\n"
            "    rustup target add wasm32-wasip2 2>/dev/null || true\n"
            # Init submodules, then DEINIT only the wasi TEST-SUITE submodule
            # under tests/ (e.g. tests/wasi_testsuite + its nested wasi-common).
            # wasmtime registers it as a direct submodule with a huge binary-blob
            # history; the shared _HARDENING_BLOCK's recursive `git gc
            # --aggressive` (window=250, no-reuse-delta) on it takes 20-40 min
            # per image.  Deinit + drop its .git/modules so hardening's
            # `foreach --recursive` skips it.  The match is scoped to `^tests/`
            # so build-critical submodules such as crates/wasi-common/WASI (which
            # provides the `witx` crate wiggle depends on in older eras) are
            # NEVER removed.  spec_testsuite / component-model / wasm-c-api stay
            # and gc quickly.  The wasi-testsuite tests don't pass at baseline
            # anyway, so dropping them costs no F2P signal.
            "RUN git submodule update --init 2>/dev/null || true; \\\n"
            "    git submodule status 2>/dev/null | awk '{print $2}' "
            "| grep -E '^tests/.*wasi' \\\n"
            "        | xargs -r -n1 git submodule deinit -f 2>/dev/null || true; \\\n"
            "    rm -rf .git/modules/tests/*wasi* 2>/dev/null || true\n"
            "RUN bash /home/prepare.sh"
        )

    def dockerfile(self) -> str:
        # SELF-CONTAINED Dockerfile generation (proxy-free by construction).
        #
        # We emit the COMPLETE Dockerfile here ourselves — crucially including
        # the "# syntax=docker/dockerfile:1.6" directive on the first line.
        # DockerfileEnhancer.enhance() returns the Dockerfile UNCHANGED whenever
        # that directive is already present, so it short-circuits and can NOT
        # inject proxy / CA-cert / MITM configuration — even when this registry
        # is built against an image.py whose enhancer still carries that logic
        # (e.g. the aurora-main tree).  This makes the registry's output
        # independent of which image.py it is built with.
        #
        # dependency() still returns a string base image, so the build harness
        # injects the REPO_URL / BASE_COMMIT build-args referenced below, and we
        # reproduce the same clone -> checkout ${BASE_COMMIT} -> extra_setup ->
        # _HARDENING_BLOCK pipeline the shared Image.dockerfile() would have.
        base_img = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        default_packages = [
            "ca-certificates", "curl", "build-essential", "git",
            "gnupg", "make", "python3", "sudo", "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base_img}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="{repo_url}"\n'
                "ARG BASE_COMMIT"
            ),
            (
                "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                "    LANG=C.UTF-8 \\\n"
                "    TZ=UTC"
            ),
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
        ]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        extra_setup = self.extra_setup()
        if extra_setup:
            sections.append(extra_setup)

        # Anti-cheat hardening (detach at BASE_COMMIT, strip all other
        # refs/commits).  Uses the LIGHT variant (gc --prune=now, no
        # --aggressive) so the amd64/qemu multi-arch leg doesn't spend hours in
        # aggressive gc/pack-objects; the future fix commit is still pruned.
        sections.append(_HARDENING_BLOCK_LIGHT)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

    def files(self) -> list[File]:
        cargo_block = _cargo_test_block(self.pr)
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
                # No re-checkout: the shared Dockerfile already detached HEAD at
                # ${BASE_COMMIT}.  Warm the cache by running the targeted tests
                # once (baseline) so target/ is baked into the image.
                """#!/bin/bash
set -uxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

cargo fetch || true
{cargo_block}""".format(pr=self.pr, cargo_block=cargo_block),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{pr.repo}
{cargo_block}""".format(pr=self.pr, cargo_block=cargo_block),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || \\
git apply --whitespace=nowarn --3way /home/test.patch || \\
{{ patch --batch --fuzz=5 -p1 < /home/test.patch; }}
{cargo_block}""".format(pr=self.pr, cargo_block=cargo_block),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || \\
git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || \\
{{ patch --batch --fuzz=5 -p1 < /home/test.patch; patch --batch --fuzz=5 -p1 < /home/fix.patch; }}
{cargo_block}""".format(pr=self.pr, cargo_block=cargo_block),
            ),
        ]


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("bytecodealliance", "wasmtime")
class Wasmtime(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WasmtimeImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Standard libtest output:        "test <name> ... ok|FAILED|ignored"
        # Cranelift filetests / mimic:    "<name> ... ok|FAILED|ignored"
        # "- should panic" variants are also handled.
        re_pass_tests = [
            re.compile(r"test (\S+)(?: - should panic)? \.\.\. ok"),
            re.compile(r"(\S+) \.\.\. ok"),
        ]
        re_fail_tests = [
            re.compile(r"test (\S+)(?: - should panic)? \.\.\. FAILED"),
            re.compile(r"(\S+) \.\.\. FAILED"),
        ]
        re_skip_tests = [
            re.compile(r"test (\S+)(?: - should panic)? \.\.\. ignored"),
            re.compile(r"(\S+) \.\.\. ignored"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))
                    break

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))
                    break

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))
                    break

        # A test can appear in several of the targeted cargo invocations (the
        # same name lives in multiple wasi crates, or is timing-flaky under
        # qemu/load) and so show up as BOTH "... ok" and "... FAILED" in the
        # concatenated log.  The report builder requires passed/failed/skipped
        # to be disjoint, so collapse any test that ever failed (or was
        # skipped) out of the weaker sets: failure wins, then skip.
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


# ---------------------------------------------------------------------------
# number_interval derivation from `prs_in_bundle`
# ---------------------------------------------------------------------------
# The raw bundle records carry `prs_in_bundle` (e.g. [146, 147, 150, 155, 157])
# but leave `number_interval` unset.  The required output form is the EXPLICIT
# dash-joined member list ("146-147-150-155-157") — NOT a range like "146-157",
# which would wrongly imply every PR in between is part of the bundle.
#
# `PullRequest.from_json` drops unknown fields, so `prs_in_bundle` never reaches
# the harness and the emitted dataset's `number_interval` comes out empty.  We
# fix this in the registry (no edits to the shared harness core) with two
# narrowly-scoped, idempotent monkeypatches:
#
#   1. Wrap PullRequest.from_json to STASH the raw `prs_in_bundle` list onto the
#      parsed PR as a plain attribute `_prs_in_bundle`.  We deliberately do NOT
#      set `number_interval` here: Instance.create() routes on
#      `f"{org}/{number_interval}"` and raises if that key is unregistered, so a
#      populated number_interval at parse/route time would break every build.
#
#   2. Wrap Dataset.build (the single output-serialization point, invoked in
#      gen_report AFTER all routing is finished) to fill in
#      `number_interval = "-".join(prs_in_bundle)` from that stash whenever the
#      record's number_interval is still empty.  Routing never sees it; only the
#      emitted *_dataset.jsonl record does.
#
# Guarded with a sentinel so importing the registry twice patches only once.
import json as _json

from multi_swe_bench.harness.dataset import Dataset as _Dataset


def _number_interval_from_bundle(bundle) -> str:
    # Explicit member list in original bundle order; dash-joined, de-duplicated
    # while preserving order.  Range collapsing is intentionally avoided.
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


# NOTE: sentinels are checked against each class's OWN __dict__ (not getattr,
# which would see inherited attrs — Dataset subclasses PullRequest and would
# otherwise inherit the from_json sentinel and skip its own patch).
if "_wasmtime_from_json_patch" not in PullRequest.__dict__:
    _orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _pr_from_json_with_bundle(cls, json_str):
        pr = _orig_pr_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            bundle = raw.get("prs_in_bundle")
            if bundle:
                # stash only; number_interval stays "" so routing is unaffected
                pr._prs_in_bundle = list(bundle)
        except Exception:
            pass
        return pr

    PullRequest.from_json = _pr_from_json_with_bundle
    PullRequest._wasmtime_from_json_patch = True


if "_wasmtime_build_patch" not in _Dataset.__dict__:
    _orig_dataset_build = _Dataset.build.__func__

    @classmethod
    def _dataset_build_with_interval(cls, pr, report):
        ds = _orig_dataset_build(cls, pr, report)
        if not getattr(ds, "number_interval", ""):
            bundle = getattr(pr, "_prs_in_bundle", None)
            if bundle:
                ds.number_interval = _number_interval_from_bundle(bundle)
        return ds

    _Dataset.build = _dataset_build_with_interval
    _Dataset._wasmtime_build_patch = True
