import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FasthttpLegacyImageBase(Image):
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
        # Pre-modules era (PR #234). The base snapshot ships no go.mod/go.sum and
        # no vendor/ tree, so the build must run under GOPATH mode. Modern
        # toolchains dropped GOPATH-mode `go get`, so pin golang:1.13 and vendor
        # the deps the package + PR #234's fix.patch need at 2017-contemporary
        # versions.
        return "golang:1.13"

    def image_tag(self) -> str:
        return "base-legacy"

    def workdir(self) -> str:
        return "base-legacy"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # `# syntax` = enhancer opt-out (PIPELINE §2): keeps the SHARED legacy
        # base from being pinned/pruned by the auto-injected checkout. fasthttp
        # imports itself as github.com/valyala/fasthttp, so the tree must live
        # under $GOPATH/src at that path (symlinked). External deps are pinned to
        # go1.13-compatible tags: klauspost/compress v1.4.0 (newer tags use
        # generics go1.13 cannot parse) + bytebufferpool/tcplisten.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOPATH=/go \\
    GO111MODULE=off

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

RUN mkdir -p /go/src/github.com/valyala /go/src/github.com/klauspost \\
    && ln -sfn /home/{repo} /go/src/github.com/valyala/{repo} \\
    && git clone https://github.com/klauspost/compress.git /go/src/github.com/klauspost/compress \\
    && git -C /go/src/github.com/klauspost/compress checkout v1.4.0 \\
    && git clone https://github.com/valyala/bytebufferpool.git /go/src/github.com/valyala/bytebufferpool \\
    && git -C /go/src/github.com/valyala/bytebufferpool checkout v1.0.0 \\
    && git clone https://github.com/valyala/tcplisten.git /go/src/github.com/valyala/tcplisten

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class FasthttpLegacyImageDefault(Image):
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
        return FasthttpLegacyImageBase(self.pr, self.config)

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

git config --global --add safe.directory '*'
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Re-establish the GOPATH symlink in case the checkout clobbered it.
mkdir -p /go/src/github.com/valyala
ln -sfn /home/{pr.repo} /go/src/github.com/valyala/{pr.repo}

""".format(pr=self.pr),
            ),
            File(
                ".",
                "common.sh",
                """#!/bin/bash
# Pre-modules era (PR #234): work inside $GOPATH/src/github.com/valyala/fasthttp
# (a symlink) so the package self-imports resolve under GO111MODULE=off.
REPO_SRC=/go/src/github.com/valyala/fasthttp

apply_patch() {
  local f="$1"
  [ -s "$f" ] || return 0
  git apply --whitespace=nowarn "$f" \\
    || git apply --whitespace=nowarn --3way "$f" \\
    || git apply --whitespace=nowarn --reject "$f" \\
    || true
}

run_go_tests() {
  echo "=== Running go test in $PWD (GO111MODULE=$GO111MODULE) ==="
  go test -v -count=1 ./...
}
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

source /home/common.sh
cd "$REPO_SRC"
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

source /home/common.sh
cd "$REPO_SRC"
apply_patch /home/test.patch
run_go_tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

source /home/common.sh
cd "$REPO_SRC"
apply_patch /home/test.patch
apply_patch /home/fix.patch
run_go_tests

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
        hardening = self._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("valyala", "fasthttp_0_to_244")
class Fasthttp0To244(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FasthttpLegacyImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")
        re_pkg = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+/\S+)")

        pending_pass: set[str] = set()
        pending_fail: set[str] = set()
        pending_skip: set[str] = set()

        def flush(pkg: str) -> None:
            for t in pending_pass:
                passed_tests.add(f"{pkg}::{t}")
            for t in pending_fail:
                failed_tests.add(f"{pkg}::{t}")
            for t in pending_skip:
                skipped_tests.add(f"{pkg}::{t}")
            pending_pass.clear()
            pending_fail.clear()
            pending_skip.clear()

        for raw_line in test_log.splitlines():
            line = raw_line.strip()
            m = re_pass.match(line)
            if m:
                pending_pass.add(m.group(1)); continue
            m = re_fail.match(line)
            if m:
                pending_fail.add(m.group(1)); continue
            m = re_skip.match(line)
            if m:
                pending_skip.add(m.group(1)); continue
            m = re_pkg.match(line)
            if m:
                flush(m.group(1))
        flush("unknown")

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined, PIPELINE §11b) ===
# Pre-modules bundles (lead PR <= 244). Data-derived; regenerate if bundles change.
_BUNDLE_NIS_FASTHTTP_LEGACY = [
    "234-280",
]
for _ni in _BUNDLE_NIS_FASTHTTP_LEGACY:
    Instance.register("valyala", _ni)(Fasthttp0To244)
