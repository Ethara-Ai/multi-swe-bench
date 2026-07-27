import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe commit sha for Dockerfile interpolation: {sha!r}")
    return sha


# ---------------------------------------------------------------------------
# Era: vercel-labs/agent-browser PRs >= 564 (the "Rust native rewrite").
#
# At these base commits the CLI is a Rust binary crate under `cli/` (no root
# Cargo workspace).  The relevant regression tests added by the dataset's
# test_patch live in `cli/src/native/e2e_tests.rs` and are `#[ignore]`d --
# they launch a real Chrome via CDP, so they only run with `-- --ignored`.
#
# Discovery (Docker, host arch arm64, verified):
#   * `rust:1-bookworm` -> rustc 1.95, Debian chromium 148 + ffmpeg via apt.
#   * Chrome-for-Testing has no Linux ARM64 build, but the e2e tests resolve
#     the browser through `find_chrome()` (which $PATH chromium) /
#     `AGENT_BROWSER_EXECUTABLE_PATH`, so system chromium works on amd64+arm64.
#   * In a container Chrome auto-gets --no-sandbox / --disable-dev-shm-usage
#     (root + /.dockerenv detection); `CI=true` makes that explicit.
#   * `cargo test --profile ci --manifest-path cli/Cargo.toml` builds the bin
#     unittests (it is a binary crate -- `--lib` fails).  Running with
#     `-- --include-ignored --test-threads=1` runs the normal suite *and* the
#     serial Chrome e2e tests in one pass.
#   * Verified `e2e_launch_navigate_evaluate_close` and the full `e2e` suite
#     pass; `git apply --whitespace=nowarn` applies the PR test/fix patches.
#
# Two-stage build (base image + per-PR image) -- see the longer comment in
# agent_browser_0_to_563.py for the full rationale. Same shape here: ImageBase
# clones the repo (no commit pinned) and installs the OS-level Chrome/ffmpeg
# stack once, cached across every Rust-era PR; ImageDefault is FROM ImageBase
# and re-applies image.py's full _HARDENING_BLOCK against its own BASE_COMMIT
# after inheriting the base image's full, unstripped git history -- that
# re-application (not the chaining itself) is what keeps this safe.
# ---------------------------------------------------------------------------


