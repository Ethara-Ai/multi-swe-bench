import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Both image classes below override `dockerfile()` wholesale, which bypasses the
# validation the shared `Image.dockerfile()` performs on `pr.repo` before it is
# interpolated into RUN/WORKDIR paths. Every interpolated component is therefore
# routed through the shared `_safe_path_component` here, so the two paths carry
# the same guarantee (see multi_swe_bench/harness/image.py).
#
# `pr.base.sha` is the one value the shared helper does not cover: upstream it
# arrives as the `${BASE_COMMIT}` build-arg, but this registry substitutes it as
# a literal into `git checkout --detach` (build args are only supplied when
# `dependency()` returns a str, and both layers here depend on an Image). A sha
# is validated as hex so it cannot carry shell metacharacters into that command.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _safe_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe base sha for Dockerfile interpolation: {sha!r}")
    return sha


# ── PER-PR NIGHTLY MAPPING ─────────────────────────────────────────────────
# PRs without rust-toolchain.toml need an explicit nightly matching their
# base commit date.  These were derived from:
#   git log -1 --format=%ci <base_sha>
# for every PR whose base commit has NO rust-toolchain.toml.
PR_NIGHTLIES = {
    1443: "nightly-2017-01-13", 1465: "nightly-2017-01-21", 1467: "nightly-2018-04-15",
    1486: "nightly-2017-01-27", 1491: "nightly-2017-02-08", 1501: "nightly-2017-06-05",
    1506: "nightly-2017-02-04", 1513: "nightly-2017-09-28", 1536: "nightly-2017-06-16",
    1575: "nightly-2017-03-21", 1581: "nightly-2017-02-28", 1589: "nightly-2017-03-02",
    1610: "nightly-2017-03-05", 1671: "nightly-2017-04-07", 1705: "nightly-2017-04-28",
    1760: "nightly-2017-05-14", 1784: "nightly-2017-05-24", 1847: "nightly-2017-07-03",
    1861: "nightly-2017-08-25", 1869: "nightly-2017-08-01", 1883: "nightly-2017-07-10",
    1900: "nightly-2017-07-31", 1923: "nightly-2017-08-06", 1945: "nightly-2017-09-25",
    1959: "nightly-2017-08-21", 1963: "nightly-2017-09-04", 2034: "nightly-2017-09-09",
    2046: "nightly-2017-09-13", 2052: "nightly-2017-09-14", 2060: "nightly-2017-09-23",
    2129: "nightly-2017-10-20", 2166: "nightly-2017-11-22", 2168: "nightly-2017-10-31",
    2203: "nightly-2017-11-17", 2216: "nightly-2017-11-15", 2269: "nightly-2017-12-15",
    2291: "nightly-2017-12-21", 2296: "nightly-2018-01-10", 2298: "nightly-2018-01-15",
    2340: "nightly-2018-01-25", 2362: "nightly-2018-01-22", 2410: "nightly-2018-01-30",
    2415: "nightly-2018-02-02", 2439: "nightly-2018-02-05", 2483: "nightly-2018-02-26",
    2533: "nightly-2018-03-16", 2539: "nightly-2018-03-19", 2579: "nightly-2018-03-27",
    2590: "nightly-2018-03-30", 2592: "nightly-2018-06-19", 2712: "nightly-2018-05-11",
    2720: "nightly-2018-05-04", 2730: "nightly-2018-05-07", 2759: "nightly-2018-05-29",
    2763: "nightly-2018-05-17", 2777: "nightly-2018-05-19", 2786: "nightly-2018-05-20",
    2797: "nightly-2018-05-23", 2803: "nightly-2018-06-10", 2805: "nightly-2018-05-26",
    2832: "nightly-2018-06-25", 2857: "nightly-2019-01-15", 3257: "nightly-2018-11-27",
    3418: "nightly-2019-06-25", 4455: "nightly-2020-05-27", 4588: "nightly-2019-10-28",
    4604: "nightly-2019-10-11", 4841: "nightly-2020-06-23", 4897: "nightly-2020-02-01",
    5230: "nightly-2020-03-17", 5727: "nightly-2020-09-23", 6070: "nightly-2020-11-05",
}

