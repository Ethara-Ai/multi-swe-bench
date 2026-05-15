import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# Default registry: netdata v1.20+ (PR #8000+) — modern era with -W unittest
#
# Build via: netdata-installer.sh (cmake + autotools compatible)
# Test via:  netdata -W unittest
# Base:      ubuntu:22.04
#
# PRs without a number_interval in the JSONL land here.
# =============================================================================


class ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
# CFLAGS env is wiped by netdata-installer.sh's `env CFLAGS=-fPIC ...`
# invocation when building bundled libwebsockets. Use a CC wrapper instead
# so the flag rides on the compiler command line, not the env.
ENV CFLAGS="-Wno-error=deprecated-declarations -Wno-deprecated-declarations"
ENV CXXFLAGS="-Wno-error=deprecated-declarations -Wno-deprecated-declarations"
RUN apt-get update && apt-get install -y --no-install-recommends \\
    bison \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    flex \\
    git \\
    gzip \\
    tar \\
    autoconf \\
    autoconf-archive \\
    automake \\
    autogen \\
    libtool \\
    pkg-config \\
    python3 \\
    uuid-dev \\
    libuv1-dev \\
    libjson-c-dev \\
    liblz4-dev \\
    libssl-dev \\
    libsystemd-dev \\
    libmnl-dev \\
    zlib1g-dev \\
    libyaml-dev \\
    libelf-dev \\
    libatomic1 \\
    libjudy-dev \\
    libcurl4-openssl-dev \\
    netcat \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# CC/GCC wrappers: appended -Wno-error=deprecated-declarations rides on the
# compiler invocation itself, so it survives netdata-installer.sh wiping the
# CFLAGS env when building bundled libwebsockets-3.2.2 (OpenSSL 3.0 EC_KEY_*).
RUN printf '#!/bin/sh\\nexec /usr/bin/gcc "$@" -Wno-error=deprecated-declarations\\n' > /usr/local/bin/gcc \\
 && printf '#!/bin/sh\\nexec /usr/bin/gcc "$@" -Wno-error=deprecated-declarations\\n' > /usr/local/bin/cc \\
 && chmod +x /usr/local/bin/gcc /usr/local/bin/cc

{code}

{self.clear_env}

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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _build_commands(self) -> str:
        return (
            "git submodule update --init --recursive\n"
            "./netdata-installer.sh --dont-wait --dont-start-it "
            "--disable-go 2>&1"
        )

    def _test_command(self) -> str:
        # Two test invocations:
        #   1. `netdata -W unittest` — built-in C unit tests (src/daemon/unit_test.c)
        #   2. `make check` — CMocka tests in exporting/tests, libnetdata/tests, etc.
        # Both wrapped in `{ ...; } || true` so non-zero exits don't trip
        # `set -e` in the parent run script before the exit-code echo lands.
        return (
            "{ timeout --kill-after=30 1200 netdata -W unittest 2>&1; "
            'echo "NETDATA_UNITTEST_EXIT_CODE=$?"; } || true\n'
            "{ timeout --kill-after=30 600 make check 2>&1; "
            'echo "NETDATA_MAKE_CHECK_EXIT_CODE=$?"; } || true'
        )

    def files(self) -> list[File]:
        build_cmds = self._build_commands()
        test_cmd = self._test_command()

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{build}
{test}
""".format(repo=self.pr.repo, build=build_cmds, test=test_cmd),
            ),
            File(
                ".",
                "strip_binaries.sh",
                r"""#!/bin/bash
# Drop diff sections for binary files. Such files in the dataset patches
# typically lack the full index line that `git apply` needs and break
# the whole apply. Images/fonts/archives never affect test outcomes.
awk '
BEGIN { skip = 0 }
/^diff --git / {
  skip = 0
  if ($0 ~ /\.(ico|icns|png|jpe?g|gif|bmp|webp|woff2?|ttf|eot|otf|pdf|zip|tgz|bz2|xz|class|jar|enc|gpg|asc|p7s|der|crt|key|pem|sig)( |$)/) skip = 1
}
{ if (!skip) print }
' "$1"
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
git apply --whitespace=nowarn /tmp/test.filtered.patch
{build}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
bash /home/strip_binaries.sh /home/fix.patch  > /tmp/fix.filtered.patch
git apply --whitespace=nowarn /tmp/test.filtered.patch /tmp/fix.filtered.patch
{build}
{test}