class ImageBase(Image):
    """Shared, commit-agnostic base image for the Rust-native era.

    Clones the repo once and installs the OS-level Chrome/ffmpeg stack (and
    warms the cargo registry cache). Carries no BASE_COMMIT and no hardening
    -- it is never evaluated against directly, only used as the FROM of
    ImageDefault, which re-hardens per commit.
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

    def dependency(self) -> str:
        return "rust:1-bookworm"

    def image_tag(self) -> str:
        # Constant (not PR-derived): every Rust-era PR resolves the same tag,
        # so Docker/the harness build this once and every PR image reuses it.
        # Named after the era's PR range (matches this file's own name), not
        # the toolchain, per convention.
        return "base-564-99999"

    def workdir(self) -> str:
        return "base-564-99999"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Syntax directive prefix makes DockerfileEnhancer.enhance() skip its
        # proxy ARGs / CA-cert symlinks / MITM mount (same reasoning as
        # ImageDefault.dockerfile() below) so this base image stays exactly
        # what's written here.
        repo = self.pr.repo
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM rust:1-bookworm
ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV CI=true
# Deliberately NOT setting AGENT_BROWSER_EXECUTABLE_PATH here. find_chrome()
# (cli/src/native/cdp/chrome.rs) already does `which chromium` as a fallback,
# which finds the apt-installed /usr/bin/chromium on its own -- so e2e tests
# don't need this var set globally. Setting it as a persistent container ENV
# used to leak into flags::tests::test_parse_executable_path_flag_no_value
# (cli/src/flags.rs), which asserts executable_path is None when no CLI flag
# and no env var are given; a global ENV here made that assertion false for
# every PR in this era, an unconditional false failure unrelated to the fix.
#
# Containers run as root, where $HOME defaults to /root. connection.rs's
# get_socket_dir() falls back to dirs::home_dir() (reads $HOME), and
# test_get_socket_dir_home_fallback asserts that fallback path contains
# "home" or "Users" -- which /root never does, so that test panics on every
# PR in this era. The panic happens while holding test_utils.rs's ENV_MUTEX
# (a process-wide Mutex<()> serializing every env-var-mutating test), and a
# panic-while-held poisons a std::sync::Mutex -- so every later test in the
# same run that also touches ENV_MUTEX panics too with PoisonError, a
# cascade whose exact membership depends on execution order (confirmed: 5
# tests in some runs, 9 in others). Setting HOME to a path containing "home"
# prevents the initial panic, which prevents the whole cascade.
ENV HOME=/home/{repo}
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (Rust-era base)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    pkg-config \\
    chromium \\
    ffmpeg \\
    dbus \\
    dbus-x11 \\
    && rm -rf /var/lib/apt/lists/*

# rm -rf first: makes this step idempotent against a stale/reused BuildKit
# cache layer that already has something at this path (observed on
# base-0-563's arm64 leg: `git clone` failing with "destination path ...
# already exists" even though nothing earlier in this Dockerfile creates
# it) -- rebuilding this same tag repeatedly with different content across
# the base image's lifetime is exactly the kind of history that produces a
# stale cache hit like this. Applying the same defensive fix here too,
# since nothing about this base image's history makes it less exposed.
RUN rm -rf /home/{repo} && git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}
RUN cargo fetch --manifest-path cli/Cargo.toml || true

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # dependency() returns an Image, so the shared Image.dockerfile()
        # refuses to run and this class must build the Dockerfile itself.
        # REPO_URL/BASE_COMMIT never arrive as build-args in this case (the
        # harness only injects those when dependency() is a string), so the
        # commit is baked in directly rather than via an ARG -- validated
        # first (_safe_sha) since it's raw string interpolation.
        #
        # Image._HARDENING_BLOCK is reused verbatim (imported, not
        # hand-copied) so this can never silently drift from the canonical
        # definition in image.py; "${BASE_COMMIT}" is substituted with the
        # literal, validated sha since no ARG declares it here.
        base = self.dependency()
        repo = self.pr.repo
        sha = _safe_sha(self.pr.base.sha)
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base.image_full_name()}

WORKDIR /home/{repo}
RUN git fetch origin || true

{hardening}

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
COPY prepare.sh /home/prepare.sh
RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""

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
                "prepare.sh",
                """#!/bin/bash
# Warm the cargo build cache at image-build time so the eval runs don't need
# network. The repo is already checked out at ${{BASE_COMMIT}} and hardened by
# ImageDefault.dockerfile() above, so this script no longer performs any git
# checkout itself. `cargo fetch` already warmed the registry cache once in
# the base image against its own HEAD snapshot; this build picks up whatever
# changed between that snapshot and this commit's actual Cargo.toml/lock. The
# cargo build is allowed to fail (|| true) because its only purpose here is
# to populate the target/ cache; the real pass/fail signal comes from the
# run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true
# [profile.ci] was introduced mid-era (absent at PR 594 / v0.15.3); fall back
# to the default profile when it is missing.  Binary crate: no --lib.
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml --no-run || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# Headless Chromium needs a D-Bus session bus; without one, the e2e suite
# intermittently crashes or drops its CDP connection ("Failed to connect to
# the bus", "Event stream closed"). Verified fix across three distinct
# failure symptoms (PRs 766, 996, both previously flaky) -- all pass cleanly
# once this daemon is running. || true: a daemon that's already running (or
# fails to start for an unrelated reason) shouldn't fail the whole test run
# -- the real pass/fail signal is the test suite itself.
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci"
cargo test $PROFILE --manifest-path cli/Cargo.toml -- --include-ignored --test-threads=1

