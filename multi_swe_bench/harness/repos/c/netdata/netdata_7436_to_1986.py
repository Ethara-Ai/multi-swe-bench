import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# Pre-v1.20 registry: netdata PR #1986–#7436 (autotools era)
#
# These old versions predate the -W unittest framework.  Build is
# verification-only via autogen.sh + configure + make.
#
# JSONL entries must set  number_interval: "netdata_7436_to_1986"
# =============================================================================


class Netdata_7436_to_1986_ImageDefault(Image):
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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _build_commands(self) -> str:
        # --enable-cmocka builds the bundled CMocka tests under tests/ subdirs
        # so `make check` (in _test_command) actually finds tests to run.
        # Without it the suite is empty (TOTAL: 0) and no transitions surface.
        return (
            "timeout --kill-after=30 1800 autoreconf -if 2>&1\n"
            "timeout --kill-after=30 1800 ./configure --enable-cmocka 2>&1 "
            "|| timeout --kill-after=30 1800 ./configure 2>&1\n"
            "timeout --kill-after=30 3600 make -j$(nproc) 2>&1"
        )

    def _test_command(self) -> str:
        # Pre-v1.20 autotools build: `make` produces the binary in either
        # ./src/netdata (older pre-#3780 layout) or ./netdata (post-flat).
        # Pre-create /usr/local/var/lib/netdata/registry — the very-old
        # versions (≤ ~v1.10) FATAL on missing registry dir before unittests
        # complete. Two invocations: `-W unittest` for built-in C tests,
        # `make check` for any CMocka tests in tests/ subdirs. Wrapping
        # in `{ ...; } || true` keeps `set -e` in the parent script from
        # aborting before the exit-code marker lands.
        return (
            "{ mkdir -p /usr/local/var/lib/netdata/registry "
            "/usr/local/etc/netdata; "
            'if [ -x ./src/netdata ]; then bin=./src/netdata; '
            'elif [ -x ./netdata ]; then bin=./netdata; '
            'else bin=$(find . -maxdepth 3 -name netdata -type f -executable '
            '2>/dev/null | head -1); fi; '
            'echo "NETDATA_BINARY=$bin"; '
            'timeout --kill-after=30 1200 "$bin" -W unittest 2>&1; '
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
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
# Drop diff sections for binary files lacking full index lines.
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
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM ubuntu:20.04

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
# Suppress -Werror=deprecated-declarations for sub-builds (e.g. bundled
# libwebsockets) against newer OpenSSL.
ENV CFLAGS="-Wno-error=deprecated-declarations -Wno-deprecated-declarations"
ENV CXXFLAGS="-Wno-error=deprecated-declarations -Wno-deprecated-declarations"
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
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
    libmnl-dev \\
    zlib1g-dev \\
    libjson-c-dev \\
    libuv1-dev \\
    libssl-dev \\
    liblz4-dev \\
    libelf-dev \\
    libatomic1 \
    libjudy-dev \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# CC/GCC wrappers — same rationale as in netdata.py: ride on the compiler
# command line so `env CFLAGS=-fPIC ...` from netdata's bundled build scripts
# can't strip the deprecation flag.
RUN printf '#!/bin/sh\\nexec /usr/bin/gcc "$@" -Wno-error=deprecated-declarations\\n' > /usr/local/bin/gcc \\
 && printf '#!/bin/sh\\nexec /usr/bin/gcc "$@" -Wno-error=deprecated-declarations\\n' > /usr/local/bin/cc \\
 && chmod +x /usr/local/bin/gcc /usr/local/bin/cc

{code}

{self.clear_env}

{copy_commands}

{prepare_commands}

"""


@Instance.register("netdata", "netdata_7436_to_1986")
class Netdata_7436_to_1986(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Netdata_7436_to_1986_ImageDefault(self.pr, self._config)

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
        # Same parser as modern netdata.py — see comments there.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_test = None
        current_failed = False

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

            m = re.match(r"^Running test '([^']+)'", stripped)
            if m:
                close_current()
                current_test = re.sub(r"\W+", "_", m.group(1)).strip("_")
                current_failed = False
                continue

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

            m = re.match(r"^\[\s*OK\s*\]\s+(\S+)", stripped)
            if m:
                passed_tests.add(f"cmocka_{m.group(1)}")
                continue
            m = re.match(r"^\[\s*FAILED\s*\]\s+(\S+)", stripped)
            if m:
                failed_tests.add(f"cmocka_{m.group(1)}")
                continue

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
                continue

        if current_test:
            close_current(failed_override=True)

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

