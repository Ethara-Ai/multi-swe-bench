import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/dagger/dagger/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Each test emits a JSON event with
    `Action` (run/pass/fail/skip), `Package`, and `Test`. Names are kept
    package-qualified (`pkg/path::TestName`) since Go test function names
    recur across packages; subtests appear as `TestName/sub`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if pkg.startswith(_REPO_PREFIX):
            pkg = pkg[len(_REPO_PREFIX) :]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # Enforce TestResult disjoint-set invariant.
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


class DaggerEra1ImageBase(Image):
    """dagger era 1 (PRs 1150-6001, v0.1->0.6): go.mod `go 1.16`-`1.20`.
    Pure-Go CI/CD container-build engine. Built with Go 1.20 (>= every go.mod
    in this era). Tests are run with `go test` on the patched unit packages;
    `core/integration/` + `e2e/` tests (which need a live dagger engine +
    container runtime) are deliberately excluded."""

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
        return "golang:1.20"

    def image_tag(self) -> str:
        return "base-go120"

    def workdir(self) -> str:
        return "base-go120"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED era base: built ONCE (image_tag "base-go120") and reused by every
        # era-1 PR. It clones the repo at default HEAD with FULL history and
        # deliberately does NOT check out a per-PR ${BASE_COMMIT} and does NOT run
        # the anti-cheat hardening block. Pinning + history-stripping a SHARED base
        # would prune every sibling PR's base.sha, and their `git checkout <sha>`
        # in prepare.sh would then fail (prepare.sh runs under `set -e`, so the
        # build of every PR but the one that seeded the base would break). The
        # per-PR checkout and the hardening happen in DaggerEra1ImageDefault, which
        # is the image the model is actually evaluated in.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this Dockerfile verbatim (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`); that is precisely what stops
        # _standardize_repo_fetch() from rewriting the clone into a
        # `git checkout ${BASE_COMMIT}` + hardening template. Because the enhancer
        # is opted out, the ARG/ENV/LABEL infra it would have injected is inlined
        # here. `origin` is dropped at this layer too, so nothing downstream can
        # `git fetch` the upstream fix even before hardening runs.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS="-buildvcs=false -mod=mod"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class DaggerEra1ImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules, run the targeted
    Go unit tests."""

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
        return DaggerEra1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Go package dirs the PR's test patch touches, excluding integration/e2e
# suites (those need a running dagger engine + container runtime).
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.webp' \
    --exclude='*.webm' --exclude='*.age' --exclude='*.eot' --exclude='*.ttf' \
    --exclude='*.lock' --exclude='*.bin' --exclude='*.class' \
    --exclude='telemetry/telemetry' --exclude='toolchains/python-sdk-dev/dockerd/main')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.webp' \
    --exclude='*.webm' --exclude='*.age' --exclude='*.eot' --exclude='*.ttf' \
    --exclude='*.lock' --exclude='*.bin' --exclude='*.class' \
    --exclude='telemetry/telemetry' --exclude='toolchains/python-sdk-dev/dockerd/main')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
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

        # Anti-cheat hardening lives in the PR layer -- this is the image the model
        # is evaluated in, and the only layer that knows a single BASE_COMMIT.
        # prepare.sh has already checked out this PR's base.sha, so bake
        # Image._HARDENING_BLOCK with the literal sha: detach at base.sha, drop
        # origin, delete every ref (heads/remotes/tags/replace), expire the
        # reflogs, `gc --prune=now --aggressive` + repack, then ASSERT
        # HEAD == base.sha, no refs left, no remote left, and
        # `git rev-list --all --count` == `git rev-list HEAD --count`. Net effect:
        # the merge/fix commits that come after base.sha become unreachable and
        # are pruned, so `git log`, `git show <future-sha>` and `git fetch` cannot
        # be used to recover the gold patch.
        #
        # The literal sha (rather than a ${BASE_COMMIT} build-arg) is required:
        # run_evaluation.build_image only passes REPO_URL/BASE_COMMIT build-args
        # when dependency() is a str, and here it is an Image. For the same reason
        # DockerfileEnhancer.enhance() returns this Dockerfile unchanged, so the
        # layout below is exactly what gets built.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("dagger", "dagger_6001_to_1150")
class DAGGER_6001_TO_1150(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DaggerEra1ImageDefault(self.pr, self._config)

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
        return parse_go_test_log(log)
