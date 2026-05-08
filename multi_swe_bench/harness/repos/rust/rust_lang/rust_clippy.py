import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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


class RustClippyImageBase(Image):
    """Single base image for all PRs.

    Contains: rust:latest + cloned repo + frozen crates.io-index-archive.
    The frozen registry uses snapshot-2024-11-27 which covers all pre-2025 PRs.
    Nightly installation is deferred to the per-PR image.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._snapshot = _get_snapshot(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "rust:latest"

    def image_tag(self) -> str:
        if self._snapshot:
            # One base image per snapshot branch
            return f"base-{self._snapshot}"
        return "base-live"

    def workdir(self) -> str:
        return self.image_tag()

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

        frozen_block = ""
        if self._snapshot:
            frozen_block = f"""
RUN git clone --bare --single-branch --branch {self._snapshot} \\
    https://github.com/rust-lang/crates.io-index-archive.git /opt/crates-io-index && \\
    git --git-dir=/opt/crates-io-index branch master {self._snapshot} && \\
    git --git-dir=/opt/crates-io-index symbolic-ref HEAD refs/heads/master

RUN mkdir -p $CARGO_HOME && \\
    printf '[source.frozen]\\nregistry = "file:///opt/crates-io-index"\\n\\n[source.crates-io]\\nreplace-with = "frozen"\\n' > $CARGO_HOME/config.toml && \\
    cp $CARGO_HOME/config.toml $CARGO_HOME/config
"""

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN rm -f rust-toolchain.toml rust-toolchain

{frozen_block}

RUN git clean -fdx

{self.clear_env}

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
        nightly = PR_NIGHTLIES.get(self.pr.number)

        # Build prepare.sh with optional nightly injection
        nightly_inject = ""
        if nightly:
            # PR has no rust-toolchain.toml — inject one after checkout
            nightly_inject = f"""
# Inject correct nightly for this PR (no rust-toolchain.toml in repo)
echo '{nightly}' > rust-toolchain
rustup toolchain install {nightly}
rustup default {nightly}
"""

        # For live PRs that inherited a frozen base (shouldn't happen with current
        # routing, but defensive), remove the cargo source replacement
        live_override = ""
        if _needs_live_registry(self.pr.number):
            live_override = """
# Remove frozen registry config — this PR needs live crates.io
rm -f $CARGO_HOME/config.toml $CARGO_HOME/config
"""

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
{nightly_inject}
{live_override}
cargo test || true

""".format(pr=self.pr, nightly_inject=nightly_inject, live_override=live_override),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test

""".format(pr=self.pr),
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
