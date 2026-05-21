"""Legacy era config for pre-modules pulumi commits.

Covers v0.16.x and early v0.17.x bundles where the repo used `dep` (Gopkg.toml)
instead of go modules, with a GOPATH workflow:
    /go/src/github.com/pulumi/pulumi

Verified Docker discovery:
- 4 dataset records target SHAs without a `go.mod`: PRs 2074, 2369, 2584, 2613
- `.travis.yml` pins Go 1.9 (PR 2074) or Go 1.11 (PRs 2369/2584/2613)
- Uses `dep ensure -vendor-only` to populate vendor/

The 4 records are release bundles (e.g. v0.16.3..v0.16.4) — their test_patch is
large and their f2p_tests map is empty in the dataset, so even with a perfect
build they cannot reach `valid=true` under gen_report's transition heuristic.
This config exists primarily so era routing is explicit (eliminates the
"no era analysis" verification WARN) and instances run-to-completion rather
than silently routing through the v3 config and producing 0/0/0 reports.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PulumiLegacyImageBase(Image):
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
        # golang:1.13 ships Debian Buster which is EOL — apt-get update fails because
        # the repos moved to archive.debian.org. Use 1.18-bullseye instead: it still
        # supports the GOPATH workflow with GO111MODULE=off, has arm64 manifests, and
        # Bullseye is still on a supported LTS track.
        return "golang:1.18-bullseye"

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

        # The base image installs git/make/curl and the dep tool, then clones the repo
        # INTO the GOPATH layout (/go/src/github.com/pulumi/pulumi). prepare.sh later
        # checks out the PR's base.sha and runs `dep ensure`.
        if self.config.need_clone:
            code = (
                f"RUN mkdir -p /go/src/github.com/{self.pr.org} && "
                f"git clone https://github.com/{self.pr.org}/{self.pr.repo}.git "
                f"/go/src/github.com/{self.pr.org}/{self.pr.repo} && "
                f"ln -sf /go/src/github.com/{self.pr.org}/{self.pr.repo} "
                f"/home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV GOPATH=/go
ENV PATH=/go/bin:$PATH
ENV GO111MODULE=off
ENV CI=true

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
    git make ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Install dep (Go's pre-modules dependency manager).
RUN curl -fsSL -o /go/bin/dep \\
    https://github.com/golang/dep/releases/download/v0.5.4/dep-linux-$(dpkg --print-architecture) && \\
    chmod +x /go/bin/dep

WORKDIR /home/

{code}

{self.clear_env}

"""


class PulumiLegacyImageDefault(Image):
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
        return PulumiLegacyImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-legacy"

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
  echo "check_git_changes: Not inside a git repository"; exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"; exit 1
fi
echo "check_git_changes: No uncommitted changes"; exit 0
""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# Operate inside the GOPATH-linked checkout.
REPO_DIR=/go/src/github.com/{pr.org}/{pr.repo}
cd "$REPO_DIR"
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# If the commit ships with go.mod (post-modules v0.17.8+), prefer modules.
if [ -f go.mod ]; then
    unset GO111MODULE
    # Pulumi v0.17.x go.mod pins gocloud.dev v0.13.0 which transitively requires
    # opencensus-proto@v0.1.0-0.20181214143942-... — that pseudo-version has the
    # form v0.1.0-0.YYYYMMDDHHMMSS which modern Go (>=1.13) rejects as "version
    # before v0.1.0 would have negative patch number". Force-replace the bad
    # versions with newer ones that resolve cleanly before `go mod download`.
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod \\
        go mod edit \\
            -replace=github.com/census-instrumentation/opencensus-proto=github.com/census-instrumentation/opencensus-proto@v0.4.1 \\
            -replace=contrib.go.opencensus.io/exporter/ocagent=contrib.go.opencensus.io/exporter/ocagent@v0.7.0 \\
            -replace=gocloud.dev=gocloud.dev@v0.20.0 2>/dev/null || true
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod tidy 2>/dev/null || true
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod go mod download || true
else
    # dep-era commit. Restore the locked vendor/ tree.
    export GO111MODULE=off
    dep ensure -vendor-only || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /go/src/github.com/{pr.org}/{pr.repo}
if [ -f go.mod ]; then
    unset GO111MODULE
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
      go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./pkg/... ./sdk/...
else
    export GO111MODULE=off
    CI=true go test -vet=off -short -timeout 600s -v -count=1 ./pkg/...
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /go/src/github.com/{pr.org}/{pr.repo}
git apply /home/test.patch || true
if [ -f go.mod ]; then
    unset GO111MODULE
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
      go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./pkg/... ./sdk/...
else
    export GO111MODULE=off
    CI=true go test -vet=off -short -timeout 600s -v -count=1 ./pkg/...
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /go/src/github.com/{pr.org}/{pr.repo}
git apply /home/test.patch /home/fix.patch || true
if [ -f go.mod ]; then
    unset GO111MODULE
    GOTOOLCHAIN=auto GOFLAGS=-mod=mod CI=true \\
      go test -mod=mod -vet=off -short -timeout 600s -v -count=1 ./pkg/... ./sdk/...
else
    export GO111MODULE=off
    CI=true go test -vet=off -short -timeout 600s -v -count=1 ./pkg/...
fi

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("pulumi", "pulumi_2613_to_2074")
class PULUMI_2613_TO_2074(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PulumiLegacyImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        # Strip non-stable Go subtest disambiguation suffixes (#NN).
        re_dup_suffix = re.compile(r"#\d+$")

        def normalize(name: str) -> str:
            return re_dup_suffix.sub("", name)

        for line in test_log.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                name = normalize(m.group(1))
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue
            m = re_fail.match(line)
            if m:
                name = normalize(m.group(1))
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue
            m = re_skip.match(line)
            if m:
                name = normalize(m.group(1))
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
