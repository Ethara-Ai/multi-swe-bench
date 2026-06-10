import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "debian:bookworm"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV GITHUB_ACTIONS=true
ENV HOME=/root
ENV RUSTUP_HOME=/root/.rustup
ENV CARGO_HOME=/root/.cargo
ENV PATH=/root/.cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin

# Debian's deb.debian.org (Fastly CDN) is reliable on arm64/amd64; no mirror swap needed.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential cmake clang llvm-dev libclang-dev \\
    libssl-dev openssl autoconf automake libtool m4 pkg-config \\
    python3 python3-venv python3-pip python3-dev \\
    unzip wget curl ca-certificates rsync gdb lcov file jq \\
    libboost-all-dev coreutils procps xz-utils zlib1g-dev \\
    redis-tools tcl tclsh \\
    && rm -rf /var/lib/apt/lists/*

# RediSearch's module ABI is era-coupled to a specific Redis. Using one static
# `unstable` for every PR caused per-test redis-server processes to crash for
# older PRs (different RedisModule API symbols/behavior), which the harness
# saw as PASS->FAIL transitions and invalidated whole reports. We bake THREE
# Redis versions and setup.sh picks the right one per PR by reading
# `supportedVersion` from src/module.c at the checked-out base commit.
#   v2.x  / supportedVersion.major <= 7 -> Redis 7.4 (last stable 7.x line)
#   v8.0/v8.2 / supportedVersion = 7.1 or 8.0 -> Redis 8.0
#   v8.4+ / supportedVersion >= 8.3 -> Redis `unstable` (only branch that
#     reports >= 8.3.200; older tags fail RediSearch's own version check)
RUN set -e; mkdir -p /usr/local/include; for spec in "7.4.2:/usr/local/redis-7.4" "8.0.2:/usr/local/redis-8.0" "unstable:/usr/local/redis-unstable"; do \\
        tag="${{spec%%:*}}"; pfx="${{spec##*:}}"; \\
        echo "=== building redis $tag -> $pfx ==="; \\
        rm -rf /tmp/redis-src && \\
        git clone --quiet --depth 1 --branch "$tag" https://github.com/redis/redis.git /tmp/redis-src && \\
        make -C /tmp/redis-src -j"$(nproc)" BUILD_TLS=no >/dev/null && \\
        make -C /tmp/redis-src install PREFIX="$pfx" >/dev/null && \\
        "$pfx/bin/redis-server" --version && \\
        if [ "$tag" = "unstable" ]; then \\
            install -m 0644 /tmp/redis-src/src/redismodule.h /usr/local/include/redismodule-unstable.h; \\
        fi; \\
        rm -rf /tmp/redis-src; \\
    done && \\
    ln -sf /usr/local/redis-unstable/bin/redis-server /usr/local/bin/redis-server && \\
    ln -sf /usr/local/redis-unstable/bin/redis-cli    /usr/local/bin/redis-cli && \\
    test -f /usr/local/include/redismodule-unstable.h && head -1 /usr/local/include/redismodule-unstable.h

# Rust via rustup. RediSearch pins an exact toolchain via rust-toolchain.toml
# (varies per era/commit); rustup auto-installs the pinned toolchain on demand.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \\
    sh -s -- -y --default-toolchain stable --profile minimal \\
    && /root/.cargo/bin/rustup component add rustfmt clippy 2>/dev/null || true

# uv (used by modern eras to manage the Python test virtualenv / RLTest).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/root/.local/bin sh \\
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "setup.sh",
                """#!/bin/bash
# Best-effort, era-adaptive system/build dependency setup.
# Heavy deps (toolchains) are pre-baked in the base image; the repo's own
# installers below top up commit-specific requirements. Never fatal.
set +e

cd /home/{pr.repo}
export HOME=/root
export GITHUB_ACTIONS=true
export RUSTUP_HOME=/root/.rustup
export CARGO_HOME=/root/.cargo
export PATH=/root/.cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
. "$HOME/.cargo/env" 2>/dev/null || true

git submodule update --init --recursive >/dev/null 2>&1 || true

# === Era-aware Redis selection ============================================
# RediSearch's module ABI is coupled to a specific Redis. One static `unstable`
# crashes the per-test redis-server for older PRs and the harness then counts
# every subsequent test as PASS->FAIL (Rule 2 -> valid:false). We read the
# minimum Redis required by THIS commit from `supportedVersion` in src/module.c
# (present across all eras with the same struct format) and point runtests.sh
# at a matching Redis binary baked into the base image.
if [ -f src/module.c ]; then
  sv_maj=$(grep -A2 'supportedVersion *=' src/module.c | grep -oE 'majorVersion *= *[0-9]+' | grep -oE '[0-9]+' | head -1)
  sv_min=$(grep -A3 'supportedVersion *=' src/module.c | grep -oE 'minorVersion *= *[0-9]+' | grep -oE '[0-9]+' | head -1)
  : "${{sv_maj:=0}}" "${{sv_min:=0}}"
  if [ "$sv_maj" -ge 8 ] && [ "$sv_min" -ge 3 ]; then
    REDIS_PREFIX=/usr/local/redis-unstable
    export REJSON_BRANCH=master
  elif [ "$sv_maj" -ge 8 ]; then
    REDIS_PREFIX=/usr/local/redis-8.0
    export REJSON_BRANCH=master
  else
    REDIS_PREFIX=/usr/local/redis-7.4
    export REJSON_BRANCH=2.6
  fi
  if [ -x "$REDIS_PREFIX/bin/redis-server" ]; then
    export REDIS_SERVER="$REDIS_PREFIX/bin/redis-server"
    export PATH="$REDIS_PREFIX/bin:$PATH"
    echo "[setup] supportedVersion=$sv_maj.$sv_min -> REDIS_SERVER=$REDIS_SERVER  REJSON_BRANCH=$REJSON_BRANCH"
  else
    echo "[setup] WARN: $REDIS_PREFIX not present; falling back to default redis-server on PATH"
  fi
fi
# ===========================================================================

# System / build deps. install_script.sh (modern) installs ONLY system/build
# deps; sbin/setup and the readies system-setup.py variants also pip-install
# the python test deps. We run whichever exists, then install the python test
# deps explicitly below regardless (install_script.sh does NOT do that).
if [ -f .install/install_script.sh ]; then
  ( cd .install && bash -l -eo pipefail install_script.sh ) >/dev/null 2>&1 || true
elif [ -f sbin/setup ]; then
  ./sbin/setup >/dev/null 2>&1 || true
elif [ -f system-setup.py ]; then
  ./deps/readies/bin/getpy3 >/dev/null 2>&1 || true
  python3 ./system-setup.py >/dev/null 2>&1 || true
elif [ -f sbin/system-setup.py ]; then
  ./deps/readies/bin/getpy3 >/dev/null 2>&1 || true
  python3 ./sbin/system-setup.py >/dev/null 2>&1 || true
fi

# Rust test tooling (nextest/llvm-cov), modern eras only.
if [ -f .install/test_deps/install_rust_deps.sh ]; then
  bash -l -eo pipefail .install/test_deps/install_rust_deps.sh >/dev/null 2>&1 || true
fi

# Python test deps (RLTest, redis-py, numpy, scipy, deepdiff, gevent, ...).
# Era-agnostic and explicit, because for the install_script.sh era nothing
# else installs them and `make test`'s pytest phase needs `python3 -m RLTest`.
PIP="python3 -m pip"
$PIP --version >/dev/null 2>&1 || PIP="pip3"
if [ -f .install/test_deps/install_python_deps.sh ]; then
  # Modern era: creates .venv via uv and `uv sync` (run.sh activates .venv).
  bash -l -eo pipefail .install/test_deps/install_python_deps.sh >/dev/null 2>&1 || true
fi
[ -f .install/common_installations.sh ] && \
  bash -l -eo pipefail .install/common_installations.sh >/dev/null 2>&1 || true
if [ -f tests/pytests/pyproject.toml ]; then
  $PIP install --break-system-packages ./tests/pytests >/dev/null 2>&1 \
    || $PIP install ./tests/pytests >/dev/null 2>&1 || true
elif [ -f tests/pytests/requirements.txt ]; then
  $PIP install --break-system-packages -r tests/pytests/requirements.txt >/dev/null 2>&1 \
    || $PIP install -r tests/pytests/requirements.txt >/dev/null 2>&1 || true
elif [ -f tests/pytests/requirements.linux.txt ]; then
  $PIP install --break-system-packages -r tests/pytests/requirements.linux.txt >/dev/null 2>&1 \
    || $PIP install -r tests/pytests/requirements.linux.txt >/dev/null 2>&1 || true
elif [ -f src/pytest/requirements.txt ]; then
  $PIP install --break-system-packages -r src/pytest/requirements.txt >/dev/null 2>&1 \
    || $PIP install -r src/pytest/requirements.txt >/dev/null 2>&1 || true
fi
# Last-resort floor: ensure the test runner itself is importable.
python3 -c "import RLTest" >/dev/null 2>&1 || \
  $PIP install --break-system-packages "RLTest" redis >/dev/null 2>&1 || \
  $PIP install "RLTest" redis >/dev/null 2>&1 || true

exit 0

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail   # NOT -e: build/test failures must not abort before output is captured.

cd /home/{pr.repo}
export HOME=/root
export GITHUB_ACTIONS=true
export RUSTUP_HOME=/root/.rustup
export CARGO_HOME=/root/.cargo
export PATH=/root/.cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
. "$HOME/.cargo/env" 2>/dev/null || true

bash /home/setup.sh || true

# Activate the uv-managed venv AFTER setup.sh creates it, so `make test`'s
# RLTest invocation (`python3 -m RLTest`) resolves to the venv interpreter
# where RLTest is installed instead of the system python3.
[ -f .venv/bin/activate ] && . .venv/bin/activate || true

# Overlay redis-unstable's redismodule.h on top of RediSearch's vendored copy
# (see test-run.sh for rationale). Even on the baseline `run.sh` (no patches)
# we apply the same overlay so the build environment matches the test/fix
# stages — keeps run vs. test vs. fix comparable.
if [ -f /usr/local/include/redismodule-unstable.h ] && [ -f src/redismodule.h ]; then
  install -m 0644 /usr/local/include/redismodule-unstable.h src/redismodule.h
fi

# Hard ceiling on any individual test's runtime; runtests.sh honors this.
export TEST_TIMEOUT=300

# Build: modern eras use `make build`, older eras default `make`. TESTS=1 makes
# build.sh / the old CMake config compile the cpptests/ctests binaries (without
# it, sbin/unit-tests aborts because the cpptests dir doesn't exist).
make build IGNORE_MISSING_DEPS=1 TESTS=1 || make IGNORE_MISSING_DEPS=1 TESTS=1 || make TESTS=1 || true

# Test: run targets independently so one failing suite (e.g. cargo test compile
# failure when the test patch references symbols added by the fix patch) does
# NOT suppress the others. Fall back to bundled `make test` for eras without
# the split targets (v2.0/v2.4).
if make -n unit-tests >/dev/null 2>&1; then
  make unit-tests IGNORE_MISSING_DEPS=1 || true
  make pytest     IGNORE_MISSING_DEPS=1 || true
  make rust-tests IGNORE_MISSING_DEPS=1 2>/dev/null || true
else
  make test IGNORE_MISSING_DEPS=1 || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail   # NOT -e: build/test failures must not abort before output is captured.

cd /home/{pr.repo}
export HOME=/root
export GITHUB_ACTIONS=true
export RUSTUP_HOME=/root/.rustup
export CARGO_HOME=/root/.cargo
export PATH=/root/.cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
. "$HOME/.cargo/env" 2>/dev/null || true

git apply --whitespace=nowarn --exclude='*.pub' --exclude='*.lockb' --exclude='*.png' /home/test.patch || true

bash /home/setup.sh || true

# Activate the uv-managed venv AFTER setup.sh creates it (see run.sh).
[ -f .venv/bin/activate ] && . .venv/bin/activate || true

# Overlay redis-unstable's redismodule.h on top of RediSearch's vendored copy
# AFTER patches+submodules have settled. Fix patches in this dataset reference
# new redis-module symbols (REDISMODULE_CONFIG_UNPREFIXED, RedisModuleSlotRangeArray,
# REDISMODULE_OPEN_KEY_ACCESS_TRIMMED, ...) without adding them to src/redismodule.h.
# Redis-module API is additive, so this is safe for older RediSearch code too.
if [ -f /usr/local/include/redismodule-unstable.h ] && [ -f src/redismodule.h ]; then
  install -m 0644 /usr/local/include/redismodule-unstable.h src/redismodule.h
fi

# Hard ceiling on any individual test's runtime; runtests.sh honors this.
# Prevents the per-test-deadlock hangs we hit on a couple of PRs (e.g. pr-5843).
export TEST_TIMEOUT=300

make build IGNORE_MISSING_DEPS=1 TESTS=1 || make IGNORE_MISSING_DEPS=1 TESTS=1 || make TESTS=1 || true

if make -n unit-tests >/dev/null 2>&1; then
  make unit-tests IGNORE_MISSING_DEPS=1 || true
  make pytest     IGNORE_MISSING_DEPS=1 || true
  make rust-tests IGNORE_MISSING_DEPS=1 2>/dev/null || true
else
  make test IGNORE_MISSING_DEPS=1 || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail   # NOT -e: build/test failures must not abort before output is captured.

cd /home/{pr.repo}
export HOME=/root
export GITHUB_ACTIONS=true
export RUSTUP_HOME=/root/.rustup
export CARGO_HOME=/root/.cargo
export PATH=/root/.cargo/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
. "$HOME/.cargo/env" 2>/dev/null || true

git apply --whitespace=nowarn --exclude='*.pub' --exclude='*.lockb' --exclude='*.png' /home/test.patch /home/fix.patch || true

bash /home/setup.sh || true

# Activate the uv-managed venv AFTER setup.sh creates it (see run.sh).
[ -f .venv/bin/activate ] && . .venv/bin/activate || true

# Overlay redis-unstable's redismodule.h on top of RediSearch's vendored copy
# AFTER patches+submodules have settled. Fix patches in this dataset reference
# new redis-module symbols (REDISMODULE_CONFIG_UNPREFIXED, RedisModuleSlotRangeArray,
# REDISMODULE_OPEN_KEY_ACCESS_TRIMMED, ...) without adding them to src/redismodule.h.
# Redis-module API is additive, so this is safe for older RediSearch code too.
if [ -f /usr/local/include/redismodule-unstable.h ] && [ -f src/redismodule.h ]; then
  install -m 0644 /usr/local/include/redismodule-unstable.h src/redismodule.h
fi

# Hard ceiling on any individual test's runtime; runtests.sh honors this.
# Prevents the per-test-deadlock hangs we hit on a couple of PRs (e.g. pr-5843).
export TEST_TIMEOUT=300

make build IGNORE_MISSING_DEPS=1 TESTS=1 || make IGNORE_MISSING_DEPS=1 TESTS=1 || make TESTS=1 || true

if make -n unit-tests >/dev/null 2>&1; then
  make unit-tests IGNORE_MISSING_DEPS=1 || true
  make pytest     IGNORE_MISSING_DEPS=1 || true
  make rust-tests IGNORE_MISSING_DEPS=1 2>/dev/null || true
else
  make test IGNORE_MISSING_DEPS=1 || true
fi
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


@Instance.register("RediSearch", "RediSearch")
class RediSearch(Instance):
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

        # Strip ANSI escape / color codes and carriage returns. RLTest colorizes
        # the test name and the [PASS]/[FAIL]/[SKIP] marker.
        ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        lines = [ansi.sub("", ln).replace("\r", "") for ln in test_log.splitlines()]

        # --- RLTest (Python integration tests, the dominant suite) -----------
        # Real captured format (after ANSI strip + CR removal):
        #   test_tags:testInvalidSyntax:
        #       [PASS]
        # i.e. a line ending in ':' carrying "<module>:<testName>", followed by
        # a line whose stripped content is exactly [PASS]/[FAIL]/[SKIP]/[ERROR].
        # ([ERROR] == test errored == failure.) Assertion-detail lines such as
        # "X  (FAIL): ... test_tags.py:159" do not end in ':' and are ignored.
        re_rltest_status = re.compile(r"^\[(PASS|FAIL|SKIP|ERROR)\]$")
        last_name = None
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            m = re_rltest_status.match(line)
            if m and last_name:
                status = m.group(1)
                if status == "PASS":
                    passed_tests.add(last_name)
                elif status == "SKIP":
                    skipped_tests.add(last_name)
                else:  # FAIL or ERROR
                    failed_tests.add(last_name)
                last_name = None
                continue
            if line.endswith(":") and len(line) > 1 and "(FAIL)" not in line:
                last_name = line[:-1].strip()

        # Also catch single-line RLTest form: "<name>: [PASS]"
        re_rltest_inline = re.compile(r"^(.*?):\s*\[(PASS|FAIL|SKIP|ERROR)\]\s*$")
        # --- googletest (C/C++ unit tests via sbin/unit-tests) ---------------
        re_gtest_ok = re.compile(r"^\[\s+OK\s+\]\s+(.+?)\s+\(\d+\s*ms\)$")
        re_gtest_fail = re.compile(r"^\[\s+FAILED\s+\]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        # --- Python unittest verbose fallback --------------------------------
        re_ut = re.compile(r"^(.+?) \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
        # --- ctest (very old v2.0/v2.4 era: `make test` -> ctest) ------------
        re_ctest = re.compile(r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.+\s*(Passed|\*\*\*Failed|\*\*\*Skipped|\*\*\*Timeout|\*\*\*Not Run).*$")

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            m = re_rltest_inline.match(line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "SKIP":
                    skipped_tests.add(name)
                else:  # FAIL or ERROR
                    failed_tests.add(name)
                continue

            m = re_gtest_ok.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue
            m = re_gtest_fail.match(line)
            if m:
                cand = m.group(1).strip()
                if cand and not cand.lower().startswith("test"):
                    failed_tests.add(cand)
                elif cand:
                    failed_tests.add(cand)
                continue

            m = re_ctest.match(line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                if status == "Passed":
                    passed_tests.add(name)
                elif status in ("***Skipped", "***Not Run"):
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            m = re_ut.match(line)
            if m:
                name, status = m.group(1).strip(), m.group(2)
                if status == "ok":
                    passed_tests.add(name)
                elif status.startswith("skipped"):
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

        # Resolve PASS/FAIL conflicts in favor of failure (a test that fails in
        # one phase and passes in another should count as failing).
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
