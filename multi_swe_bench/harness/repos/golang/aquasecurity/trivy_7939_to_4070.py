import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/aquasecurity/trivy/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Names are kept package-qualified
    (`pkg/path::TestName`); subtests appear as `TestName/sub`."""
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
            pkg = pkg[len(_REPO_PREFIX):]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


class TrivyEra2ImageBase(Image):
    """trivy era 2 (PRs 4070-7939, v0.42->v0.57, 2023-mid-2024). go.mod
    `go 1.19`-`go 1.22`. Post-mage cutover. Base golang:1.24 covers the era's
    go.mod range and provides wasip1 toolchain for pkg/module/ wasm fixtures."""

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
        return "golang:1.24"

    def image_tag(self) -> str:
        return "base-go124"

    def workdir(self) -> str:
        return "base-go124"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # Base image hardening: removes remote and all refs so the image cannot
        # be used to recover branch/tag history, but does NOT run gc/repack so
        # all commit objects remain available for per-PR `git checkout <sha>`.
        base_hardening = (
            "RUN set -eux; \\\n"
            "    git checkout --detach HEAD; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            "    git config --local remote.pushDefault \"\"; \\\n"
            "    test -z \"$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)\"; \\\n"
            "    test -z \"$(git remote)\""
        )

        base_hardening_submodules = (
            "RUN if [ -f .gitmodules ]; then \\\n"
            "        git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            "            git for-each-ref --format=\"%(refname)\" refs/heads refs/remotes refs/tags refs/replace \\\n"
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            git gc --prune=now; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            label,
            "ENV CGO_ENABLED=0 \\\n    GOTOOLCHAIN=local",
            "ENV GOFLAGS=-buildvcs=false -mod=mod",
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            base_hardening,
            base_hardening_submodules,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class TrivyEra2ImageDefault(Image):
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
        return TrivyEra2ImageBase(self.pr, self._config)

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
timeout 600 go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE 'pkg/fanal/test/integration/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
if echo "$TEST_DIRS" | grep -q '^pkg/module'; then
    if [ -d pkg/module/testdata ]; then
        ( GOOS=wasip1 GOARCH=wasm go generate ./pkg/module/testdata/... ) 2>&1 || true
    fi
fi
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
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.wasm' \
    --exclude='*.bin' --exclude='*.test' --exclude='*.rpm' --exclude='*.deb' \
    --exclude='*.tgz' --exclude='*.jar' --exclude='*.war' --exclude='*.par' \
    --exclude='*.db' --exclude='*.zst' --exclude='*.tfplan' --exclude='*.exe' \
    --exclude='*.elf' --exclude='*.macho' --exclude='*.egg' --exclude='*.vmdk' \
    --exclude='executable_*')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE 'pkg/fanal/test/integration/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
if echo "$TEST_DIRS" | grep -q '^pkg/module'; then
    if [ -d pkg/module/testdata ]; then
        ( GOOS=wasip1 GOARCH=wasm go generate ./pkg/module/testdata/... ) 2>&1 || true
    fi
fi
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
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.wasm' \
    --exclude='*.bin' --exclude='*.test' --exclude='*.rpm' --exclude='*.deb' \
    --exclude='*.tgz' --exclude='*.jar' --exclude='*.war' --exclude='*.par' \
    --exclude='*.db' --exclude='*.zst' --exclude='*.tfplan' --exclude='*.exe' \
    --exclude='*.elf' --exclude='*.macho' --exclude='*.egg' --exclude='*.vmdk' \
    --exclude='executable_*')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    timeout 600 go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
    | grep -vE 'pkg/fanal/test/integration/' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
if echo "$TEST_DIRS" | grep -q '^pkg/module'; then
    if [ -d pkg/module/testdata ]; then
        ( GOOS=wasip1 GOARCH=wasm go generate ./pkg/module/testdata/... ) 2>&1 || true
    fi
fi
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

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_command}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
"""


@Instance.register("aquasecurity", "trivy_7939_to_4070")
class TRIVY_7939_TO_4070(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TrivyEra2ImageDefault(self.pr, self._config)

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
