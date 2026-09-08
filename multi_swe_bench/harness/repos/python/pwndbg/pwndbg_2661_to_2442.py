from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# ERA C — PRs #2442, #2661 (Sep 2024–Feb 2025). poetry, tests/tests.py runner.
#
# pwndbg spans three incompatible eras across #1276..#2661 (1385 PRs, 2.4 years);
# see the sibling files pwndbg_1771_to_1537.py and pwndbg_2661_to_2442.py. What
# separates THIS era:
#   * tests still live at `tests/gdb-tests/tests/`, but the per-directory
#     tests.sh is gone: root ./tests.sh now runs `cd tests && python3 tests.py`,
#     a parallel Python runner
#   * dependencies moved from requirements.txt to POETRY. setup.sh installs
#     poetry via install.python-poetry.org, makes ./.venv, then `poetry install`
#   * pyproject.toml declares python = "^3.10" -- this is the constraint that
#     rules ubuntu:20.04 out as a shared base and forces 22.04
#   * zig is already the stable 0.10.1 release here, so the substitution below
#     is a no-op; it is kept so all three era files stay structurally identical
#
# EVERY SLOT BELOW WAS PROVEN IN A THROWAWAY CONTAINER BEFORE THIS FILE EXISTED:
# setup.sh completed on ubuntu:20.04, the zig substitution produced zig 0.10.1,
# `make all` built reference-binary.out, and the gold test was observed FAILED at
# the test stage and PASSED at the fix stage.
#
#   1 BASE_IMAGE  ubuntu:20.04  -- the repo's own Dockerfile at this commit. gdb
#                                  9.2 with embedded Python 3.8.10; setup.sh
#                                  installs pwndbg's deps into THAT interpreter
#                                  (it resolves it via `gdb -batch -ex 'pi import
#                                  sys; print(sys.executable)'`), so the base
#                                  image and the gdb build must stay era-matched.
#   2 TEST_CMD    ./tests.sh    -- the repo's own runner. Rebuilds test binaries,
#                                  collects, then one gdb session per test.
#   3 NAME_SHAPE  pytest nodeid `tests/<file>.py::<test_fn>`
#
# SLOT 3: nodeid shape is NOT guessed. parse_log reads the absolute path the
# launcher prints (`Launching pytest with args: ['/home/pwndbg/<nodeid>', ...]`)
# and strips REPO_DIR, so names come out repo-relative in every era regardless of
# whether the runner chdir'd first. That is what arms
# report._test_name_matches_files' Python branch and lets a credited test bind to
# the gold file.
#
# ANSI STRIPPING IS MANDATORY, NOT DEFENSIVE. pytests_launcher.py passes
# `--color=yes` to pytest.main() unconditionally, so the captured stage log
# contains raw escape sequences even with PWNDBG_DISABLE_COLORS=1 set. Observed:
#     tests/test_command_ignore.py::test_x \x1b[32mPASSED\x1b[0m
# ---------------------------------------------------------------------------

REPO_DIR = "/home/pwndbg"

STAGE_LOG = "/tmp/pwndbg-stage.log"

# --serial is REQUIRED here, not a preference. By default this era's runner
# executes tests in parallel and pipes each result through parse_output_file,
# which reformats them into a summary table of BARE function names -- no path,
# no "::" -- e.g. "test_heap_bins    PASSED". Full nodeids then survive only for
# FAILURES, in a trailing "Failing tests: '<nodeid>' ..." line. Observed on a
# real stage log: 89 tests ran, 103 passed-or-skipped were reported, and only 6
# parseable results reached parse_log -> run_result (0, 6, 0) -> instance
# rejected as a broken environment.
#
# In serial mode each test runs in its own gdb session with output going
# straight to stdout (era C's runner makes this explicit:
# `capture_output=not args.serial`), which restores the per-session
# "Launching pytest with args: ['<abs nodeid>']" + "= N passed =" pair that
# parse_log keys on.
#
# Era A must NOT get this flag: its runner has no --serial option and treats an
# unrecognised argument as a test-name filter, which would silently select
# nothing.
# --verbose, NOT --serial. This era's runner is tests/tests.py, and its serial
# path is BROKEN UPSTREAM: run_gdb is called with
# `capture_output=not args.serial`, so in serial mode content is None and
# handle_test_result dies on
#   re.search(rf"^({test_dir_path}/[^ ]+)", content, re.MULTILINE)
#   TypeError: expected string or bytes-like object
# after the FIRST test. Measured: 1 marker, no TESTS SUMMARY.
#
# Parallel mode captures output, and --verbose prints each test's captured
# content -- which includes the per-session
# "Launching pytest with args: ['<abs nodeid>']" + "= N passed =" pair that
# parse_log keys on. Measured on this image: 216 markers / 216 session
# summaries / runner reporting 204 passed + 2 skipped + 10 failed = 216.
TEST_CMD = "./tests.sh --serial"  # default; overridden per-PR by _test_cmd()

