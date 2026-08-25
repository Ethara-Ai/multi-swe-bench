import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Packages under `internal/pkg` whose tests start real containers and therefore
# cannot pass without a Docker daemon. They carry NO build tag, so `-tags=unit`
# does not exclude them; they have to be dropped by package path instead.
# Verified at base commit 897ed0bd by reading every `*_test.go` under
# `internal/pkg`: these are the only untagged packages that reach for
# testcontainers/dockertest/gnomock without going through `testUtils.SkipCI`.
# The rabbitmq packages are deliberately NOT listed -- they call
# `testUtils.SkipCI(t)`, which skips cleanly because the scripts export CI=true.
_PKG_INFRA_SKIP = "/test/containers/|/gorm_postgres/repository$|/mongodb/repository$"

# The repository is a Go *multi-module* workspace: every entry below owns its own
# go.mod. `internal/pkg` is consumed by the three services through a
# `replace github.com/.../internal/pkg => ../../pkg/` directive, so a patch to the
# shared package is picked up by the services without any module publishing.
# Second element is an extended-regex of package paths to skip (empty = run all).
_MODULES = [
    ("internal/pkg", _PKG_INFRA_SKIP),
    ("internal/services/catalog_write_service", ""),
    ("internal/services/catalog_read_service", ""),
    ("internal/services/order_service", ""),
]

# Tests here are separated by build tags (`unit`, `integration`, `e2e`) -- see
# scripts/test.sh, which the Makefile targets call. The integration and e2e
# suites drive testcontainers, i.e. they need a live Docker daemon plus
# Postgres/Mongo/RabbitMQ/EventStoreDB containers, which the evaluation sandbox
# does not provide. Building with `-tags=unit` compiles the untagged tests *and*
# the unit-tagged ones while leaving the infrastructure suites out of the build
# entirely, which mirrors `make unit-test`.
_GO_TEST_FLAGS = "-tags=unit -v -count=1 -p=1 -parallel=1 -timeout 20m"

_ENV_EXPORTS = "\n".join(
    [
        "export CGO_ENABLED=1",
        # CI=true is load-bearing beyond convention here: `testUtils.SkipCI(t)`
        # keys off it, which is what makes the untagged rabbitmq suites skip
        # instead of hanging against a RabbitMQ that does not exist.
        "export CI=true",
        "export GOFLAGS=-mod=mod",
        # Keep the toolchain pinned to the image's Go: the modules declare
        # `go 1.21` and must never trigger an on-the-fly toolchain download.
        "export GOTOOLCHAIN=local",
    ]
)

# Shared by all three stage scripts, so the test command can never drift between
# stages. No `|| true` on the test command itself: `go test` exits 1 for ordinary
# test *and* build failures, which are real results the parser must see, so only
# that status is tolerated per module. Anything above 1 means the toolchain could
# not run at all and aborts the stage loudly, rather than leaving the parser to
# report an empty result that looks like "nothing to fix".
_RUN_MODULES_BLOCK = """\
ran=0

run_module() {
  module="$1"
  skip_re="$2"

  echo "===== MODULE $module ====="
  cd "/home/__REPO__/$module"

  # `grep -Ev` exits 1 when it filters everything out, which is not an error
  # here; a failure of `go list` itself still propagates via pipefail.
  if [ -n "$skip_re" ]; then
    pkgs=$(go list -tags=unit ./... | ( grep -Ev "$skip_re" || true ))
  else
    pkgs=$(go list -tags=unit ./...)
  fi

  if [ -z "$pkgs" ]; then
    echo "FATAL: no packages resolved in $module"
    exit 1
  fi

  rc=0
  # $pkgs is intentionally unquoted: it is a whitespace-separated package list.
  go test __FLAGS__ $pkgs || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "FATAL: go test could not run in $module (exit $rc)"
    exit "$rc"
  fi
  ran=$((ran + 1))
}

__CALLS__
if [ "$ran" -eq 0 ]; then
  echo "FATAL: no module produced test output"
  exit 1
fi
"""


def _run_modules_block(repo: str) -> str:
    calls = "\n".join(
        f'run_module "{module}" "{skip}"' for module, skip in _MODULES
    )
    return (
        _RUN_MODULES_BLOCK.replace("__REPO__", repo)
        .replace("__FLAGS__", _GO_TEST_FLAGS)
        .replace("__CALLS__", calls + "\n")
    )


