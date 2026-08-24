import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ServicecombServiceCenterImageBase(Image):
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
        # `go.mod` declares `go 1.13` and CI pins 1.13; 1.16 is the oldest tag
        # whose Debian base (bullseye) is not EOL, so apt still resolves.
        return "golang:1.16"

    def image_tag(self) -> str:
        # Per-PR, not a shared `base`: DockerfileEnhancer injects
        # `git checkout ${BASE_COMMIT}` plus the history scrub into this image, so a
        # tag shared across PRs would be pinned to whichever PR built it first and
        # would have every other PR's commit pruned out of the object store.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# `scripts/ut_test_in_docker.sh` runs the datasource/mongo suite against a
# MongoDB on 127.0.0.1:27017 started as a sibling container. The harness runs
# each stage in a single container, so the server is installed here and started
# per stage by `start-mongo.sh`. Without it every test in the package fails on
# connection refused and the instance is unusable.
#
# Each tarball is pinned by SHA-256 (the digests MongoDB publishes alongside the
# archives as `<url>.sha256`). `curl -fsSL` only proves the transfer completed --
# it says nothing about the bytes. Under an inspecting MITM proxy an unverified
# binary download is exactly the fetch that should be digest-pinned, and a
# corrupted archive would otherwise unpack into a subtly broken mongod that
# fails as "test failures" at run time.
#
# arm64 note: mongod itself is fine on arm64, but the repo's own test
# dependency `bou.ke/monkey` patches machine code and implements amd64 only, so
# `datasource/mongo/event` cannot compile there. The arm64 image therefore
# builds but cannot run this suite; that is an upstream limitation neither this
# Dockerfile nor the run scripts can repair. Grade instances on amd64.
RUN set -eux; \\
    case "$(dpkg --print-architecture)" in \\
        amd64) \\
            MONGO_URL="https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian10-4.4.29.tgz"; \\
            MONGO_SHA256="dd254701bebe1050f96cbb6928f0c7f66b541747dcf4881e4517cb1f435c2b8e" \\
            ;; \\
        arm64) \\
            MONGO_URL="https://fastdl.mongodb.org/linux/mongodb-linux-aarch64-ubuntu2004-4.4.29.tgz"; \\
            MONGO_SHA256="fa1c6f8758aba1624d3f3b2f7cc2e99b14e682fac9495c42c9a3c50c8a8697f5" \\
            ;; \\
        *) echo "unsupported architecture" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "$MONGO_URL" -o /tmp/mongodb.tgz; \\
    echo "$MONGO_SHA256  /tmp/mongodb.tgz" | sha256sum -c -; \\
    tar -xzf /tmp/mongodb.tgz -C /opt; \\
    mv /opt/mongodb-linux-* /opt/mongodb; \\
    rm -f /tmp/mongodb.tgz

{code}

{self.clear_env}

"""


class ServicecombServiceCenterImageDefault(Image):
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
        return ServicecombServiceCenterImageBase(self.pr, self.config)

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

export GOFLAGS=-mod=mod
go mod download || true
go build ./... || true

# Warm the test-binary compile cache WITHOUT executing anything.
#
# `-exec /bin/true` is load-bearing, not a tidy-up. Three of the
# `datasource/mongo` test files declare a package-level
#
#     func init() {{ client.NewMongoClient(storage.Options{{URI: "mongodb://localhost:27017"}}) }}
#
# and `NewMongoClient` dials through `Initialize` -> `newClient(context.Background())`
# with no timeout context. `init()` runs when the test BINARY STARTS, and at
# image-build time no mongod is listening (`start-mongo.sh` only runs per stage),
# so the binary blocks on connection setup. Observed live: a `mongo.test` process
# wedged under qemu-aarch64 with 11s of CPU across 21 minutes, frozen net/block IO.
#
# None of the obvious guards help:
#   * `-run ZZZ_WARM_CACHE_ONLY` filters which TESTS EXECUTE; the binary still
#     starts and still runs `init()`.
#   * `-timeout` is armed inside `m.Run()`, which a pre-`m.Run()` block never reaches.
#   * `|| true` catches a non-zero exit, but the process never exits at all.
#
# `-exec /bin/true` makes `go test` compile each binary and then "run" it via
# /bin/true, which exits 0 immediately -- the cache is warmed and `init()` never
# fires. `timeout` is the belt-and-braces bound in case a future test package
# blocks somewhere else in the toolchain.
timeout 900 go test -count=1 -p 1 -run ZZZ_WARM_CACHE_ONLY -exec /bin/true ./datasource/mongo/... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "start-mongo.sh",
                """#!/bin/bash
set -eo pipefail

rm -rf /data/db
mkdir -p /data/db
# `--fork` exits 0 once the PARENT has forked; it is not proof the child
# survived initialization. The readiness probe below is the real gate.
/opt/mongodb/bin/mongod --dbpath /data/db --bind_ip 127.0.0.1 --fork --logpath /tmp/mongod.log

for i in $(seq 1 60); do
  (echo > /dev/tcp/127.0.0.1/27017) > /dev/null 2>&1 && break
  sleep 1
done

# The loop above cannot fail on its own. When the port never opens it simply
# runs out of iterations, the last command executed is `sleep 1`, and the script
# would return 0 with no mongod listening -- `set -e` never sees a failure. Every
# mongo-backed test would then fail on connection refused and get recorded as a
# genuine test failure, producing a confident and completely wrong report. Probe
# once more and abort loudly instead.
if ! (echo > /dev/tcp/127.0.0.1/27017) > /dev/null 2>&1; then
  echo "start-mongo: mongod did not accept connections on 127.0.0.1:27017 within 60s" >&2
  echo "start-mongo: ---- /tmp/mongod.log ----" >&2
  cat /tmp/mongod.log >&2 || true
  exit 1
