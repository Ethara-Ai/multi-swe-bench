import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Shell snippet emitted by every run script. `go test -json` reports each
# test's Package and Test name but NOT its source file, so we print a
# test->file map (scanned AFTER any patch is applied, so newly-added tests
# are included). parse_log reads these `### GTF <relfile> <func> ###` lines
# to render ids as `<relfile>::<Test>`, e.g.
# `infrastructure/grpc/server_test.go::TestServer_CreateMicroVM`.
_GTF_EMIT = (
    'echo "### GTF_BEGIN ###"\n'
    "grep -rIn --include='*_test.go' -E "
    "'^func (Test|Example|Benchmark|Fuzz)[A-Za-z0-9_]*[(]' . 2>/dev/null "
    "| sed -E 's|^[.]/||' "
    "| sed -E 's|^([^:]+):[0-9]+:func ([A-Za-z0-9_]+).*|### GTF \\1 \\2 ###|' "
    "|| true\n"
    'echo "### GTF_END ###"'
)


class FlintlockImageBase(Image):
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
        # flintlock's go.mod at the target commit declares `go 1.17` (module
        # github.com/weaveworks/flintlock). Match the toolchain. NOTE: do NOT
        # set GOFLAGS=-buildvcs=false here — the -buildvcs flag was added in
        # Go 1.18 and errors on 1.17.
        return "golang:1.17-bullseye"

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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    CGO_ENABLED=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git make \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class FlintlockImageDefault(Image):
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
        return FlintlockImageBase(self.pr, self.config)

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

go mod download || true
go test -count=1 -timeout 20m ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{gtf}
go test -json -count=1 -timeout 20m ./...

""".format(pr=self.pr, gtf=_GTF_EMIT),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{gtf}
go test -json -count=1 -timeout 20m ./...

""".format(pr=self.pr, gtf=_GTF_EMIT),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{gtf}
go test -json -count=1 -timeout 20m ./...

""".format(pr=self.pr, gtf=_GTF_EMIT),
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


@Instance.register("liquidmetal-dev", "flintlock")
class Flintlock(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlintlockImageDefault(self.pr, self._config)

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

    def _rel_pkg(self, pkg: str) -> str:
        """Reduce a Go import path to a repo-relative package directory.

        The module path is `github.com/weaveworks/flintlock`, so
        `github.com/weaveworks/flintlock/infrastructure/grpc` ->
        `infrastructure/grpc`. Keyed on the repo name marker `/flintlock/`.
        """
        if not pkg:
            return ""
        marker = f"/{self.pr.repo}/"
        idx = pkg.find(marker)
        if idx != -1:
            return pkg[idx + len(marker):]
        if pkg.endswith(f"/{self.pr.repo}") or pkg == self.pr.repo:
            return ""
        return pkg

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI colour codes first so matching is robust even if the
        # toolchain emits coloured banner lines alongside the -json stream.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Pass 1: build a (package_dir, func_name) -> source_file map from the
        # `### GTF <relfile> <func> ###` lines emitted by the run script.
        gtf_re = re.compile(r"^### GTF (\S+) (\S+) ###$")
        file_map: dict[tuple[str, str], str] = {}
        for line in test_log.splitlines():
            m = gtf_re.match(line.strip())
            if m:
                relfile, func = m.group(1), m.group(2)
                reldir = relfile.rsplit("/", 1)[0] if "/" in relfile else ""
                file_map[(reldir, func)] = relfile

        # Pass 2: parse `go test -json` events. Each per-test event has both
        # Package and Test; resolve the source file so the id reads as
        # `<relfile>::<Test>` (falling back to `<package_dir>::<Test>` if the
        # file is unknown). Package-level events (no Test) are ignored.
        for line in test_log.splitlines():
            line = line.strip()
            if not line.startswith("{") or '"Action"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue

            test_name = event.get("Test")
            if not test_name:
                continue
            action = event.get("Action")
            if action not in ("pass", "fail", "skip"):
                continue

            reldir = self._rel_pkg(event.get("Package", ""))
            base_func = test_name.split("/", 1)[0]
            relfile = file_map.get((reldir, base_func))
            if relfile:
                test_id = f"{relfile}::{test_name}"
            elif reldir:
                test_id = f"{reldir}::{test_name}"
            else:
                test_id = test_name

            if action == "pass":
                passed_tests.add(test_id)
            elif action == "fail":
                failed_tests.add(test_id)
            elif action == "skip":
                skipped_tests.add(test_id)

        # Enforce TestResult invariants: the three sets must be disjoint.
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