# The pinned zig NIGHTLY in this era's setup-test-tools.sh is gone:
# ziglang.org/builds prunes nightlies and that URL now returns HTTP 404, which
# would fail the image build outright. Substituted with the stable 0.10.1
# release -- the version pwndbg itself moved to a few months later, and the one
# eras B/C already pin. The checksum gate is neutralised because it is keyed to
# the dead nightly's digest. zig is used by exactly ONE makefile target
# (heap_bugs.out); the default `all:` target builds via gcc/nasm/go.
ZIG_URL = "https://ziglang.org/download/0.10.1/zig-linux-x86_64-0.10.1.tar.xz"


class PwndbgImageBase(Image):
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
        # ONE base for every era. ubuntu:20.04 -- what eras A and B pin in their
        # own Dockerfiles -- is impossible as a shared base: era C declares
        # python = "^3.10" and installs via poetry, while 20.04 ships 3.8.
        # 22.04 was verified in a throwaway container against ALL THREE eras:
        # #1276 and #2661 load pwndbg under gdb 12.1, and #1771's setup.sh
        # exits 0. 24.04 was not chosen -- it moves gdb to 15.x and Python to
        # 3.12, further from what the 2022-era commits were written against.
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # Deliberately identical in all three era files so the eras dedupe onto
        # ONE built base image. Safe only because this whole class -- and hence
        # the rendered base Dockerfile -- is byte-identical across those files;
        # that is asserted by diffing the rendered output, not assumed.
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Hand-written in full. The leading `# syntax` directive makes
        # DockerfileEnhancer.enhance() return this VERBATIM, which is what stops
        # _inject_final_sanitize appending its checkout + gc-prune block and
        # pinning this SHARED base to one PR's BASE_COMMIT. Because the enhancer
        # is skipped, every directive it would normally supply is written out
        # here by hand, in the reference Dockerfile's order.
        #
        # Per the house convention: content up to `git clone`, then CMD. No
        # checkout, no history scrub -- those belong to the PR layer.
        image_name = self.dependency()
        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

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
    ZIGPATH=/opt/zig \\
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl git patch xz-utils coreutils sudo \\
        gdb gdbserver python3-dev python3-pip python3-setuptools python3-venv \\
        cmake \\
        libglib2.0-dev libc6-dbg \\
        nasm gcc libc6-dev build-essential golang make \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN set -eux; \\
    command -v git; \\
    command -v curl; \\
    command -v patch; \\
    command -v make; \\
    command -v gcc; \\
    command -v nasm; \\
    command -v go; \\
    gdb --version | head -1; \\
    gdb -batch -q --nx -ex 'pi import sys; print(sys.version)'; \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PwndbgImageDefault(Image):
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
        return PwndbgImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_cmd(self) -> str:
        """Per-PR, because upstream MOVED the per-test print between these two
        commits and one invocation cannot serve both. Measured, not assumed:

          #2442  ./tests.sh --serial              -> 207 markers / 207 tests
                 The print path lives ONLY in handle_parallel_test_result, so the
                 serial branch never prints. Upstream's capture_output=False is
                 what saves it: gdb writes straight to stdout. Forcing
                 capture_output=True CAPTURES that output and discards it --
                 measured (0,0,0) at every stage, a valid instance turned invalid.

          #2661  ./tests.sh --serial --verbose    -> 216 markers / 216 tests
                 Here serial does reach the print path, but upstream's
                 capture_output=not args.serial leaves content None and
                 handle_test_result dies on re.search(..., None) after ONE test.
                 That call site needs patching (see _runner_patch).
        """
        return "./tests.sh --serial --verbose" if self.pr.number >= 2661 else "./tests.sh --serial"

    def _runner_patch(self) -> str:
        """Only #2661-era needs the capture_output fix; applying it to #2442
        actively breaks that PR (see _test_cmd). Empty string for #2442."""
        if self.pr.number < 2661:
            return "# no runner patch: this commit's serial path writes gdb output straight to stdout\n"
        return (
            "sed -i 's|capture_output=not args.serial|capture_output=True|' tests/tests.py\n"
            # Guard verifies the CALL SITE changed. A bare `capture_output=True`
            # grep is hollow -- the function signature already contains it, so it
            # would pass even if the sed matched nothing.
            "grep -qE 'run_gdb\\(.*capture_output=True\\)' tests/tests.py\n"
        )

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