fi

""".format(),
            ),
            File(
                ".",
                "report-build-failures.sh",
                """#!/bin/bash
# Usage: report-build-failures.sh <go-test-log>
#
# `go test` builds one test binary per package. A single `_test.go` that does
# not compile takes that whole binary down, and the only thing `go test` then
# prints for the package is
#
#     FAIL	github.com/apache/servicecomb-service-center/datasource/mongo [build failed]
#
# with no `--- PASS:`/`--- FAIL:` lines at all. Every test in the package
# silently disappears from the parsed result instead of being recorded as
# failing.
#
# That is precisely the test-patch stage of any PR whose gold test exercises a
# symbol the fix patch introduces -- the usual unexported-to-exported rename.
# The test patch is applied to the base commit, where the symbol does not exist
# yet, so `datasource/mongo` fails to compile and its tests vanish, leaving
# test_patch_result far smaller than run_result even though a test patch can
# only ever add tests.
#
# Re-attribute the compile failure to the tests it silenced: for every package
# reported `[build failed]`, emit one `--- FAIL:` line per top-level
# `func TestXxx` declared in that package's `_test.go` files. A test that
# cannot be built did not pass, so FAIL is the honest status, and it keeps the
# stage-over-stage counts comparable.
#
# Deliberately not `set -e`: `grep` exiting 1 on "no match" is the normal case
# here (no build failures) and must not abort the script.
set -uo pipefail

MODULE=github.com/apache/servicecomb-service-center
log="${{1:?usage: report-build-failures.sh <go-test-log>}}"

[ -f "$log" ] || exit 0

pkgs=$(grep -E '^FAIL[[:space:]]+[^[:space:]]+[[:space:]]+\\[build failed\\]' "$log" \\
       | awk '{{print $2}}' | sort -u)

for pkg in $pkgs; do
  # Import path -> directory, relative to the module root (this script runs
  # with the repo as cwd). Anything outside the module is not ours to map.
  case "$pkg" in
    "$MODULE") dir="." ;;
    "$MODULE"/*) dir=".${{pkg#$MODULE}}" ;;
    *) continue ;;
  esac
  [ -d "$dir" ] || continue

  # Only top-level `func TestXxx(` counts. Methods (`func (s *Suite) TestX`)
  # run as subtests of their runner, and TestMain is the package entry point,
  # not a test.
  names=$(grep -hoE '^func Test[A-Za-z0-9_]*\\(' "$dir"/*_test.go 2>/dev/null \\
          | sed -E 's/^func //; s/\\($//' | sort -u)

  emitted=0
  for name in $names; do
    if [ "$name" = "TestMain" ]; then
      continue
    fi
    # Never contradict a real result. parse_log flattens test names across
    # packages, so a name that another package already reported must keep the
    # status that package observed.
    if grep -qE "^[[:space:]]*--- (PASS|FAIL|SKIP): ${{name}}[ /]" "$log"; then
      continue
    fi
    # Provenance banner, printed once, immediately before the first synthetic
    # line for this package. Without it the appended `--- FAIL:` lines sit after
    # the final package summary with no matching `=== RUN`, and anyone diffing
    # raw `go test` output against the parsed report would reasonably read them
    # as fabricated. The banner cannot be mistaken for a result: parse_log only
    # matches `--- PASS/FAIL/SKIP:`, so these lines are inert to the parser.
    if [ "$emitted" -eq 0 ]; then
      echo "=== SYNTHETIC: $pkg reported [build failed]; go test produced no per-test"
      echo "=== SYNTHETIC: results for it. report-build-failures.sh attributes the compile"
      echo "=== SYNTHETIC: failure to every top-level Test func declared in $dir."
      emitted=1
    fi
    echo "--- FAIL: $name (0.00s)"
  done

  if [ "$emitted" -eq 1 ]; then
    echo "=== SYNTHETIC: end of synthesized results for $pkg"
  fi
done

""".format(),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_MODE=mongo
export GOFLAGS=-mod=mod

cd /home/{pr.repo}
bash /home/start-mongo.sh

# `tee` so the harness still sees the live output while
# `report-build-failures.sh` gets a file to re-read; PIPESTATUS keeps the
# stage's exit code the one `go test` returned.
set +e
go test -v -count=1 -p 1 ./datasource/mongo/... 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_MODE=mongo
export GOFLAGS=-mod=mod

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/start-mongo.sh

set +e
go test -v -count=1 -p 1 ./datasource/mongo/... 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_MODE=mongo
export GOFLAGS=-mod=mod

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/start-mongo.sh

set +e
go test -v -count=1 -p 1 ./datasource/mongo/... 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

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


@Instance.register("apache", "servicecomb-service-center")
class ServicecombServiceCenter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServicecombServiceCenterImageDefault(self.pr, self._config)

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

        # Only the `--- STATUS:` lines are parsed. The package-level summary
        # (`FAIL\tgithub.com/...\t90.088s`, and `[build failed]` at the test
        # stage) is deliberately left alone: a pattern loose enough to catch it
        # captures the import path as if it were a test name. The tests a
        # `[build failed]` package swallows are recovered upstream instead --
        # `report-build-failures.sh` appends a synthetic `--- FAIL:` line for
        # each one, so they arrive here as ordinary failures.
        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(get_base_name(match.group(1)))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(get_base_name(match.group(1)))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(get_base_name(match.group(1)))

        # Subtests collapse onto their parent, so one parent can be reported
        # both ways. Failure wins, which also keeps the sets disjoint.
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
