import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Shared environment stage: OS packages, Python test deps, repo checkout.

    Split out of ImageDefault so this repo matches the two-stage layout the other
    registries use (see repos/python/alteryx/woodwork.py). build_dataset.py
    creates one directory per *Image object* in the dependency chain, named by
    that object's workdir() -- so a single-stage config produced only
    workdir/<org>/<repo>/images/pr-<n>/ and no images/base/.

    dependency() returns a STRING, which is what makes this the base stage:
    build_dataset.py:622-630 passes REPO_URL and BASE_COMMIT as build args only
    when dependency() is a str.

    dockerfile() is overridden here rather than inheriting the generic
    Image.dockerfile(). The generic one appends
    "WORKDIR /home/\\nENV DEBIAN_FRONTEND=noninteractive\\nENV LANG=C.UTF-8",
    but DockerfileEnhancer._ENV_BLOCK has already set both of those to the same
    values further up the file -- so every generated base Dockerfile carried a
    duplicate ENV. Writing the body here drops the redundant pair without
    touching shared harness code, and matches how the reference base config does
    it (repos/typescript/sindresorhus/p_retry.py also overrides dockerfile()).

    Note the tag is per base-sha, so the base is shared across PRs that sit on the
    same base commit and rebuilt when the commit differs.
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
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Body only. DockerfileEnhancer supplies the rest:
        #   - prepends the syntax directive and, after FROM, the infrastructure
        #     block (TARGETARCH/REPO_URL/BASE_COMMIT + proxy ARGs, the single ENV
        #     block, LABEL, CA-cert symlinks);
        #   - _standardize_repo_fetch() rewrites the `RUN git clone <url>
        #     /home/scapy` line below into the full fetch sequence: clone via
        #     "${REPO_URL}", WORKDIR /home/scapy, `git reset --hard`,
        #     `git checkout ${BASE_COMMIT}`, the history-hardening block, the
        #     submodule pass, and CMD ["/bin/bash"].
        # So the checkout and hardening are NOT lost by overriding dockerfile();
        # they are still generated, just from the clone line rather than inline.
        #
        # The apt and pip steps sit BEFORE the clone line on purpose. Everything
        # after that line is replaced by the block above, so anything placed
        # after it would land past CMD and never run. Both steps are independent
        # of the checkout -- apt needs to precede the clone anyway (python:3.9-slim
        # ships no git), and pytest/tox/mock come from PyPI, not the source tree
        # (scapy is run in-place via `cd /home/scapy`, never `pip install -e .`).
        #
        # Package list = the generic default set, so dropping the inherited
        # dockerfile() costs nothing, plus libpcap-dev / samba-common-bin / tshark
        # for the pcap, SMB and wireshark campaigns. `mock` is the critical pip
        # one: test/answering_machines.uts imports it and is the alphabetically
        # first entry of the `test/*.uts` glob, so without it those 6 tests died
        # on ModuleNotFoundError and aborted the run before test/contrib/ (see the
        # -c/-b ordering note on run.sh). With mock present: PASSED=10 FAILED=0.
        base_img = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        return f"""FROM {base_img}

{self.global_env}

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
    libpcap-dev \\
    samba-common-bin \\
    tshark \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel
RUN python -m pip install --no-cache-dir pytest tox mock

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

{self.clear_env}
"""


class ImageDefault(Image):
    """Per-PR stage: inherits the environment, adds the patches and run scripts."""

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

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
            # check_git_changes.sh -- the integrity guard prepare.sh calls.
            # Asserts we are inside a git work tree and that the tree is pristine,
            # so a graded run can never start from a dirty/patched checkout.
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -euo pipefail
cd /home/[[REPO_NAME]]
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "Error: /home/[[REPO_NAME]] is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "Error: working tree is not clean" >&2
    git status --porcelain >&2
    exit 1
fi
echo "check_git_changes: clean"
""".replace("[[REPO_NAME]]", repo_name),
            ),
            # prepare.sh -- runs ONCE at PR-image build time (see the RUN in dockerfile()).
            #
            # This file used to ship the raw authoring-session transcript (`ls -la`,
            # `pip install ...`, separated by literal ###ACTION_DELIMITER### lines). It was
            # COPY'd but never executed, so it was dead weight -- and a footgun, because
            # running it would have executed those delimiters as commands and re-run apt,
            # pip and the whole test suite at build time. The environment steps it recorded
            # now live where they belong: ImageBase.dockerfile() above.
            #
            # What it must do instead (QC P5): cd into the repo, reset, assert clean,
            # pin to BASE_COMMIT, assert clean again. Warm-cache steps are optional and
            # deliberately omitted -- scapy runs from the source tree with no build step.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail
cd /home/[[REPO_NAME]]
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_COMMIT]]
bash /home/check_git_changes.sh
echo "prepare: pinned to [[BASE_COMMIT]]"
""".replace("[[REPO_NAME]]", repo_name).replace("[[BASE_COMMIT]]", self.pr.base.sha),
            ),
            # NOTE on the `-c ... -b` argument order in the three run scripts
            # below (it used to be `-b -c ...`, which was a silent no-op):
            #
            # UTscapy parses options with getopt, in command-line order.
            #   `-b` sets BREAKFAILED = False        (UTscapy.py:1010)
            #   `-c` sets BREAKFAILED = data.breakfailed  (UTscapy.py:1041)
            # test/configs/linux.utsc contains `"breakfailed": true`, so passing
            # `-b` BEFORE `-c` let the config overwrite it straight back to True.
            # UTscapy.py:1204 then does `if BREAKFAILED: break`, aborting the run
            # at the first failing campaign -- 2 of 157 campaigns executed, and
            # test/contrib/rtcp.uts (the file this PR's test_patch adds) was never
            # reached in ANY phase. All three phase logs came out byte-identical,
            # so no test transitioned FAIL->PASS and f2p_tests was empty.
            #
            # `-b` must come AFTER `-c` to survive. Note also that UTscapy prints
            # "All campaigns executed" unconditionally after the loop, so a
            # broken-out run still looks complete in the log.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
