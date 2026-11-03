import json
import posixpath
import re

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _tidy(dockerfile: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", dockerfile).lstrip("\n")


GO_ENV = """export GOTOOLCHAIN=local
export GOFLAGS=-buildvcs=false
export CGO_ENABLED=1
export GOPROXY=https://proxy.golang.org,direct
export GOSUMDB=sum.golang.org
"""


HARDENING_BLOCK = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


def _filter_binary_patches(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        section
        for section in sections
        if "GIT binary patch" not in section
        and not re.search(r"^Binary files .* differ$", section, re.MULTILINE)
    )


class TailscaleImageBase(Image):
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
        return "golang:1.26.10-bookworm"

    def image_tag(self) -> str:
        return "base-go1.26"

    def workdir(self) -> str:
        return "base-go1.26"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return _tidy(
            f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN GOTOOLCHAIN=auto go mod download || true

{self.clear_env}

CMD ["/bin/bash"]
"""
        )


class TailscaleImageBaseGo127(TailscaleImageBase):
    def dependency(self) -> str | Image:
        return "golang:1.27.3-bookworm"

    def image_tag(self) -> str:
        return "base-go1.27"

    def workdir(self) -> str:
        return "base-go1.27"


class TailscaleImageDefault(Image):
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
        if self.pr.number >= 20829:
            return TailscaleImageBaseGo127(self.pr, self._config)
        return TailscaleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(f"{self.pr.fix_patch}")
        filtered_test_patch = _filter_binary_patches(f"{self.pr.test_patch}")
        if self.pr.number == 21303:
            filtered_fix_patch = "".join(
                section
                for section in re.split(
                    r"(?=^diff --git )", filtered_fix_patch, flags=re.MULTILINE
                )
                if not section.startswith("diff --git a/flakehashes.json ")
            )

        directories = {
            posixpath.dirname(path)
            for path in re.findall(
                r"^diff --git a/.+? b/(.+)$", filtered_test_patch, re.MULTILINE
            )
            if path.endswith(".go") and "/testdata/" not in f"/{path}"
        }
        packages = " ".join(
            sorted(f"./{directory}" if directory else "." for directory in directories)
        )
        select_packages = f"""pkgs=""
for pkg in {packages}; do
  if [ -d "$pkg" ]; then
    pkgs="$pkgs $pkg"
  fi
done
"""
        test_command = f"""{select_packages}if [ -n "$pkgs" ]; then
  go test -json -count=1 -p 1 -timeout=20m $pkgs
fi
"""

        return [
            File(".", "fix.patch", filtered_fix_patch),
            File(".", "test.patch", filtered_test_patch),
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
set -eo pipefail

{go_env}
cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

for attempt in 1 2 3 4 5; do
  if go mod download; then
    break
  fi
  if [ "$attempt" = 5 ]; then
    exit 1
  fi
  sleep 20
done

{select_packages}
if [ -n "$pkgs" ]; then
  go test -count=1 -p 4 -run '^$' -vet=off $pkgs > /dev/null
else
  go build -p 4 ./...
fi
echo DEPS_OK
""".format(pr=self.pr, select_packages=select_packages, go_env=GO_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{go_env}
cd /home/{pr.repo}
{test_command}""".format(pr=self.pr, test_command=test_command, go_env=GO_ENV),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{go_env}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_command}""".format(pr=self.pr, test_command=test_command, go_env=GO_ENV),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
{go_env}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_command}""".format(pr=self.pr, test_command=test_command, go_env=GO_ENV),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return _tidy(
            f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{HARDENING_BLOCK}
{self.clear_env}
"""
        )


@Instance.register("tailscale", "tailscale_21886_to_19329")
class TAILSCALE_21886_TO_19329(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TailscaleImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            package = event.get("Package")
            test = event.get("Test")
            if not package or not test:
                continue
            name = f"{package}::{test}"
            action = event.get("Action")
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            elif action == "skip":
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


Instance.register("tailscale", "tailscale")(TAILSCALE_21886_TO_19329)