class GoFoodDeliveryMicroservicesImageBase(Image):
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
        return "golang:1.21"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def _clone_and_scrub(self) -> str:
        repo = self.pr.repo
        return (
            "# Clone + detach + scrub in ONE layer -- see _clone_and_scrub().\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} \\\n'
            f"    && cd /home/{repo} \\\n"
            '    && git checkout --detach "${BASE_COMMIT}" \\\n'
            "    && (git remote remove origin 2>/dev/null || true) \\\n"
            "    && git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d \\\n"
            "    && git reflog expire --expire=now --all \\\n"
            "    && git reflog expire --expire-unreachable=now --all \\\n"
            "    && git gc --prune=now --aggressive \\\n"
            "    && git repack -a -d -l --quiet \\\n"
            "    && rm -f .git/objects/info/alternates"
        )

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = self._clone_and_scrub()
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GoFoodDeliveryMicroservicesImageDefault(Image):
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
        return GoFoodDeliveryMicroservicesImageBase(self.pr, self._config)

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

{env}

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the module cache at IMAGE BUILD time, while the network is still
# reachable, so no evaluation stage needs to download anything.
#
# The gold patch rewrites every go.mod/go.sum (it swaps go-redis/v8 for
# go-redis/v9, drops solsw/go2linq, and adds asynq, samber/lo, ginkgo/gomega and
# the testcontainers postgres module), so warming only the BASE dependency graph
# would leave the fix stage needing the network. The cache is therefore filled
# twice: once against the patched tree, then again after resetting to base.
warm() {{
  for module in {modules}; do
    cd "/home/{pr.repo}/$module"
    echo "===== warming $module"
    go mod download all || true
    # `|| true` above is the harness convention (a transient fetch hiccup should
    # not abort the build), but a Go image with an incomplete module cache is
    # useless: every later stage would fail to compile with an unhelpful error.
    # `go list -deps` resolves the full graph explicitly and, under `set -e`,
    # fails the build here, where the cause is obvious.
    go list -deps -tags=unit ./... > /dev/null
  done
}}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
warm

# Back to the pristine base tree. `git checkout -- .` alone is not enough: the
# test patch adds new files, and `go mod download` may append missing checksums
# to go.sum, either of which would leave the tree dirty and make `git apply` of
# the gold hunks fail at evaluation time.
cd /home/{pr.repo}
git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

warm

cd /home/{pr.repo}
git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

""".format(
                    pr=self.pr,
                    env=_ENV_EXPORTS,
                    modules=" ".join(module for module, _ in _MODULES),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

{tests}
""".format(env=_ENV_EXPORTS, tests=_run_modules_block(self.pr.repo)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

# Patch paths are repo-root relative, so apply from the repo root and only then
# descend into each Go module. This is why every stage script starts at the root.
# `set -e` matters here: a failed `git apply` must abort, otherwise the stage
# would test unpatched code and report a clean run that hides the real cause.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{tests}
""".format(pr=self.pr, env=_ENV_EXPORTS, tests=_run_modules_block(self.pr.repo)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{tests}
""".format(pr=self.pr, env=_ENV_EXPORTS, tests=_run_modules_block(self.pr.repo)),
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


@Instance.register("mehdihadeli", "go-food-delivery-microservices")
class GoFoodDeliveryMicroservices(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GoFoodDeliveryMicroservicesImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # `go test -v` prints one verdict line per test (and per subtest, which
        # is indented -- hence the strip() below) followed by a package summary
        # line. Names are qualified with the package import path, which is unique
        # across the four modules.
        re_verdict = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")
        re_pkg_end = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

        def record(name: str, status: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        pending: list[tuple[str, str]] = []

        for line in clean.splitlines():
            line = line.strip()

            verdict = re_verdict.match(line)
            if verdict:
                pending.append((verdict.group(2), verdict.group(1)))
                continue

            pkg_end = re_pkg_end.match(line)
            if pkg_end and ("/" in pkg_end.group(2) or "." in pkg_end.group(2)):
                marker, pkg = pkg_end.group(1), pkg_end.group(2)
                # `?   pkg [no test files]` carries no verdict, so it must not
                # flush the pending verdicts onto the wrong package.
                if marker == "?":
                    continue
                for name, status in pending:
                    record(f"{pkg}::{name}", status)
                pending.clear()

                if marker == "FAIL":
                    record(pkg, "FAIL")
                elif marker == "ok":
                    record(pkg, "PASS")

        for name, status in pending:
            record(name, status)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
