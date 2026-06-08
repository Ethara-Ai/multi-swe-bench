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
            # Use ${REPO_URL} (provided by DockerfileEnhancer as ARG) so the
            # Path B regex in _standardize_repo_fetch SKIPS this line — base
            # image has no BASE_COMMIT and must not be rewritten to a template
            # containing `RUN git checkout ${BASE_COMMIT}`. Per-PR hardening
            # happens in ImageDefault. See ANTI_CHEAT_HARDENING.md.
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
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

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{self.clear_env}

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

        # BASE_COMMIT as ENV so Image._HARDENING_BLOCK (which references
        # ${BASE_COMMIT}) resolves to this PR's base SHA. Hardening RUN runs
        # AFTER prepare.sh so the repo is already detached on BASE_COMMIT,
        # then we delete every ref, GC unreachable objects, and self-audit.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
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
        re_running_test = re.compile(r"^Running test '([^']+)'")
        re_unit_test_fail = re.compile(r"^UNIT TEST\((\d+),\s*(\d+)\)\s*FAILED")
        re_make_check = re.compile(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):\s+(\S+)")
        re_cmocka_ok = re.compile(r"^\[\s*OK\s*\]\s+(\S+)")
        re_cmocka_fail = re.compile(r"^\[\s*FAILED\s*\]\s+(\S+)")
        re_unittest_fn = re.compile(r"^(\w+(?:_unittest|_test|_tests))\(\)(?::|\s)\s*(.*)")
        re_slug_chars = re.compile(r"\W+")
        pass_verbs = ("passed", "ok", "done", "completed", "success")
        fail_verbs = ("failed", "fail", "error")

        def slug(text, maxlen=80):
            return re_slug_chars.sub("_", text.lower()).strip("_")[:maxlen]

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_name = None
        current_tag = None
        current_failed = False
        awaiting_description = False
        suite_completed = False

        def close_current(failed_override=None):
            nonlocal current_name, current_tag, current_failed, awaiting_description
            if not current_tag:
                return
            failed = current_failed if failed_override is None else failed_override
            (failed_tests if failed else passed_tests).add(current_tag)
            current_name = None
            current_tag = None
            current_failed = False
            awaiting_description = False

        for line in test_log.splitlines():
            stripped = line.strip()

            # FIX 1: consume description line BEFORE empty-line skip so an
            # empty description (rare) still clears the flag.
            if awaiting_description:
                awaiting_description = False
                if stripped:
                    current_tag = f"{current_name}_{slug(stripped)}"
                continue

            if not stripped:
                continue

            m = re_running_test.match(stripped)
            if m:
                close_current()
                current_name = slug(m.group(1))
                current_tag = current_name
                current_failed = False
                awaiting_description = True
                continue

            if current_tag and "### E R R O R ###" in stripped:
                current_failed = True
                continue

            if "SQLite is OK" in stripped:
                passed_tests.add("test_sqlite")
                continue
            if "Failed to test SQLite" in stripped:
                failed_tests.add("test_sqlite")
                continue

            m = re_unit_test_fail.match(stripped)
            if m:
                failed_tests.add(f"unit_test_{m.group(1)}_{m.group(2)}")
                continue

            m = re_make_check.match(stripped)
            if m:
                verdict, name = m.group(1), m.group(2)
                tag = f"check_{slug(name)}"
                if verdict in ("PASS", "XFAIL"):
                    passed_tests.add(tag)
                elif verdict in ("FAIL", "XPASS"):
                    failed_tests.add(tag)
                else:
                    skipped_tests.add(tag)
                continue

            m = re_cmocka_ok.match(stripped)
            if m:
                passed_tests.add(f"cmocka_{m.group(1)}")
                continue
            m = re_cmocka_fail.match(stripped)
            if m:
                failed_tests.add(f"cmocka_{m.group(1)}")
                continue

            # FIX 2: tightened to require _unittest|_test|_tests suffix AND
            # verdict prefix (not substring) — guards against false positives
            # from arbitrary `func(): error` lines in C traces.
            m = re_unittest_fn.match(stripped)
            if m:
                name = m.group(1)
                rest = (m.group(2) or "").lstrip().lower()
                if rest.startswith(pass_verbs):
                    passed_tests.add(name)
                    continue
                if rest.startswith(fail_verbs):
                    failed_tests.add(name)
                    continue

            if "all tests passed" in stripped.lower():
                close_current()
                passed_tests.add("netdata_unittest_suite")
                suite_completed = True
                continue

        # FIX 3: only override-to-failed when suite did NOT complete; if
        # ALL TESTS PASSED already fired, the open subtest is part of a
        # passing suite (log truncated after suite completion).
        if current_tag:
            close_current(failed_override=False if suite_completed else True)

        # FIX 4: failure dominates over passed; passed dominates over skipped.
        # Previous `passed -= common; failed -= common` silently dropped
        # tests that appeared in both sets.
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