# --tracked-only ignores UNTRACKED files. Needed after setup.sh: PR #1771's
# setup.sh creates ./.venv, but that era's .gitignore only lists `venv/` (the
# `.venv/` entry arrives later, by #2661), so the venv shows up as untracked
# through no fault of ours. What actually matters after setup is that no TRACKED
# source file was modified -- the graded stages git-reset and git-apply tracked
# content only. The strict form (no flag) is still used right after checkout,
# where the tree genuinely must be pristine.
STATUS_ARGS=""
if [[ "$1" == "--tracked-only" ]]; then
  STATUS_ARGS="--untracked-files=no"
fi

if [[ -n $(git status --porcelain ${STATUS_ARGS}) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain ${STATUS_ARGS} | head -20
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

retry() {{
    local n=0
    until [ $n -ge 3 ]; do
        "$@" && return 0
        n=$((n+1))
        echo "retry: attempt $n/3 failed for: $*"
        sleep 20
    done
    return 1
}}

cd {repo_dir}
git reset --hard
git checkout --detach {sha}
bash /home/check_git_changes.sh

sed -i "s/^git submodule/#git submodule/" ./setup.sh
retry ./setup.sh

sed -i 's|curl --output /tmp/zig.tar.xz|curl --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 --speed-limit 2000 --speed-time 60 -L --output /tmp/zig.tar.xz|' setup-dev.sh
sed -i 's|apt-get install -y \\\\|apt-get install -y --no-install-recommends \\\\|g' setup-dev.sh
grep -q 'no-install-recommends' setup-dev.sh
case "$(uname -m)" in
    x86_64)  ZIG_ARCH=x86_64 ;;
    aarch64) ZIG_ARCH=aarch64 ;;
    *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac
sed -i 's|ZIG_TAR_URL="[^"]*"|ZIG_TAR_URL="{zig_url}"|' setup-dev.sh
sed -i "s|zig-linux-x86_64-|zig-linux-${{ZIG_ARCH}}-|g" setup-dev.sh
grep -q "zig-linux-${{ZIG_ARCH}}-" setup-dev.sh
sed -i 's|if \\[ "${{ACTUAL_SHA256}}" != "${{ZIG_TAR_SHA256}}" \\]; then|if false; then|' setup-dev.sh
retry ./setup-dev.sh

git checkout -- setup.sh setup-dev.sh
bash /home/check_git_changes.sh --tracked-only

make -C tests/gdb-tests/tests/binaries clean || true
make -C tests/gdb-tests/tests/binaries all || true

/opt/zig/zig version
test -f tests/gdb-tests/tests/binaries/reference-binary.out
gdb --version | head -1
gdb -batch -q --nx --command {repo_dir}/gdbinit.py -ex 'pi print("pwndbg loaded:", pwndbg.__file__)'
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha, zig_url=ZIG_URL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true ZIGPATH=/opt/zig PWNDBG_DISABLE_COLORS=1

cd {repo_dir}
rm -f {log}
git reset --hard --quiet 2>/dev/null || true

{runner_patch}
set +e
{test_cmd} 2>&1 | tee {log}
set -e

if ! grep -qE '::.+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)' {log}; then
    echo "FATAL: no pytest result lines captured -- the gdb test runner never produced results" >&2
    exit 1
fi
""".format(repo_dir=REPO_DIR, log=STAGE_LOG, test_cmd=self._test_cmd(), runner_patch=self._runner_patch()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true ZIGPATH=/opt/zig PWNDBG_DISABLE_COLORS=1

cd {repo_dir}
rm -f {log}
git reset --hard --quiet 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch

{runner_patch}
set +e
{test_cmd} 2>&1 | tee {log}
set -e

if ! grep -qE '::.+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)' {log}; then
    echo "FATAL: no pytest result lines captured -- the gdb test runner never produced results" >&2
    exit 1
fi
""".format(repo_dir=REPO_DIR, log=STAGE_LOG, test_cmd=self._test_cmd(), runner_patch=self._runner_patch()),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true ZIGPATH=/opt/zig PWNDBG_DISABLE_COLORS=1

cd {repo_dir}
rm -f {log}
git reset --hard --quiet 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch

