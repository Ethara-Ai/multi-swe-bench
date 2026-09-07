import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.19-bullseye"

_PACKAGES = ["ca-certificates", "git"]

_LABELS = (
    'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
    '      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
    '      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
    '      org.opencontainers.image.authors="https://www.ethara.ai/"'
)


class PipelineImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            DockerfileEnhancer._TARGETARCH_ARG
            + "\n"
            + f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
            + "ARG BASE_COMMIT\n"
            + "\n"
            + DockerfileEnhancer._PROXY_ARGS,
            DockerfileEnhancer._ENV_BLOCK,
            _LABELS.format(org=self.pr.org, repo=self.pr.repo),
            DockerfileEnhancer._CERT_SYMLINKS,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(
            self._get_apt_update_command(" \\\n    ".join(_PACKAGES), base_img)
        )
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class PipelineImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return PipelineImageBase(self.pr, self.config)

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
set -eo pipefail

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
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go test -v -count=1 -mod=vendor ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
go test -v -count=1 -mod=vendor ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.md' /home/test.patch
go test -v -count=1 -mod=vendor ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='docs/*' --exclude='*.md' /home/test.patch /home/fix.patch
go test -v -count=1 -mod=vendor ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        sections = [f"FROM {base.image_full_name()}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(f"ARG BASE_COMMIT={self.pr.base.sha}")
        sections.append("ENV BASE_COMMIT=${BASE_COMMIT}")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        sections.append(copy_commands.rstrip("\n"))

        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(Image._HARDENING_BLOCK.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("tektoncd", "pipeline")
class Pipeline(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PipelineImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_result = re.compile(r"--- (PASS|FAIL|SKIP): (\S+)")
        re_package = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")
        module_prefix = f"github.com/{self.pr.org}/{self.pr.repo}/"

        def record(name: str, status: str) -> None:
            if status == "PASS":
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            elif status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            else:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        pending = []

        for line in log.splitlines():
            line = line.strip()

            match = re_result.match(line)
            if match:
                pending.append((match.group(2), match.group(1)))
                continue

            match = re_package.match(line)
            if match:
                marker, package = match.group(1), match.group(2)
                if package.startswith(module_prefix):
                    package = package[len(module_prefix):]
                elif package == module_prefix.rstrip("/"):
                    package = "."
                for name, status in pending:
                    record(f"{package}::{name}", status)
                if marker == "FAIL" and not pending:
                    failed_tests.add(package)
                pending = []

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