""".format(pr=self.pr),
            ),
        ]


@Instance.register("vercel-labs", "agent_browser_564_to_99999")
class AGENT_BROWSER_564_TO_99999(Instance):
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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_re.sub("", test_log)

        # Standard libtest output, e.g.:
        #   test native::e2e_tests::e2e_launch_navigate_evaluate_close ... ok
        #   test some::unit::test ... FAILED
        #   test other::test ... ignored
        re_pass = re.compile(r"^test (\S+) \.\.\. ok\b")
        re_fail = re.compile(r"^test (\S+) \.\.\. FAILED\b")
        re_skip = re.compile(r"^test (\S+) \.\.\. ignored\b")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

        # Also harvest the trailing "failures:" summary block as a safety net
        # (a panicking test still lists its name there even if the per-test
        # line was interleaved with captured stdout).
        if "\nfailures:\n" in test_log or test_log.startswith("failures:\n"):
            for block in re.findall(r"\nfailures:\n((?:    \S+\n)+)", test_log):
                for name in block.splitlines():
                    name = name.strip()
                    if name and not name.startswith("----"):
                        failed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# Bundle-NI registrations (Rule 5, Pattern 2).
# vercel-labs__agent-browser_lht_final.jsonl records are release-delta
# bundles: number_interval is the sorted dash-joined prs_in_bundle (e.g.
# "146-147-150-155-157"), NOT a "146-157" range -- a bundle skips PRs that
# didn't land on this release line, so the range form would silently claim
# PRs this record's fix_patch/test_patch never touched.
# Instance.create() looks up f"{pr.org}/{pr.number_interval}"; each NI below
# is registered to whichever era class owns its anchor PR (the bundle's
# lowest number, i.e. row["number"]): <=563 -> the TS-era class in
# agent_browser_0_to_563.py, >=564 -> AGENT_BROWSER_564_TO_99999 above.
# ---------------------------------------------------------------------------
from multi_swe_bench.harness.repos.rust.vercel_labs.agent_browser_0_to_563 import (
    AGENT_BROWSER_0_TO_563,
)

_TS_ERA_BUNDLE_NIS = [
    "184-451-452",
    "236-242-243",
    "3-35-68-99-109-138-141-154-157-164-180-181-183-188-190-203-205-216-217-218-219-220",
    "358-359-360",
    "373-374-375-376-377",
    "416-420-421-424-427-428-429-430",
    "502-745-746-747-748-749-750-752-755-756",
    "503-504-505",
    "510-512-513",
    "536-537-538-543-544-545",
    "563-564-571-573-576-581-583-585",
    "290-475-486-487-488-492-493-494-495-496",
    "385-400-401-403-404-406-407-408",
    "515-521-524-527-528-529-531-533-534-535",
    "93-200-247-260-266-268-270-272-274-275-276-280-281",
]

_RUST_ERA_BUNDLE_NIS = [
    "1040-1042-1059-1064-1066-1075-1088-1089-1090",
    "1087-1095-1096",
    "1111-1257-1273-1305-1328-1330-1332",
    "1145-1153-1154-1156-1160-1163-1165-1166-1167-1168",
    "1208-1218-1220-1233-1244-1245-1246",
    "594-595-596-597",
    "607-609-610",
    "625-687-690-694-695-696-697-698-699-700-701-704-706-707-713-717-718-720-722-729-730-731-734-736-737-738-740-742",
    "671-691-692-693",
    "754-757-759-760-761-762-763-768-769-770-771-772",
    "766-783-784-786-787-789-790",
    "802-803-804",
    "806-808-809",
    "836-837-838-839",
    "840-854-855-856-857-858-859-860",
    "842-844-845",
    "892-1241-1242-1248-1249-1250-1251-1253-1254-1255",
    "935-949-952-955-960-961-964-968-969-970-971-972-973-975",
    "945-948",
    "951-1008-1014-1015-1019-1023-1025-1027",
    "1033-1161-1178-1202-1225-1227-1228",
    "605-614-619-620-630-637-646-648-649-650-652-654-675-683-684",
    "624-872-894-909-915-919-920",
    "996-1110-1122-1126-1131-1132-1133-1134-1135-1136-1137-1142",
]

for _ni in _TS_ERA_BUNDLE_NIS:
    Instance._registry[f"vercel-labs/{_ni}"] = AGENT_BROWSER_0_TO_563

for _ni in _RUST_ERA_BUNDLE_NIS:
    Instance._registry[f"vercel-labs/{_ni}"] = AGENT_BROWSER_564_TO_99999