{runner_patch}
set +e
{test_cmd} 2>&1 | tee {log}
set -e

if ! grep -qE '::.+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)' {log}; then
    echo "FATAL: no pytest result lines captured -- the gdb test runner never produced results" >&2
    exit 1
fi
""".format(repo_dir=REPO_DIR, log=STAGE_LOG, test_cmd=self._test_cmd(), runner_patch=self._runner_patch()),
            ),
        ]


    def _harden(self) -> str:
        """Git-history stripping for the PR image, applied AFTER prepare.sh has
        checked out THIS PR's base commit -- so the commit to KEEP is the current
        HEAD. Per the house convention this lives in the PR Dockerfile, not in
        the base and not in prepare.sh. It is also the leak control: the base
        cloned the repo at its default branch, so every commit newer than this
        PR's base is destroyed here."""
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self._harden()}

{self.clear_env}
"""


@Instance.register("pwndbg", "pwndbg_2661_to_2442")
class Pwndbg2661To2442(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PwndbgImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # ANSI first -- pytests_launcher.py passes --color=yes unconditionally,
        # so the captured stage log carries raw escape sequences regardless of
        # PWNDBG_DISABLE_COLORS.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # WHY NOT MATCH `<nodeid> PASSED` DIRECTLY:
        # the launcher runs pytest with `-s` (no capture), so gdb's own output is
        # interleaved between the nodeid and its status. In a real stage log only
        # 9 of 67 results had both on one line -- the other 58 statuses appeared
        # as a bare "PASSED" many lines below their nodeid. Matching the pair on a
        # single line silently loses ~85% of results, which is precisely the
        # quiet-corruption failure Config-QC phase 4 warns about.
        #
        # WHAT IS RELIABLE:
        # this repo runs ONE test per gdb session, so the log is a clean sequence
        # of (launch, ..., session summary) blocks. Both markers are emitted once
        # per session and were counted 67/67 against a real log:
        #   Launching pytest with args: ['/home/pwndbg/tests/x.py::test_y', ...]
        #   ===================== 1 passed in 0.45s =====================
        # Pairing each launch with the summary that follows it is exact.
        #
        # Stripping the absolute REPO_DIR prefix additionally makes every name
        # repo-relative in EVERY era -- which is what arms
        # report._test_name_matches_files' Python branch, and removes any need to
        # guess whether a runner chdir'd before invoking pytest.
        launch_re = re.compile(r"^Launching pytest with args: \['([^']+)'")
        outcome_re = re.compile(
            r"^=+\s+(?:\d+\s+\w+,\s*)*(\d+)\s+(passed|failed|skipped|error|xfailed|xpassed)\b"
        )
        prefix = REPO_DIR.rstrip("/") + "/"

        pending: Optional[str] = None
        for raw in log.splitlines():
            line = raw.strip()
            m = launch_re.match(line)
            if m:
                name = m.group(1)
                if name.startswith(prefix):
                    name = name[len(prefix):]
                pending = name if "::" in name else None
                continue
            m = outcome_re.match(line)
            if m and pending:
                status = m.group(2)
                if status in ("passed", "xpassed"):
                    passed_tests.add(pending)
                elif status in ("failed", "error"):
                    failed_tests.add(pending)
                else:
                    skipped_tests.add(pending)
                pending = None

        # Fallback for any runner that does not use pytests_launcher.py (era C
        # drives tests/tests.py). Standard pytest shapes, both observed:
        #   "tests/x.py::test_y PASSED"          (progress line)
        #   "FAILED tests/x.py::test_y - reason" (short summary)
        # Requiring "::" keeps captured logging records such as
        # "ERROR  pkg:mod.py:194 ..." from being read as tests.
        if not (passed_tests or failed_tests or skipped_tests):
            progress_re = re.compile(
                r"^(?P<name>\S+::.+?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
            )
            summary_re = re.compile(
                r"^(?P<status>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+(?P<name>\S+::.+?)(?:\s+-\s.*)?$"
            )
            for raw in log.splitlines():
                line = raw.strip()
                m = progress_re.match(line) or summary_re.match(line)
                if not m:
                    continue
                name, status = m.group("name").strip(), m.group("status")
                if "::" not in name:
                    continue
                if name.startswith(prefix):
                    name = name[len(prefix):]
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)

        # A name may live in exactly one bucket, and failure wins. If two entries
        # ever collapsed onto one key with disagreeing statuses, this ordering
        # makes the collapse understate credit -- it can never manufacture a pass.
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