# ── FROZEN REGISTRY SNAPSHOT SELECTION ──────────────────────────────────────
# Each PR gets the closest crates.io-index-archive snapshot AFTER its nightly
# date so that all crate versions in the snapshot are compatible with the
# compiler.  The available snapshot branches are:
#
#   2018-09-26, 2019-10-17, 2020-03-25, 2020-08-04, 2020-11-20,
#   2021-05-05, 2021-07-02, 2021-09-24, 2021-12-21, 2022-03-02,
#   2022-07-06, 2022-08-31, 2022-12-19, 2023-01-12, 2023-04-03,
#   2023-06-30, 2023-12-03, 2024-03-11, 2024-05-18, 2024-09-08,
#   2024-11-27, 2025-03-12, 2025-06-14, 2025-09-05, 2025-11-17,
#   2026-01-27, 2026-03-13, 2026-04-19
#
# For PRs without rust-toolchain.toml, the nightly date comes from PR_NIGHTLIES.
# For newer PRs, it comes from rust-toolchain.toml checked into the repo.
#
# The 14 earliest PRs (nightly < 2017-06-08) still use snapshot-2018-09-26;
# some crates there use pub(crate) which was only stabilised in Rust 1.18
# (2017-06-08).  These PRs may fail to compile dependencies — this is a
# known limitation of the earliest available snapshot.
#
# pr-12971 is special: needs askama ^0.13 (Oct 2025) but zmij 1.0.21
# (Feb 2026) breaks it, so it gets snapshot-2025-11-17.

_SNAPSHOT_DATES = [
    "2018-09-26", "2019-10-17", "2020-03-25", "2020-08-04", "2020-11-20",
    "2021-05-05", "2021-07-02", "2021-09-24", "2021-12-21", "2022-03-02",
    "2022-07-06", "2022-08-31", "2022-12-19", "2023-01-12", "2023-04-03",
    "2023-06-30", "2023-12-03", "2024-03-11", "2024-05-18", "2024-09-08",
    "2024-11-27", "2025-03-12", "2025-06-14", "2025-09-05", "2025-11-17",
    "2026-01-27", "2026-03-13", "2026-04-19",
]


def _find_snapshot(nightly_date: str) -> str:
    """Closest snapshot branch on or after *nightly_date*."""
    for snap in _SNAPSHOT_DATES:
        if snap >= nightly_date:
            return f"snapshot-{snap}"
    return f"snapshot-{_SNAPSHOT_DATES[-1]}"


# Nightly dates from rust-toolchain.toml at each PR's base commit
_TOOLCHAIN_NIGHTLIES = {
    3875: "2022-10-20", 6179: "2021-01-30", 6342: "2021-03-11",
    7160: "2021-06-03", 7338: "2021-08-12", 7359: "2022-05-05",
    7463: "2021-11-04", 7962: "2022-09-08", 8037: "2021-12-30",
    8070: "2022-02-10", 8355: "2022-06-16", 8403: "2022-03-24",
    8685: "2023-06-29", 8694: "2022-07-28", 9102: "2023-02-25",
    9701: "2022-12-01", 9948: "2024-11-14", 10028: "2023-01-12",
    10155: "2024-05-30", 10175: "2023-04-06", 10283: "2023-12-16",
    10300: "2023-09-25", 10358: "2023-05-20", 10595: "2023-08-10",
    10903: "2024-01-25", 11002: "2023-11-02", 11287: "2024-03-07",
    11421: "2025-02-06", 11441: "2024-07-11", 11476: "2024-08-23",
    11540: "2024-04-18", 11796: "2024-10-03", 12971: "2025-03-20",
    13207: "2025-05-01", 13465: "2024-12-26", 13696: "2025-06-12",
    13787: "2025-09-04", 14177: "2025-07-25", 14361: "2025-10-16",
    14966: "2025-11-28", 15629: "2026-01-08",
}


