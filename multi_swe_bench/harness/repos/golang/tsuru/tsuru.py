import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# tsuru's own Makefile target is `go test ./... -check.v`. The -check.v flag is
# consumed by gopkg.in/check.v1 (gocheck) inside the test binary, not by
# `go test`, so it has to sit *after* the package list. Without it gocheck
# reports a single Go-level "--- PASS: Test" per package and every individual
# S.TestXxx stays invisible, which collapses a whole package into one result
# and makes the instance useless for grading.

# -p 1 serializes packages. provision/kubernetes contains timing-sensitive
# suites (S.TestServiceManagerDeployMultipleFlows and friends) that drive a
# fake Kubernetes reactor and poll for objects another goroutine is still
# creating. When the 8 CPUs are oversubscribed by parallel package runs, the
# poll wins the race and the test dies with
# `deployments.apps "myapp4-p1-v2" not found`. Observed flipping between pass
# and fail across two builds of the identical commit, which invalidated the
# whole instance ("before the fix patch the test passed; after it failed").
# Serializing costs ~2 min per run (163s -> 281s, the long pole is one 108s
# package) and makes the result independent of how loaded the host is, rather
# than relying on the caller to pass a low --max_workers.
GO_TEST = """go test -p 1 -count=1 -v -timeout 1800s ./... -check.v"""

# tsuru's suites talk to a real MongoDB and a real Redis (see
# .github/workflows/ci.yaml, which runs mongo:5 on 27017 and redis on 6379).
# Both are started inside the container rather than linked as services, so
# every script that runs tests has to bring them up first.
START_SERVICES = "bash /home/start-services.sh"

# Import path from go.mod; stripped from package lines to get repo-relative
# directories for the `path::test` ids parse_log emits.
MODULE_PATH = "github.com/tsuru/tsuru"

# Tests excluded from every result set because they flip between pass and fail
# on identical code. report.py rejects an instance when a test passes under the
# test patch and fails under the fix patch, so a randomly flipping test decides
# validity by luck: across three builds of commit 32701d23 this one failed in
# prepare on build 1, in fix-run on build 2 (which invalidated the instance),
# and in run on build 3. It is a race in tsuru's fake-Kubernetes reactor --
# the test polls for a Deployment another goroutine is still creating and dies
# with `deployments.apps "myapp4-p1-v2" not found` -- and it is unrelated to
# this PR, whose target test is S.TestRebuildRoutesSetsHealthcheck. Serializing
# packages with -p 1 did NOT fix it, so it is dropped from the counts entirely
# rather than left to chance. Excluding it costs 1 of 2142 tests.
KNOWN_FLAKY_TESTS = frozenset(
    {
        "S.TestServiceManagerDeployMultipleFlows",
    }
)

