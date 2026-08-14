import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LibmodbusImageBase(Image):
    """Level 1: toolchain-only base image (SINGLE, shared across all PRs).

    ``dependency()`` returns a *string* (the Debian toolchain) and this
    Dockerfile carries NO ``# syntax`` directive, so ``DockerfileEnhancer``
    engages and prepends the ``# syntax``/ARG/ENV/LABEL/cert infra block.

    IMPORTANT: this image must NOT clone the repository. ``image_tag()`` is the
    constant ``base``, so exactly one base image is built for the whole repo --
    but ``build_dataset`` passes ``--build-arg BASE_COMMIT=<one PR's sha>`` to
    every string-dependency image, and the enhancer's ``_standardize_repo_fetch``
    rewrites any clone here into "checkout ${BASE_COMMIT} + strip every ref /
    remote / reflog + gc --prune=now". A shared image pinned and history-stripped
    at one PR's base commit makes ``git checkout <sha>`` impossible for every
    other PR sharing it (the objects are gone), so the clone lives in
    ``LibmodbusImageDefault`` instead -- per PR, and left verbatim by the
    enhancer because that image's ``dependency()`` is an ``Image``.

    debian:bookworm (GCC 12), not debian:latest: this dataset spans libmodbus
    v3.1.4 through v3.1.11. The legacy C in the older releases only warns under
    GCC 12 but is a hard error under GCC 14 (where
    ``-Werror=implicit-function-declaration`` is the default), so the oldest
    instances would not compile at baseline on a newer toolchain.
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
        return "debian:bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # libmodbus is an autotools/C project. build-essential, git, make,
        # python3, curl, wget, ca-certificates are already in the default
        # package set below; psmisc supplies `killall` for reaping the
        # background unit-test-server.
        return [
            "autoconf",
            "automake",
            "libtool",
            "pkg-config",
            "psmisc",
        ]

    def dockerfile(self) -> str:
        base_img = self.dependency()

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

        # No `git clone` here on purpose (see the class docstring) and no
        # `# syntax` directive, so DockerfileEnhancer injects the ARG/ENV/LABEL
        # infra block but no clone/hardening rewrite (there is no clone to
        # rewrite). LC_ALL is pinned alongside LANG so autotools' generated
        # shell scripts sort/collate deterministically across builds.
        return f"""FROM {base_img}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

{apt_command}