def _nightly_date(pr_number: int) -> Optional[str]:
    """Return the nightly date (YYYY-MM-DD) for a PR, or None if unknown."""
    nightly = PR_NIGHTLIES.get(pr_number)
    if nightly:
        # "nightly-YYYY-MM-DD" → "YYYY-MM-DD"
        return nightly.split("-", 1)[1]
    return _TOOLCHAIN_NIGHTLIES.get(pr_number)


PR_SNAPSHOTS: dict[int, str] = {12971: "snapshot-2025-11-17"}
for _pr_num in list(PR_NIGHTLIES) + list(_TOOLCHAIN_NIGHTLIES):
    if _pr_num in PR_SNAPSHOTS:
        continue
    _date = _nightly_date(_pr_num)
    if _date:
        PR_SNAPSHOTS[_pr_num] = _find_snapshot(_date)

# Default snapshot for any PR not in PR_SNAPSHOTS and not live
DEFAULT_SNAPSHOT = "snapshot-2024-11-27"

# PRs that use live crates.io (no frozen registry)
LIVE_PR_RANGES = [(11421, 11421), (11797, 12970), (12972, 99999)]


def _needs_live_registry(pr_number: int) -> bool:
    for lo, hi in LIVE_PR_RANGES:
        if lo <= pr_number <= hi:
            return True
    return False


def _get_snapshot(pr_number: int) -> Optional[str]:
    """Return the frozen registry snapshot branch for this PR, or None for live."""
    if _needs_live_registry(pr_number):
        return None
    return PR_SNAPSHOTS.get(pr_number, DEFAULT_SNAPSHOT)


# ── LEGACY-COHORT MANIFEST STRIP ────────────────────────────────────────────
# The PR_NIGHTLIES cohort (chunk 2, 2017-2018 base commits) fails to build
# because each bundle's fix_patch is the CUMULATIVE diff up to a modern head, so
# it rewrites Cargo.toml/Cargo.lock to require modern crate versions (e.g. regex
# 1.8, 2023) that the PR's own 2017-2018 toolchain cannot even parse
# ("editions are unstable", "feature `edition` is required"). cargo dies at
# dependency resolution before any test runs -> zero tests captured -> invalid.
#
# Stripping the Cargo.toml / Cargo.lock file-sections from fix_patch leaves the
# OLD base manifest in place, so cargo resolves against the era-correct frozen
# index and the old toolchain can build it. This is a PROBE: it only helps PRs
# whose *source* fix does not itself need the newer dependency; PRs that added a
# real dependency will still fail with "unresolved import / cannot find". Scoped
# strictly to PR_NIGHTLIES so the modern cohort (chunk 1, already finalized at
# 36) is never touched — its fix_patch is emitted verbatim, exactly as before.
_MANIFEST_DIFF_HEADER = re.compile(
    r"^diff --git a/(?:.*/)?(Cargo\.toml|Cargo\.lock) b/", re.MULTILINE
)


def _strip_manifest_hunks(fix_patch: str) -> str:
    """Remove every Cargo.toml / Cargo.lock file-section from a unified diff.

    A git diff is a sequence of `diff --git ...` sections; drop the whole section
    (header through the line before the next `diff --git`) for manifest files and
    keep everything else untouched.
    """
    if "Cargo.toml" not in fix_patch and "Cargo.lock" not in fix_patch:
        return fix_patch
    sections = re.split(r"(?m)(?=^diff --git )", fix_patch)
    kept = [
        s
        for s in sections
        if not _MANIFEST_DIFF_HEADER.match(s)
    ]
    return "".join(kept)


