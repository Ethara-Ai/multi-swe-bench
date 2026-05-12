import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "gcc:12"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt update && apt install -y meson ninja-build pkg-config git python3

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./configure --prefix=/usr
make -j$(nproc)
make install
cd test
r2r -V db/
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./configure --prefix=/usr
make -j$(nproc)
make install
cd test
r2r -V db/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./configure --prefix=/usr
make -j$(nproc)
make install
cd test
r2r -V db/

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

        # Fetch capstone dependency after prepare.sh checkouts the correct base commit.
        # shlr/Makefile defines CS_TIP (commit) and CS_BRA (branch) for the correct capstone version.
        # We clone capstone, checkout the pinned commit, apply radare2's patches, and fix headers.
        capstone_commands = """RUN cd /home/{pr.repo}/shlr && \
  if [ ! -d capstone/.git ]; then \
    CS_BRA=$(grep '^CS_BRA=' Makefile | head -1 | cut -d= -f2) && \
    CS_TIP=$(grep '^CS_TIP=' Makefile | head -1 | cut -d= -f2) && \
    git clone --depth 1 https://github.com/capstone-engine/capstone.git capstone -b "${{CS_BRA}}" && \
    cd capstone && \
    git fetch --depth 1 origin "${{CS_TIP}}" && \
    git checkout "${{CS_TIP}}" && \
    cd .. && \
    if [ -d capstone-patches/v5 ]; then \
      for p in capstone-patches/v5/*.patch; do \
        (cd capstone && patch -p1 < "../$p") || true; \
      done; \
    fi && \
    mkdir -p capstone/include/capstone && \
    cp -rf capstone/include/*.h capstone/include/capstone/; \
  fi""".format(pr=self.pr)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{capstone_commands}

{self.clear_env}

"""


@Instance.register("radareorg", "radare2_17535_to_99999")
class Radare2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        # r2r output contains ANSI codes; strip before parsing
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # r2r -V format: [STATUS] db/path test_name
        # [OK]/[FX] = passed, [XX]/[BR] = failed, [SK] = skipped
        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            ok_match = re.match(r"^\[OK\]\s+(\S+)\s+(.*)", line)
            if ok_match:
                passed_tests.add(f"{ok_match.group(1)} {ok_match.group(2).strip()}")
                continue

            xx_match = re.match(r"^\[XX\]\s+(\S+)\s+(.*)", line)
            if xx_match:
                failed_tests.add(f"{xx_match.group(1)} {xx_match.group(2).strip()}")
                continue

            br_match = re.match(r"^\[BR\]\s+(\S+)\s+(.*)", line)
            if br_match:
                failed_tests.add(f"{br_match.group(1)} {br_match.group(2).strip()}")
                continue

            sk_match = re.match(r"^\[SK\]\s+(\S+)\s+(.*)", line)
            if sk_match:
                skipped_tests.add(f"{sk_match.group(1)} {sk_match.group(2).strip()}")
                continue

            fx_match = re.match(r"^\[FX\]\s+(\S+)\s+(.*)", line)
            if fx_match:
                passed_tests.add(f"{fx_match.group(1)} {fx_match.group(2).strip()}")
                continue

        # Dedup: if a test appears in both passed and failed, keep it as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