CMD ["/bin/bash"]
"""


class LibmodbusImageDefault(Image):
    """Level 2: per-PR image -- clone, pin to ``base.sha``, then harden.

    ``dependency()`` returns an ``Image``, so ``DockerfileEnhancer.enhance()``
    returns this Dockerfile verbatim: the clone, the ``${BASE_COMMIT}``
    checkout and the anti-reward-hacking hardening block must therefore be
    written out explicitly here. Pinning is correct at this level because the
    image is per-PR (tag ``pr-<number>``), not shared.
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
        return LibmodbusImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Shared build+test prelude, inlined into run/test/fix-run.sh.
        #
        # Deliberately NOT run under `set -e`: libmodbus's ASSERT_TRUE macro
        # aborts unit-test-client at the first failing assertion and the client
        # then exits non-zero. At the baseline stage that is the expected
        # outcome (it is exactly what makes a test F2P), so aborting the script
        # there would truncate the very log the harness parses and would leave
        # the background server unreaped.
        #
        # The server's stdout is redirected to its own file and replayed after
        # the client finishes, so server chatter ("New connection from ...")
        # can never interleave into the middle of a client result line -- the
        # client prints "<name>: " and the verdict lands on that same line, so
        # an interleaved write would corrupt parsing.
        build_and_test = """
set -o pipefail

cd /home/{pr.repo}

# autogen + configure are re-run every stage on purpose: test.patch may patch
# tests/Makefile.am, and regenerating is the only way that change takes effect.
./autogen.sh
./configure
make -j"$(nproc)"

cd /home/{pr.repo}/tests
killall -q unit-test-server 2>/dev/null || true
./unit-test-server > /tmp/unit-test-server.log 2>&1 &
server_pid=$!
sleep 1

# The client's stderr is split off from its stdout. libmodbus logs its
# diagnostics with fprintf(stderr, ...) -- unbuffered -- while the test names go
# to stdout, which is block-buffered when it is a pipe. Merged into one stream
# an stderr write lands MID-LINE, even mid-word, and corrupts the test name:
#   1/2 Too small byte timeout (3ms < 5mMessage length not corresponding ...
#   s): OK
# parse_log then cannot match that test, it is scored NONE for the stage, and a
# pre-existing test gets misfiled as N2P. Splitting the streams keeps every
# verdict line intact; the diagnostics are replayed below, after the results.
timeout 600 ./unit-test-client 2> /tmp/unit-test-client.err
rc=$?

kill "$server_pid" 2>/dev/null || true
killall -q unit-test-server 2>/dev/null || true

echo ""
echo ">>>>> unit-test-client stderr"
cat /tmp/unit-test-client.err 2>/dev/null || true
echo ">>>>> end unit-test-client stderr"

echo ""
echo ">>>>> unit-test-server output"
cat /tmp/unit-test-server.log 2>/dev/null || true
echo ">>>>> end unit-test-server output"

exit $rc
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

# The clone and the `git checkout ${{BASE_COMMIT}}` happen inline in the
# Dockerfile (before this script runs) so the hardening block that follows can
# assert HEAD == BASE_COMMIT. prepare.sh only proves the tree is pristine and
# warms the build so the run/test/fix stages start from a compiled tree.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

./autogen.sh || true
./configure || true
make -j$(nproc) || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{build_and_test}""".format(build_and_test=build_and_test.format(pr=self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{build_and_test}""".format(
                    pr=self.pr, build_and_test=build_and_test.format(pr=self.pr)
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_and_test}""".format(
                    pr=self.pr, build_and_test=build_and_test.format(pr=self.pr)
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared toolchain base does not clone, so this per-PR image clones
        # full history and checks out ${BASE_COMMIT} itself. BASE_COMMIT is an
        # ARG defaulted to this PR's base sha (build_dataset only passes it as a
        # build arg to string-dependency images) and promoted to ENV so the
        # hardening block below can reference it.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, drop origin, delete every ref, expire the
        # reflogs, gc/repack --prune=now, drop alternates, then assert all of
        # it; finally strip submodules the same way). Concatenated raw rather
        # than through an f-string so its ${BASE_COMMIT} and %(refname) tokens
        # stay literal. It runs LAST so nothing after it can reintroduce the
        # post-fix history an agent could read the gold fix out of.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


# Tests libmodbus itself declares non-deterministic. `1us response timeout` asks
# for a 1-microsecond response timeout and expects ETIMEDOUT; upstream does not
# assert on it but prints "FAILED (can fail on some platforms)"
# (tests/unit-test-client.c), because the result depends on scheduler and I/O
# latency rather than on the code under test. It is the only test in the suite
# using that soft-failure form.
#
# Observed flipping between two runs of the SAME commits on this machine:
#   run A: run=FAIL test=FAIL fix=PASS  -> scored as the instance's only F2P
#   run B: run=PASS test=NONE fix=FAIL  -> tripped the anomalous-pattern guard
# In run A it manufactured a reward the gold fix had not earned; in run B it
# invalidated an otherwise-good instance. Both directions are wrong, so it is
# dropped from every bucket.
#
# The drop MUST stay symmetric -- passes and failures alike. Dropping only the
# failures would silently convert a flaky failure into a pass and could
# manufacture F2P transitions, which is precisely the reward-hacking pattern the
# harness guards against elsewhere.
# `Adapted byte timeout (7ms > 5ms)` is the second one, found by re-running a
# recorded baseline: it failed once in the captured run.log and then passed 4/4
# on replay of the same image at the same commit. It asks for a read to SUCCEED
# within a 7ms byte timeout against a server that sleeps 5ms per byte -- a 2ms
# margin on a containerised scheduler. Host load pushes it toward failure, so
# the flake is one-directional.
#
# Its sibling `Too small byte timeout (3ms < 5ms)` shares the 2ms margin but is
# kept: it asserts the read TIMES OUT, so load only reinforces the expected
# outcome, and it is 10/10 stable across the captured logs. The 0.2s/0.6s vs
# 0.5s response-timeout pair is kept for the same reason -- a 100ms margin, 50x
# wider, and likewise 10/10 stable.
#
# That single flake did double damage in pr-398: it was credited as an F2P the
# gold fix had not earned, AND -- because ASSERT_TRUE does `goto close` -- it
# truncated the baseline at 49 tests instead of 70, so the 21 tests after it
# were never observed at baseline and were misfiled as N2P.
_FLAKY_TESTS = frozenset(
    {
        "1us response timeout",
        "Adapted byte timeout (7ms > 5ms)",
    }
)


@Instance.register("stephane", "libmodbus")
class Libmodbus(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LibmodbusImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # unit-test-client prints the test name with NO trailing newline, so the
        # verdict normally lands on that same line:
        #
        #   1/2 Too small byte timeout (3ms < 5ms): OK
        #   * modbus_send_raw_request: OK
        #   4/6 1us response timeout: FAILED (can fail on some platforms)
        #
        # ...but the client runs with modbus_set_debug(TRUE), so libmodbus's own
        # tracing can land between the name and the verdict, which then ends up
        # alone on a later line:
        #
        #   * modbus_receive_confirmation: Waiting for a confirmation...
        #   <00><00><00><00><00><0D><FF><03><0A>...
        #   OK
        #
        # And the ASSERT_TRUE macro's failure path (BUG_REPORT) opens with "\n",
        # so a hard failure always reports on its own line and then `goto close`s
        # out of the client:
        #
        #   * modbus_read_bits: Waiting for a confirmation...
        #   Line 234: assertion error for 'rc == 1': FAILED (0 != 1)
        #
        # Matching only ": OK"/": FAILED" would therefore drop every test whose
        # verdict was pushed onto a later line, silently turning real F2P
        # transitions into phantom N2P ones. Parse as a small state machine
        # instead: a name line opens a test, and it stays open until an "OK", an
        # assertion-error line, the next test, or end of output resolves it.
        # Unresolved at the end == failed (fail closed): ASSERT_TRUE aborts the
        # client, so an open test at EOF is exactly the one that blew up.
        #
        # `(.+)` is greedy on purpose -- test names contain colons of their own
        # ("* try function 0x1: read 0 values: ..."), so the verdict is whatever
        # follows the LAST ": " on the line.
        re_test_line = re.compile(r"^(?:\d+/\d+|\*)\s+(.+):\s*(.*?)\s*$")
        re_pass_only = re.compile(r"^(?:\d+/\d+|\*)\s+(.+?):\s+OK$")
        re_assertion_error = re.compile(r"^Line \d+: assertion error\b")
        # Everything the run scripts replay after the client's own stdout --
        # the client's stderr, then the server's output -- is introduced by a
        # ">>>>>" delimiter. Those sections are diagnostics, never verdicts, so
        # parsing stops at whichever delimiter comes first.
        section_marker = ">>>>>"

        pending: str | None = None

        for line in test_log.splitlines():
            line = line.strip()

            if line.startswith(section_marker):
                break

            if not line:
                continue

            m_test = re_test_line.match(line)
            if m_test:
                # An open test with no verdict of its own never got one.
                if pending is not None:
                    failed_tests.add(pending)
                    pending = None

                # Prefer the anchored ": OK" split, so a name that itself ends
                # in a colon-separated clause is not truncated by the greedy one.
                m_pass = re_pass_only.match(line)
                if m_pass:
                    passed_tests.add(m_pass.group(1))
                    continue

                name, verdict = m_test.group(1), m_test.group(2)
                if verdict.startswith("FAILED"):
                    failed_tests.add(name)
                else:
                    pending = name
                continue

            if pending is None:
                continue

            if line == "OK":
                passed_tests.add(pending)
                pending = None
            elif re_assertion_error.match(line) or line.startswith("FAILED"):
                failed_tests.add(pending)
                pending = None

        if pending is not None:
            failed_tests.add(pending)

        # Deduplication: worst-result-wins
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        # Symmetric removal of the self-declared-flaky tests (see _FLAKY_TESTS).
        passed_tests -= _FLAKY_TESTS
        failed_tests -= _FLAKY_TESTS
        skipped_tests -= _FLAKY_TESTS

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval
# ---------------------------------------------------------------------------
# A bundle's interval enumerates EVERY PR it contains, hyphen-joined:
#
#     prs_in_bundle = [146, 147, 150, 155, 157]
#     number_interval = "146-147-150-155-157"
#
# It is NEVER collapsed to a range like "146-157": that would silently claim the
# nine PRs in between, which are not in the bundle. The canonical producer,
# multi_swe_bench/collect/build_lht_dataset.py, emits it the same way, and also
# fixes `number` to the LOWEST bundled PR (`sorted(pr_numbers)[0]`).
#
# Two independent paths need covering, so both are handled below.
#
# 1. DERIVING the interval for the resolved JSONL. `Dataset.build()` copies
#    `pr.number_interval` verbatim, so a raw record that only carries
#    `prs_in_bundle` would emit an empty field. The patches below stash
#    `prs_in_bundle` off the raw JSON at `PullRequest.from_json()` time and fill
#    `number_interval` in at `Dataset.build()` time. `pr.number_interval` itself
#    stays "" so `Instance.create()` keeps routing on "stephane/libmodbus".
#
#    Registries that already need this (wasmtime, emscripten, serverless) each
#    install their own sentinel-guarded copy; libmodbus does the same rather
#    than relying on one of those being imported first, since these are global
#    monkey-patches and the import order across `repos/**` is incidental.
#
# 2. ROUTING when a raw record carries `number_interval` directly, which the
#    four stephane__libmodbus records now do. `Instance.create()` then routes on
#    f"{org}/{number_interval}", so every such interval must also be registered
#    or it raises "Instance 'stephane/<interval>' is not registered" before any
#    image is built.
#
# Each bundle is every PR merged in that record's release span (base.sha is the
# span's LOWER tag). libmodbus rebases/cherry-picks most contributions, so
# GitHub marks only 52 of its 199 closed PRs as "merged" and merge-SHA matching
# under-counts; these were reconstructed instead from GitHub's own commit->PR
# association for every commit in the span, which survives squash and rebase
# merges. Each record's own `number` falls inside its bundle.
_NUMBER_INTERVALS: list[str] = [
    "509-653-669",  # v3.1.8..v3.1.9
    "441-460-489-539-545-569-580-619",  # v3.1.6..v3.1.7
    "398-457-682-694-702-765",  # v3.1.10..v3.1.11
    "362-363-370-386-389-395-404-434-436",  # v3.1.4..v3.1.5
]

for _interval in _NUMBER_INTERVALS:
    Instance.register("stephane", _interval)(Libmodbus)


import json as _json
import logging as _logging

from multi_swe_bench.harness.dataset import Dataset as _Dataset


def _libmodbus_number_interval(bundle) -> str:
    # Explicit members in the bundle's own order, de-duplicated, dash-joined.
    # Range collapsing is intentionally avoided.
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


# Sentinels are checked against each class's OWN __dict__, not via getattr:
# Dataset subclasses PullRequest, so getattr would see the inherited from_json
# sentinel and skip Dataset's own patch.
if "_libmodbus_from_json_patch" not in PullRequest.__dict__:
    _orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _pr_from_json_with_bundle(cls, json_str):
        pr = _orig_pr_from_json(cls, json_str)
        try:
            bundle = _json.loads(json_str).get("prs_in_bundle")
            if bundle:
                # Stash only; number_interval stays "" so routing is unaffected.
                pr._prs_in_bundle = list(bundle)
        except (ValueError, TypeError, AttributeError):
            # Malformed JSON, a non-object record, or a non-iterable bundle:
            # the interval is metadata, so degrade to "" rather than failing the
            # whole load. Logged so a silently-missing interval is diagnosable.
            _logging.getLogger(__name__).debug(
                "prs_in_bundle lookup failed for %s/%s#%s; number_interval will be empty",
                getattr(pr, "org", "?"),
                getattr(pr, "repo", "?"),
                getattr(pr, "number", "?"),
                exc_info=True,
            )
        return pr

    PullRequest.from_json = _pr_from_json_with_bundle
    PullRequest._libmodbus_from_json_patch = True


if "_libmodbus_build_patch" not in _Dataset.__dict__:
    _orig_dataset_build = _Dataset.build.__func__

    @classmethod
    def _dataset_build_with_interval(cls, pr, report):
        ds = _orig_dataset_build(cls, pr, report)
        if not getattr(ds, "number_interval", ""):
            bundle = getattr(pr, "_prs_in_bundle", None)
            if bundle:
                ds.number_interval = _libmodbus_number_interval(bundle)
        return ds

    _Dataset.build = _dataset_build_with_interval
    _Dataset._libmodbus_build_patch = True