class RustClippyImageBase(Image):
    """ONE base image for all 113 PRs: rust:latest + the cloned repo.

    The crates.io index deliberately does NOT live here. It used to, pinned to a
    single snapshot branch, which forced one base image PER SNAPSHOT — 23 of them
    for this dataset, 13 of which served <=2 PRs each. Two facts make that the
    wrong shape:

      * The snapshot branches of crates.io-index-archive are INDEPENDENT orphan
        histories, not points on one timeline (the 2018-09-26 tip is not an object
        in a 2024-11-27 clone, and 2018 carries MORE commits than 2024). So a
        single base holding every branch is the sum of them, ~7GB+, and since each
        per-image tar is a standalone OCI export that cost would land in all 113
        PR tars.
      * a PR only ever needs ONE branch, so a single-branch clone of exactly the
        snapshot it is pinned to is sufficient. (It must be full-depth, not
        `--depth 1` — see the note in prepare.sh: libgit2 in older cargo cannot
        read a shallow repository, which silently zeroes out every pre-2023-08
        toolchain.)

    So the index moved into the per-PR layer (see prepare.sh), single-branch.
    Every PR still resolves against the exact same snapshot it did before — same
    branch, same crate versions — so this changes packaging only, not resolution.

    Nightly installation is likewise deferred to the per-PR image.
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
        return "rust:latest"

    def image_tag(self) -> str:
        # Constant: this base carries nothing PR-specific, so all 113 PRs share
        # one image instead of the 23 the per-snapshot tag used to produce.
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        # REPO_URL is declared as an ARG and consumed here, so the
        # `--build-arg REPO_URL=...` the pipeline already passes for every
        # str-dependency image is actually honoured instead of silently
        # discarded against a hardcoded URL.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` opt-out: this is THE shared base, reused by all 113 PRs.
        # DockerfileEnhancer would otherwise
        # rewrite the clone to `git checkout ${{BASE_COMMIT}}` + hardening, pinning
        # the shared base to a single commit and gc-pruning it — which breaks every
        # other PR's `git checkout {{base.sha}}`. The base must keep FULL history;
        # the per-PR image (RustClippyImageDefault) checks out + hardens to its own
        # base commit instead.
        #
        # Because the enhancer is skipped, its ARG/ENV/LABEL block is skipped too,
        # so it is written out here by hand (same shape as the sqlalchemy shared
        # bases). Without it these images carry NO provenance labels at all — they
        # inherit only `image.source=github.com/rust-lang/docker-rust` from
        # rust:latest, i.e. every published image misreports where it came from.
        #
        # BASE_COMMIT is deliberately NOT declared: this base is shared by up to 61
        # PRs with 61 different base shas, so any single value would be wrong. The
        # per-PR layer pins its own sha instead.
        #
        # No proxy/CA-cert/MITM args either — the build reaches the network
        # directly and trusts rust:latest's own CA store.
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} shared base (all PRs)" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

{label_block}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{repo}

RUN rm -f rust-toolchain.toml rust-toolchain

RUN git clean -fdx

{self.clear_env}

CMD ["/bin/bash"]
"""


class RustClippyImageDefault(Image):
    """Per-PR image.

    Installs the exact nightly for this PR and runs prepare.sh.
    For PRs in PR_NIGHTLIES: writes a rust-toolchain file with the correct nightly.
    For PRs with their own rust-toolchain.toml: git checkout restores it.
    For live PRs (R7): removes the frozen cargo config if present.
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

    def dependency(self) -> Image | None:
        return RustClippyImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = _safe_path_component(self.pr.repo)
        sha = _safe_sha(self.pr.base.sha)
        nightly = PR_NIGHTLIES.get(self.pr.number)

        # Build prepare.sh with optional nightly injection
        nightly_inject = ""
        if nightly:
            # PR has no rust-toolchain.toml — inject one after checkout.
            #
            # The COMPONENTS matter as much as the channel. clippy_lints links
            # against the compiler's own crates, so without `rustc-dev` the build
            # dies at `error[E0463]: can't find crate for 'rustc_ast'` and NOT ONE
            # test runs — while `cargo test || true` below still lets the image
            # build "successfully". PRs that ship their own rust-toolchain.toml
            # get these from the repo (clippy declares cargo/llvm-tools/rust-src/
            # rust-std/rustc/rustc-dev/rustfmt); the ones routed here previously
            # got a bare channel name and therefore only cargo/rust-std/rustc.
            #
            # Component availability varies by nightly age (rustc-dev and
            # llvm-tools-preview did not exist for the 2017-2018 nightlies), so the
            # install degrades: try with components, fall back to the bare channel,
            # then add each component best-effort. prepare.sh runs under `set -e`,
            # hence the explicit guards.
            nightly_inject = f"""