python -m scapy.tools.UTscapy -d -c ./test/configs/linux.utsc -b -N

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m scapy.tools.UTscapy -d -c ./test/configs/linux.utsc -b -N

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
python -m scapy.tools.UTscapy -d -c ./test/configs/linux.utsc -b -N

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        # Thin per-PR layer on top of ImageBase. Everything environmental (apt
        # packages, pip deps, clone, BASE_COMMIT checkout, history hardening)
        # now happens once in the base stage, so this only needs to carry the
        # patches and run scripts in.
        base = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # `RUN bash /home/prepare.sh` runs exactly once, as the last build step (QC P4).
        # It re-asserts the pristine BASE_COMMIT checkout at PR-build time. The base image
        # already checked that commit out, so this is belt-and-braces -- but it is what
        # guarantees every graded run starts from an identical clean tree instead of
        # relying on each phase happening to get a fresh container. Note that run.sh /
        # test-run.sh / fix-run.sh each `git apply` WITHOUT resetting first, so without
        # this pin a re-used container would stack a patch on an already-patched tree.
        return f"""FROM {base.image_name()}:{base.image_tag()}

{self.global_env}

{copy_commands}
{self.clear_env}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


# Registered under BOTH the bare `secdev/scapy` key and the era key.
#
# Instance.create routes on `f"{org}/{number_interval}"` when number_interval is
# non-empty, else falls back to `f"{org}/{repo}"`. The SWE collector
# (multi_swe_bench/collect/build_dataset.py) writes the raw GitHub PR object and
# never emits number_interval, so every record in
# dataset/secdev__scapy_raw_dataset.jsonl arrives with number_interval == "" and
# resolves to the bare key `secdev/scapy`. Registering only the era key raised
#   ValueError: Instance 'secdev/scapy' is not registered.
#
# This is a single-era repo (one config file for secdev), so the bare key is
# claimed directly rather than via a dispatcher -- same shape as
# repos/python/PrefectHQ/fastmcp.py. The era key is kept so a dataset that DOES
# carry number_interval="scapy_3811_to_3810" still routes here.
@Instance.register("secdev", "scapy")
@Instance.register("secdev", "scapy_3811_to_3810")
class SCAPY_3811_TO_3810(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        # Parse UTscapy output into per-campaign-qualified test results.
        #
        # Test names are NOT unique across campaigns: many .uts files define
        # their own test called "Imports", and the ISOTP tests appear in both
        # test/contrib/isotp.uts and the automotive variants. A given name can
        # therefore legitimately fail in one campaign and pass in another. The
        # previous implementation collected bare names into one flat namespace,
        # so with the full 177-campaign suite running, passed_tests and
        # failed_tests overlapped by 13 names and TestResult.__post_init__ threw
        #   "Passed tests and failed tests should not have common items".
        # (It only appeared to work before because the -b/-c ordering bug meant
        # just 2 campaigns ever ran -- too few to collide.)
        #
        # Fix: track the current campaign from the "━ Loading: <file>" marker and
        # qualify every test as "<campaign file>::<test name>". The file path is
        # stable across the run/test/fix phases, so f2p/p2p diffing still works.
        #
        # We read the LIVE result lines ("passed <crc> <time>s <name>" /
        # "failed ..."), not the trailing "###(NNN)=[failed] <name>" summary:
        # the config sets "onlyfailed": true so the summary lists failures only,
        # and it is keyed by campaign *title* rather than file, which would not
        # line up with the live namespace.
        #
        # ANSI codes are stripped up front rather than baked into each pattern --
        # UTscapy wraps the status word in two separate escapes
        # ("\x1b[31m\x1b[1mfailed\x1b[0m"), which is easy to get subtly wrong.
        import re

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        loading_re = re.compile(r"^━ Loading: (\S+)")
        result_re = re.compile(r"^(passed|failed)\s+\w+\s+[\d.]+s\s+(.*?)\s*$")

        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # UTscapy filters via kw_ko; nothing is reported
        campaign = "<unknown>"

        for raw_line in log.splitlines():
            line = ansi_re.sub("", raw_line)

            match = loading_re.match(line)
            if match:
                campaign = match.group(1)
                continue

            match = result_re.match(line)
            if match:
                status, name = match.groups()
                qualified = f"{campaign}::{name}"
                if status == "passed":
                    passed_tests.add(qualified)
                else:
                    failed_tests.add(qualified)

        # A campaign can re-run a test (e.g. retries); if any run failed, the
        # test counts as failed. Guarantees the disjointness TestResult requires.
        passed_tests -= failed_tests

        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
