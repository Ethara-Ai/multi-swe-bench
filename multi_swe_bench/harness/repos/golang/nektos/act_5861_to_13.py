import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ActImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # DockerfileEnhancer in image.py injects proxy / MITM / CA-cert
    # infrastructure into every generated Dockerfile. The ECR images for this
    # repo must NOT carry that MITM proxy / cert wiring, so we emit a
    # fully-formed Dockerfile here that carries the BuildKit syntax directive.
    # DockerfileEnhancer.enhance() returns the Dockerfile untouched when that
    # directive is already present, so its proxy ARGs, proxy ENV vars, CA-cert
    # symlinks and MITM secret mount are all skipped. We re-emit only the
    # non-proxy infra (REPO_URL / BASE_COMMIT ARGs + image labels + plain env).
    _SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"

    # NOTE: GOPROXY is intentionally NOT pinned here. This ENV applies to BOTH
    # the build (prepare.sh warms the cache and needs the real proxy) and the
    # offline eval. GOPROXY=off is therefore set per-script: real proxy in
    # prepare.sh, "off" in the run/test/fix eval scripts.
    _ENV_BLOCK = (
        "ENV DEBIAN_FRONTEND=noninteractive \\\n"
        "    LANG=C.UTF-8 \\\n"
        "    GOFLAGS=-mod=mod \\\n"
        "    GOTOOLCHAIN=local \\\n"
        "    TZ=UTC"
    )

    def dockerfile(self) -> str:
        raw = super().dockerfile()

        org = self.pr.org
        repo = self.pr.repo
        github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo
        repo_url = f"https://github.com/{org}/{github_repo}.git"

        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="{repo_url}"\n'
            "ARG BASE_COMMIT"
        )
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )
        infra = "\n\n".join([build_args, self._ENV_BLOCK, label_block]) + "\n"

        lines = raw.split("\n")
        from_idx = next(
            (i for i, l in enumerate(lines) if l.strip().upper().startswith("FROM ")),
            None,
        )
        if from_idx is None:
            return raw

        result = [self._SYNTAX_DIRECTIVE, ""]
        result.extend(lines[:from_idx])
        result.append(lines[from_idx].strip())
        result.append("")
        result.append(infra)
        result.extend(lines[from_idx + 1 :])
        return "\n".join(result)

    def extra_packages(self) -> list[str]:
        # act drives GitHub Actions through the Docker engine, so the daemon CLI
        # is installed alongside the default toolchain (git is already in the
        # base package set in Image.dockerfile()).
        return ["docker.io"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the runtime helper scripts + patches into /home/ and
        # warms the Go module/build caches so the eval scripts run offline. The
        # copied files live outside /home/{repo}, so the hardening pass (which
        # only operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# warms the Go module/build caches for BOTH the base AND the post-patch graph
# so the offline eval never needs the network or `go mod tidy`.
set -e

# Build time: network IS available, so use a real proxy to warm the cache.
export GOPROXY=https://proxy.golang.org,direct
export GOFLAGS=-mod=mod

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

# 1) Base graph
go mod download -x 2>&1 || true
go build -buildvcs=false -mod=mod ./... 2>&1 || true

# 2) Post-patch graph (network available at build time): refresh go.mod/go.sum
#    and pull every module the test+fix build needs, then restore the base tree.
git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true
go mod download all 2>&1 || true
go mod tidy 2>&1 || true
git checkout -- . 2>&1 || true
git clean -fd 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Start Docker daemon if socket available, otherwise skip
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Reset go.mod/go.sum to the clean base before applying patches
git checkout -- go.mod go.sum 2>/dev/null || true

# Apply test patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || true

# Patches carry a complete, consistent go.mod/go.sum (verified: the new direct
# deps are already hashed in the base go.sum). Do NOT rm go.sum + `go mod tidy`
# here -- the eval is offline, tidy fails silently and leaves an incomplete
# go.sum, which makes `go test` report "[build failed]".

# Start Docker daemon if socket available
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

# Eval is offline: force module resolution to the build-warmed cache only.
export GOPROXY=off

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}

# Reset go.mod/go.sum to the clean base before applying patches
git checkout -- go.mod go.sum 2>/dev/null || true

# Apply test patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/test.patch 2>&1 || true

# Apply fix patch with fallbacks
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' /home/fix.patch 2>&1 || true

# Patches carry a complete, consistent go.mod/go.sum (verified: the new direct
# deps are already hashed in the base go.sum). Do NOT rm go.sum + `go mod tidy`
# here -- the eval is offline, tidy fails silently and leaves an incomplete
# go.sum, which makes `go test` report "[build failed]".

# Start Docker daemon if socket available
if [ -e /var/run/docker.sock ]; then
  echo "Docker socket detected"
fi

go test -buildvcs=false -mod=mod -count=1 -short -timeout 300s -vet=off ./... 2>&1

""".format(pr=self.pr),
            ),
        ]


# Routing key. `Instance.create` looks up `nektos/<number_interval>`, so each
# bundle's `number_interval` must match a registered key. The original era key
# `act_5861_to_13` is kept so the existing 61-bundle dataset (every row still
# carries that value) keeps routing here. Stacked below it are the per-bundle
# dash-joined `prs_in_bundle` keys (the canonical number_interval format,
# e.g. 514-537-566-...); decorators stack because `Instance.register` returns
# the class unchanged, so the same image class answers to every listed key.
@Instance.register("nektos", "514-537-566-593-594-595-596-600-601-604-605-606-608-619-620-624-628-630-633-635-637-643-648-650-654-658-659-660-661-662-663-665-667-670-672-674-675")
@Instance.register("nektos", "act_5861_to_13")
class Act(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ActImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^\s*--- PASS: (\S+)"),
            re.compile(r"^ok\s+(\S+)\s+"),
        ]
        re_fail_tests = [
            re.compile(r"^\s*--- FAIL: (\S+)"),
            re.compile(r"^FAIL\s+(\S+)"),
        ]
        re_skip_tests = [
            re.compile(r"^\s*--- SKIP: (\S+)"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        # Failed takes priority over passed/skipped
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