""".format(repo=self.pr.repo, build=build_cmds, test=test_cmd),
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


@Instance.register("netdata", "netdata")
class Netdata(Instance):
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
        # `netdata -W unittest` output is heterogeneous and not designed for
        # machine parsing. State-machine logic, derived from inspecting
        # src/daemon/unit_test.c:
        #
        # - `run_all_mockup_tests()` calls subtests in sequence, bailing on
        #   first failure. Each subtest emits "Running test 'NAME':" at start.
        #   It does NOT emit a bare OK/FAILED at the end — instead it prints
        #   inline "test1/dim1: ..., OK" or "..., ### E R R O R ###" for
        #   each assertion. A test passed iff the next `Running test '...':`
        #   line or the final `ALL TESTS PASSED` sentinel appears.
        # - `check_*()` helpers print inline "...: OK" or "...: FAILED" and
        #   bail on first error, so "any '### E R R O R ###' inside the
        #   subtest's lines" is the failure signal.
        # - `test_sqlite()` prints "Testing SQLIte" then "SQLite is OK" or
        #   "Failed to test SQLite: ...".
        # - `unit_test(delay, shift)` prints "UNIT TEST(N, M) FAILED" on fail.
        # - "ALL TESTS PASSED" is the bulk sentinel.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_test = None
        current_failed = False  # whether the active subtest hit any error
        suite_started = False
        suite_completed = False

        def close_current(failed_override=None):
            nonlocal current_test, current_failed
            if not current_test:
                return
            failed = current_failed if failed_override is None else failed_override
            tag = f"run_test_{current_test}"
            (failed_tests if failed else passed_tests).add(tag)
            current_test = None
            current_failed = False

        for line in test_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # New subtest starts → close previous (it passed since we reached here).
            m = re.match(r"^Running test '([^']+)'", stripped)
            if m:
                close_current()
                current_test = re.sub(r"\W+", "_", m.group(1)).strip("_")
                current_failed = False
                suite_started = True
                continue

            # Inline assertion failures inside a run_test() subtest
            if current_test and "### E R R O R ###" in stripped:
                current_failed = True
                continue

            if "SQLite is OK" in stripped:
                passed_tests.add("test_sqlite")
                continue
            if "Failed to test SQLite" in stripped:
                failed_tests.add("test_sqlite")
                continue

            m = re.match(r"^UNIT TEST\((\d+),\s*(\d+)\)\s*FAILED", stripped)
            if m:
                failed_tests.add(f"unit_test_{m.group(1)}_{m.group(2)}")
                continue

            # `make check` (autotools) emits PASS:/FAIL:/SKIP: lines per test
            m = re.match(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):\s+(\S+)", stripped)
            if m:
                verdict, name = m.group(1), m.group(2)
                tag = f"check_{re.sub(r'[^A-Za-z0-9_]+', '_', name)}"
                if verdict in ("PASS", "XFAIL"):
                    passed_tests.add(tag)
                elif verdict in ("FAIL", "XPASS"):
                    failed_tests.add(tag)
                else:
                    skipped_tests.add(tag)
                continue

            # CMocka inline reports
            m = re.match(r"^\[\s*OK\s*\]\s+(\S+)", stripped)
            if m:
                passed_tests.add(f"cmocka_{m.group(1)}")
                continue
            m = re.match(r"^\[\s*FAILED\s*\]\s+(\S+)", stripped)
            if m:
                failed_tests.add(f"cmocka_{m.group(1)}")
                continue

            # Modern *_unittest() helpers — self-announce on a single line
            m = re.match(r"^(\w+)\(\)(?::|\s)\s*(.*)", stripped)
            if m:
                name = m.group(1)
                rest = (m.group(2) or "").lower()
                if any(w in rest for w in ["passed", "completed", " ok", "done"]):
                    passed_tests.add(name)
                    continue
                if any(w in rest for w in ["fail", "error"]):
                    failed_tests.add(name)
                    continue

            if "all tests passed" in stripped.lower():
                close_current()
                passed_tests.add("netdata_unittest_suite")
                suite_completed = True
                continue

        # End of log: if a subtest was still active (timed out / killed before
        # completing), mark it failed.
        if current_test:
            close_current(failed_override=True)

        # If the suite started but never reached "ALL TESTS PASSED" and we
        # didn't otherwise mark anything failed, the suite was interrupted —
        # leave the per-subtest failed state as-is; the missing bulk sentinel
        # is implicit signal.
        _ = (suite_started, suite_completed)

        common = passed_tests & failed_tests
        passed_tests -= common
        failed_tests -= common

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