# Everything the image needs before the repo is fetched. Kept as a plain
# (non-f) string so the shell's ${TARGETARCH} / ${PATH} survive verbatim.
#
# An Ubuntu focal base rather than golang:1.22-bullseye on purpose. MongoDB
# publishes NO arm64 server package for Debian at any version (bullseye
# 5.0/6.0, bookworm 7.0/8.0 all ship only mongosh and the Atlas CLI). Ubuntu
# focal's multiverse component does ship mongodb-org-server for both amd64 and
# arm64, so it is the only supported source that covers a multi-arch build.
#
# ca-certificates/curl/git are listed below even though buildpack-deps:focal-scm
# already provides them: apt treats them as satisfied, and naming them keeps the
# requirement explicit if the base image is ever changed again.
TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git gnupg redis-server xmlsec1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://pgp.mongodb.com/server-5.0.asc \
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-5.0.gpg \
    && echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-5.0.gpg ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/5.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-5.0.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mongodb-org-server mongodb-org-shell \
    && rm -rf /var/lib/apt/lists/*

# Go comes from the official tarball because the golang: images are Debian
# based and would drag the MongoDB arm64 problem straight back in. TARGETARCH
# is supplied by BuildKit and maps directly onto Go's archive names. The
# checksum is verified per arch so a corrupted or substituted download fails
# the build instead of silently producing a bad image.
ARG GO_VERSION=1.22.12
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) go_sha="4fa4f869b0f7fc6bb1eb2660e74657fbf04cdd290b5aef905585c86051b34d43" ;; \
        arm64) go_sha="fd017e647ec28525e86ae8203236e0653242722a7436929b1f775744e26278e7" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz"; \
    echo "${go_sha}  /tmp/go.tgz" | sha256sum -c -; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm /tmp/go.tgz; \
    /usr/local/go/bin/go version

ENV PATH="/usr/local/go/bin:${PATH}"
# go.mod declares `go 1.22`; pinning the toolchain stops Go silently fetching
# a different one at build time, which would make the image non-reproducible.
ENV GOTOOLCHAIN=local

RUN mkdir -p /data/db /var/log/mongodb
"""


class TsuruImageBase(Image):
    """Level 1: per-PR base image -- toolchain plus the repository checkout.

    Tagged `base-pr-<number>`, not a shared `base`, because the Dockerfile QC
    contract requires the PR layer to inherit
    `mswebench/<org>_m_<repo>:base-pr-<N>`. A shared tag also hides a real
    hazard: this image bakes in one BASE_COMMIT, so a single reused `base`
    stays pinned to whichever PR built it first, and any later PR whose base
    commit is unreachable from that sha dies in prepare.sh with
    `fatal: unable to read tree`. The cost is one ~1.4 GB base image per PR
    instead of one per repo -- deliberate, and it is what rule 4 in CLAUDE.md
    was written to avoid before that rule was set aside.

    dependency() returns a *string*, which matters for three separate reasons:

      1. DockerfileEnhancer.enhance() only rewrites Dockerfiles whose
         dependency is a string; anything else is returned verbatim. So this
         is the only image that receives the `# syntax` directive, the proxy
         ARGs, the CA-certificate symlinks and the OCI labels.
      2. build_dataset only passes the REPO_URL / BASE_COMMIT build args to
         string-dependency images. An image further down the chain cannot see
         them.
      3. DockerfileEnhancer._standardize_repo_fetch() rewrites the `{code}`
         line below into the canonical `git clone "${REPO_URL}"` +
         `git checkout ${BASE_COMMIT}` + hardening + CMD block.

    Together those mean the repository fetch *must* live here. Putting it in
    the per-PR image instead yields a hardcoded clone URL with no proxy or
    certificate support and a hand-pasted hardening block.

    Built on buildpack-deps:focal-scm with Go installed from the official
    tarball, rather than on a golang: image -- see TOOLCHAIN_SETUP for why.
    The image and the Go archives both cover amd64 and arm64, so this base is
    multi-arch.

    focal-scm rather than a bare ubuntu:20.04 for a specific reason. The
    enhancer injects its CA-certificate symlink farm immediately after FROM,
    before any line this config contributes, and every one of those symlinks
    targets /etc/ssl/certs/ca-certificates.crt. A bare ubuntu:20.04 does not
    ship that file (verified: `docker run --rm ubuntu:20.04 ls
    /etc/ssl/certs/ca-certificates.crt` -> No such file), so the farm would
    create six dangling links and ENV SSL_CERT_FILE would point at nothing
    until the first apt install happened to pull ca-certificates in. That
    worked only because Ubuntu's apt uses http and the first https call came
    later -- ordering luck, not design, and it is Dockerfile QC item D8.
    buildpack-deps:focal-scm is the same Ubuntu focal userland (so the MongoDB
    focal repo below still resolves) but already ships ca-certificates, curl
    and git, so the farm is valid the moment it is created.
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
        return "buildpack-deps:focal-scm"

    def image_tag(self) -> str:
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

        # Everything that is not the repo fetch has to sit *above* `{code}`:
        # _standardize_repo_fetch replaces that single line with a block that
        # ends in CMD ["/bin/bash"], so any instruction placed after it would
        # land past the CMD.
        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{code}

{self.clear_env}

"""
        )


class TsuruImageDefault(Image):
    """Level 2: per-PR image -- patches, run scripts, and the warm-up build.

    dependency() returns an Image, so this Dockerfile is used exactly as
    written. It deliberately contains no repo fetch and no hardening block:
    both already happened in the base image, where the enhancer generated them.
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
        return TsuruImageBase(self.pr, self.config)

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
                "start-services.sh",
                """#!/bin/bash
# Bring up the two backing services tsuru's suites expect on localhost.
# Deliberately NOT `set -e`: this script runs at the top of every run script
# and must be idempotent -- on the second call mongod/redis are already up and
# the start commands are expected to no-op rather than abort the run.
mkdir -p /data/db /var/log/mongodb

if ! pgrep -x mongod > /dev/null 2>&1; then
  mongod --fork --dbpath /data/db --logpath /var/log/mongodb/mongod.log \\
         --bind_ip 127.0.0.1 --port 27017
fi

if ! pgrep -x redis-server > /dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379
fi

# mongod --fork returns before the socket is accepting connections, so poll
# instead of sleeping a fixed amount: a fixed sleep is exactly what makes these
# runs flaky on a loaded host.
for i in $(seq 1 60); do
  if mongo --quiet --host 127.0.0.1:27017 --eval 'db.runCommand({ ping: 1 })' > /dev/null 2>&1; then
    echo "start-services: mongod is ready"
    break
  fi
  if [ "$i" = "60" ]; then
    echo "start-services: mongod did not become ready in 60s"
    cat /var/log/mongodb/mongod.log || true
    exit 1
  fi
  sleep 1
done

for i in $(seq 1 30); do
  if redis-cli -h 127.0.0.1 -p 6379 ping > /dev/null 2>&1; then
    echo "start-services: redis is ready"
    break
  fi
  sleep 1
done

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

go mod download

# The suite is run here only to warm the Go build cache so the three grading
# stages are fast. On a foreign architecture that warm-up runs under QEMU
# emulation, which measured ~10x slower on this repo: ~21 min just to compile,
# and hours to execute. It also buys nothing for grading -- docker_util.run()
# passes no platform to containers.run(), and build() loads only
# _detect_native_platform() into the daemon, so run/test-run/fix-run and the
# final report are always produced on the native arch. Skipping it therefore
# costs the foreign-arch image a warm cache, not correctness.
if [ "$(uname -m)" = "x86_64" ]; then
  {start_services}
  {go_test} || true
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up (see comment in tsuru.py prepare.sh)."
fi

""".format(pr=self.pr, start_services=START_SERVICES, go_test=GO_TEST),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{start_services}
{go_test}

""".format(pr=self.pr, start_services=START_SERVICES, go_test=GO_TEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
{start_services}
{go_test}

""".format(pr=self.pr, start_services=START_SERVICES, go_test=GO_TEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
{start_services}
{go_test}

""".format(pr=self.pr, start_services=START_SERVICES, go_test=GO_TEST),
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


@Instance.register("tsuru", "tsuru")
class Tsuru(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TsuruImageDefault(self.pr, self._config)

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
        """Build repo-relative `path::test` ids, e.g.

            router/rebuild/rebuild_test.go::S.TestRebuildRoutesSetsHealthcheck

        The id has to be assembled from two different lines, because neither
        carries it alone:

          * gocheck prints only the *basename*
                PASS: rebuild_test.go:59: S.TestRebuildRoutesSetsHealthcheck
          * the package summary prints the full import path, and arrives only
            after every test in that package has reported
                ok  github.com/tsuru/tsuru/router/rebuild   0.4s

        So results are buffered per package and the directory is applied when
        the summary line lands. `go test -p 1` keeps packages strictly
        sequential, so a buffer cannot mix two packages together.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # gocheck (-check.v): "PASS: rebuild_test.go:59: S.TestFoo   0.003s"
        re_gocheck = re.compile(r"^(PASS|FAIL|SKIP): (\S+):\d+: (\S+\.\S+)")
        # Go's own: "--- PASS: TestDynamicSuites (0.00s)"
        re_go_level = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")
        # "ok  	github.com/tsuru/tsuru/router/rebuild	0.419s" and
        # "FAIL	github.com/tsuru/tsuru/app/version [build failed]"
        re_pkg_done = re.compile(
            r"^(ok|FAIL)\s+(\S+)\s+(?:[\d.]+m?s|\[(?:build|setup) failed\])"
        )
        re_pkg_broken = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]")

        # Every tsuru package declares the same gocheck entry point,
        # `func Test(t *testing.T) { check.TestingT(t) }`, so Go's own output
        # carries one `--- PASS: Test` per package, all sharing the name
        # "Test". The real per-test verdicts arrive on the gocheck lines, so
        # counting it would let packages fight over one identifier.
        GOCHECK_ENTRY_POINT = "Test"

        pending = []  # (status, basename or None, test name) for current package

        def rel_dir(pkg: str) -> str:
            if pkg == MODULE_PATH:
                return ""
            if pkg.startswith(MODULE_PATH + "/"):
                return pkg[len(MODULE_PATH) + 1 :]
            return pkg

        def record(status: str, test_id: str) -> None:
            if status == "PASS":
                if test_id in failed_tests:
                    return
                skipped_tests.discard(test_id)
                passed_tests.add(test_id)
            elif status == "FAIL":
                passed_tests.discard(test_id)
                skipped_tests.discard(test_id)
                failed_tests.add(test_id)
            elif status == "SKIP":
                if test_id not in passed_tests and test_id not in failed_tests:
                    skipped_tests.add(test_id)

        def flush(pkg: str) -> None:
            directory = rel_dir(pkg)
            for status, basename, name in pending:
                if basename:
                    path = f"{directory}/{basename}" if directory else basename
                else:
                    # Plain Go tests report no file, so the package directory
                    # is the most precise location available.
                    path = directory or pkg
                record(status, f"{path}::{name}")
            pending.clear()

        for line in test_log.splitlines():
            line = line.strip()

            m = re_gocheck.match(line)
            if m:
                status, basename, name = m.group(1), m.group(2), m.group(3)
                if name not in KNOWN_FLAKY_TESTS:
                    pending.append((status, basename, name))
                continue

            m = re_go_level.match(line)
            if m:
                status, name = m.group(1), m.group(2)
                if name != GOCHECK_ENTRY_POINT and name not in KNOWN_FLAKY_TESTS:
                    pending.append((status, None, name))
                continue

            # A package that fails to build runs no tests at all, so it would
            # otherwise vanish from the counts instead of being a failure. It
            # is a package, not a test, so it carries no `::name` suffix.
            m = re_pkg_broken.match(line)
            if m:
                flush(m.group(1))
                broken = rel_dir(m.group(1))
                passed_tests.discard(broken)
                failed_tests.add(broken)
                continue

            m = re_pkg_done.match(line)
            if m:
                flush(m.group(2))
                continue

        # Safety net: a package whose summary line never arrived (killed run,
        # truncated log) would otherwise silently drop its results.
        if pending:
            flush(MODULE_PATH)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
