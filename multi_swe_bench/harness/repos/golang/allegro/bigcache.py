import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GTF_EMIT = (
    'echo "### GTF_BEGIN ###"\n'
    "grep -rIn --include='*_test.go' -E "
    "'^func (Test|Example|Benchmark|Fuzz)[A-Za-z0-9_]*[(]' . 2>/dev/null "
    "| sed -E 's|^[.]/||' "
    "| sed -E 's|^([^:]+):[0-9]+:func ([A-Za-z0-9_]+).*|### GTF \\1 \\2 ###|' "
    "|| true\n"
    'echo "### GTF_END ###"'
)


class BigcacheImageBase(Image):
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
        return "golang:1.13"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class BigcacheImageDefault(Image):
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
        return BigcacheImageBase(self.pr, self.config)

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


@Instance.register("allegro", "bigcache")
class Bigcache(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BigcacheImageDefault(self.pr, self._config)

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
        if not pkg:
            return ""
        marker = f"/{self.pr.repo}/"
        idx = pkg.find(marker)
        if idx != -1:
            rel = pkg[idx + len(marker) :]
        elif pkg.endswith(f"/{self.pr.repo}") or pkg == self.pr.repo:
            rel = ""
        else:
            return pkg
        if re.fullmatch(r"v[0-9]+", rel):
            return ""
        return re.sub(r"^v[0-9]+/", "", rel)

    def parse_log(self, test_log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        gtf_re = re.compile(r"^### GTF (\S+) (\S+) ###$")
        file_map: dict[tuple[str, str], str] = {}
        for line in clean_log.splitlines():
            m = gtf_re.match(line.strip())
            if m:
                relfile, func = m.group(1), m.group(2)
                reldir = relfile.rsplit("/", 1)[0] if "/" in relfile else ""
                file_map[(reldir, func)] = relfile

        for line in clean_log.splitlines():
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