# Inject correct nightly for this PR (no rust-toolchain.toml in repo)
echo '{nightly}' > rust-toolchain
rustup toolchain install {nightly} \\
        --component rustc-dev llvm-tools-preview rust-src rustfmt \\
    || rustup toolchain install {nightly}
rustup default {nightly}
for _c in rustc-dev llvm-tools-preview rust-src rustfmt; do
    rustup component add --toolchain {nightly} "$_c" 2>/dev/null || true
done
rustup component list --toolchain {nightly} --installed || true
"""

        # Frozen crates.io index — PER PR, not in the base.
        #
        # This is what collapses 23 base images into 1: the index used to be baked
        # into the base, so every distinct snapshot forced its own base image. It is
        # cloned here instead, SHALLOW and single-branch, because cargo only reads
        # the index tree at HEAD and never walks its history. Verified: a depth-1
        # snapshot-2024-11-27 index resolves and locks serde v1.0.215, identically
        # to a full-depth clone.
        #
        # The snapshot each PR gets is unchanged from the per-base arrangement —
        # _get_snapshot(pr.number) is the same call the base used to make — so crate
        # resolution is bit-for-bit what it was. Shallow also makes it much smaller:
        # 11MB (2018) / 134MB (2024) vs 127MB / 403MB at full depth.
        #
        # Live PRs (LIVE_PR_RANGES) get no block at all and therefore no source
        # replacement, so cargo talks to real crates.io. The base ships no cargo
        # config, so there is nothing to undo for them.
        snapshot = _get_snapshot(self.pr.number)
        frozen_setup = ""
        if snapshot:
            frozen_setup = f"""
# Frozen crates.io index for this PR's snapshot.
#
# NOT shallow. `--depth 1` looks safe (cargo only reads the index tree at HEAD)
# and works on current cargo, but cargo fetches this path through libgit2, and
# libgit2 in cargo older than ~1.73 cannot read a shallow repository. Every PR on
# a pre-2023-08 nightly then dies with:
#     failed to fetch `file:///opt/crates-io-index`
#     object not found - no match for id (...); class=Odb (9); code=NotFound (-3)
# and captures ZERO tests in all three stages. Confirmed against the full chunk-1
# run: all 18 PRs with a nightly <= 2023-06-29 failed this way, while the 2024+
# ones resolved fine. Full depth costs 127MB (2018) - 403MB (2024) per PR instead
# of 11MB - 134MB; that is the price of supporting the old toolchains.
git clone --bare --single-branch --branch {snapshot} \\
    https://github.com/rust-lang/crates.io-index-archive.git /opt/crates-io-index
git --git-dir=/opt/crates-io-index branch -f master {snapshot}
git --git-dir=/opt/crates-io-index symbolic-ref HEAD refs/heads/master
mkdir -p "$CARGO_HOME"
printf '[source.frozen]\\nregistry = "file:///opt/crates-io-index"\\n\\n[source.crates-io]\\nreplace-with = "frozen"\\n' > "$CARGO_HOME/config.toml"
"""
        else:
            frozen_setup = """
# Live crates.io for this PR — ensure no source replacement is in effect
rm -f "$CARGO_HOME/config.toml" "$CARGO_HOME/config"
"""

        # Legacy cohort (chunk 2) only: strip the Cargo.toml/Cargo.lock
        # modernization from the bundle's cumulative fix_patch so the old base
        # toolchain can resolve era-correct dependencies. The modern cohort
        # (chunk 1) is NOT in PR_NIGHTLIES, so its fix_patch is emitted verbatim
        # and its finalized results are unaffected.
        fix_patch_content = self.pr.fix_patch
        if self.pr.number in PR_NIGHTLIES:
            fix_patch_content = _strip_manifest_hunks(fix_patch_content)

        return [
            File(
                ".",
                "fix.patch",
                f"{fix_patch_content}",
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
{frozen_setup}
{nightly_inject}
cargo test || true

""".format(
                    repo=repo,
                    sha=sha,
                    frozen_setup=frozen_setup,
                    nightly_inject=nightly_inject,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
cargo test

""".format(repo=repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply /home/test.patch
cargo test

""".format(repo=repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply /home/test.patch /home/fix.patch
cargo test

""".format(repo=repo),
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

        repo = _safe_path_component(self.pr.repo)
        org = _safe_path_component(self.pr.org, "org")
        sha = _safe_sha(self.pr.base.sha)

        # This layer depends on an Image, so DockerfileEnhancer returns it
        # verbatim and its ARG/LABEL block never gets injected. Written out here
        # for the same reason as in the base: without it the published PR images
        # carry no provenance labels. TARGETARCH is a predefined build arg that
        # buildx supplies automatically for multi-platform builds — declaring it
        # is enough, no --build-arg needed (and none is passed to this stage).
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} pr-{self.pr.number} (base {tag})" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # Anti-reward-hacking HEAD strip, taken VERBATIM from the shared
        # Image._HARDENING_BLOCK rather than hand-copied, so this registry cannot
        # drift from the canonical hardening when image.py changes. (The previous
        # revision inlined its own copy and had already fallen a revision behind:
        # it was missing the `.gitmodules` submodule pass entirely.)
        #
        # It has to live in the PER-PR layer, not the base: the base image is
        # SHARED by every PR on the same crates.io snapshot and must keep full
        # history for each of their `git checkout {base.sha}` to resolve.
        #
        # ${BASE_COMMIT} is substituted with the literal sha because build args
        # are only supplied by the builder when dependency() returns a str (see
        # build_dataset._build_image), and this layer depends on an Image. The
        # substitution keeps the shell quoting of the shared block intact
        # (`"${BASE_COMMIT}"` -> `"<sha>"`), and the sha is hex-validated above.
        #
        # WORKDIR is set explicitly rather than inherited from the base layer:
        # the shared block has no `cd`, and its second RUN (the submodule pass)
        # must run from the repo root too.
        #
        # After this block the container has no origin remote, no branches/tags/
        # remote refs, no reflog and no unreachable objects — the commit that
        # fixes the bug is not present in the image and cannot be logged,
        # shown, fetched or cherry-picked. The trailing `test` assertions fail
        # the BUILD if any of that is untrue, so it is verified, not assumed.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG TARGETARCH

{label_block}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("rust-lang", "rust-clippy")
class RustClippy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RustClippyImageDefault(self.pr, self._config)

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

    # Old compiletest_rs format (clippy pre-~2023): `test [ui] ui/foo.rs ... ok`.
    # The `[suite]` tag sits between `test ` and the path, so the anchored
    # `test (\S+) ...` matchers below capture `[ui]` (never ` ... ok`) and every
    # such line yields NOTHING — old-toolchain PRs then score zero tests and are
    # rejected as "no test results captured". Verified on pr-8694 (nightly
    # 2022-07-28): 773 `test [ui] ...` lines, ~0 parsed. This is the entire
    # pre-2023 cohort (all of chunk 2's older half), so the gap silently zeroed
    # them. Capture the path AFTER the tag and normalize `ui/foo.rs` ->
    # `tests/ui/foo.rs` so it equals the modern ui_test name for the same file
    # (keeps f2p stable across the format boundary and lets the report's tamper
    # guard match old-format test files by path exactly as it does modern ones).
    _re_old_pass = re.compile(r"test \[[\w./+-]+\] (\S+) \.\.\. ok")
    _re_old_fail = re.compile(r"test \[[\w./+-]+\] (\S+) \.\.\. FAILED")
    _re_old_skip = re.compile(r"test \[[\w./+-]+\] (\S+) \.\.\. ignored")

    @staticmethod
    def _normalize_old(path: str) -> str:
        # `ui/foo.rs` -> `tests/ui/foo.rs`; idempotent for already-rooted paths.
        return path if path.startswith("tests/") else f"tests/{path}"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"test (\S+) \.\.\. ok"),
            re.compile(r"(\S+) \.\.\. ok"),
        ]
        re_fail_tests = [
            re.compile(r"test (\S+) \.\.\. FAILED"),
            re.compile(r"(\S+) \.\.\. FAILED"),
        ]
        re_skip_tests = [
            re.compile(r"test (\S+) \.\.\. ignored"),
            re.compile(r"(\S+) \.\.\. ignored"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            # Old compiletest format first: it is a superset the generic anchored
            # matchers below cannot read, so it must win before them. A line that
            # matches here is fully classified — skip the modern matchers.
            m = self._re_old_pass.match(line)
            if m:
                passed_tests.add(self._normalize_old(m.group(1)))
                continue
            m = self._re_old_fail.match(line)
            if m:
                failed_tests.add(self._normalize_old(m.group(1)))
                continue
            m = self._re_old_skip.match(line)
            if m:
                skipped_tests.add(self._normalize_old(m.group(1)))
                continue

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population — REGISTRY-SCOPED shim (no other file edited).
#
# The output/resolve jsonl writes `number_interval` straight off the loaded
# PullRequest (Dataset.build -> number_interval=pr.number_interval), but the
# bundle's PR list (`prs_in_bundle`) is dropped when the raw record is parsed:
# PullRequest is a dataclass_json model and discards unknown keys, and nothing
# in the harness derives the interval. Every rust-clippy row would therefore
# ship `number_interval: ""`.
#
# The interval is the EXPLICIT dash-joined bundle — "146-147-150-155-157" — and
# deliberately NOT a range like "146-157", which would assert that 148/149/…
# are in the bundle when they are not. This matches how the dataset's own
# `instance_id` is already built (org__repo-<dash-joined prs_in_bundle>).
#
# As this must live ONLY in the registry, two small idempotent rust-clippy-scoped
# shims are installed at import time:
#
#   1. PullRequest.from_json — for rust-lang/rust-clippy records whose
#      number_interval is empty, fill it from the raw line's prs_in_bundle.
#      That value then flows into the report and the output dataset record.
#   2. Instance.create — a non-empty number_interval makes routing look up
#      `rust-lang/<that-list>`, which is not a registered key; fall back to
#      `rust-lang/rust-clippy` so the build still routes.
#
# Both wrap whatever is currently installed rather than the pristine original,
# so they compose with the identical shims other registries install (e.g.
# radareorg/radare2). Only EMPTY number_intervals are filled, so era-keyed
# datasets that pre-set a registered `org/<era>` key are untouched, and other
# repos never reach either shim's body.
# ---------------------------------------------------------------------------
import json as _clippy_json  # noqa: E402

from multi_swe_bench.harness.pull_request import (  # noqa: E402
    PullRequest as _ClippyPullRequest,
)

_CLIPPY_ORG = "rust-lang"
_CLIPPY_REPO = "rust-clippy"


def _clippy_number_interval(prs) -> str:
    """`[146, 147, 150]` -> `"146-147-150"`.

    Bundle order is preserved (it is the dataset's own ordering), non-integers
    are skipped and repeats dropped, so a malformed field degrades to a shorter
    interval rather than poisoning the record.
    """
    seen: set[int] = set()
    out: list[str] = []
    for p in prs or []:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(str(n))
    return "-".join(out)


if not getattr(_ClippyPullRequest, "_rust_clippy_ni_shim", False):
    _clippy_prev_from_json = _ClippyPullRequest.from_json.__func__

    def _clippy_from_json(cls, json_str):
        pr = _clippy_prev_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _CLIPPY_ORG
                and getattr(pr, "repo", "") == _CLIPPY_REPO
                and not getattr(pr, "number_interval", "")
            ):
                raw = _clippy_json.loads(json_str) or {}
                interval = _clippy_number_interval(raw.get("prs_in_bundle"))
                if interval:
                    pr.number_interval = interval
        except Exception:
            # A record we cannot enrich must still load: an empty interval only
            # costs the metadata field, a raise would drop the whole instance.
            pass
        return pr

    _ClippyPullRequest.from_json = classmethod(_clippy_from_json)
    _ClippyPullRequest._rust_clippy_ni_shim = True


if not getattr(Instance, "_rust_clippy_route_shim", False):
    _clippy_prev_create = Instance.create.__func__

    def _clippy_create(cls, pr, config, *args, **kwargs):
        try:
            return _clippy_prev_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _CLIPPY_ORG
                and getattr(pr, "repo", "") == _CLIPPY_REPO
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_clippy_create)
    Instance._rust_clippy_route_shim = True
