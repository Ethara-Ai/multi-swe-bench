import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase73to25(Image):
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
        return "rust:1.39.0"

    def image_tag(self) -> str:
        return "base-rust1390-prerust"

    def workdir(self) -> str:
        return "base-rust1390-prerust"

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
{(chr(10) + self.global_env + chr(10)*2) if self.global_env else chr(10)}WORKDIR /home/

RUN echo 'deb http://archive.debian.org/debian buster main' > /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends --allow-unauthenticated git && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --bare https://github.com/rust-lang/crates.io-index.git /opt/crates-index.git && \
    cd /opt/crates-index.git && \
    git clone . /tmp/idx && cd /tmp/idx && \
    head -89 ti/me/time > ti/me/time.tmp && mv ti/me/time.tmp ti/me/time && \
    rm -f ti/me/time-core && touch ti/me/time-core && \
    grep -v '"vers":"2.14.0"' in/de/indexmap > in/de/indexmap.tmp && mv in/de/indexmap.tmp in/de/indexmap && \
    rm -f se/rd/serde_spanned && touch se/rd/serde_spanned && \
    rm -f to/ml/toml_datetime && touch to/ml/toml_datetime && \
    head -50 on/ce/once_cell > on/ce/once_cell.tmp && mv on/ce/once_cell.tmp on/ce/once_cell && \
    head -102 an/yh/anyhow > an/yh/anyhow.tmp && mv an/yh/anyhow.tmp an/yh/anyhow && \
    rm -f ad/le/adler2 && touch ad/le/adler2 && \
    head -35 mi/ni/miniz_oxide > mi/ni/miniz_oxide.tmp && mv mi/ni/miniz_oxide.tmp mi/ni/miniz_oxide && \
    head -30 ei/th/either > ei/th/either.tmp && mv ei/th/either.tmp ei/th/either && \
    head -31 nu/m-/num-traits > nu/m-/num-traits.tmp && mv nu/m-/num-traits.tmp nu/m-/num-traits && \
    head -20 un/ic/unicode-ident > un/ic/unicode-ident.tmp && mv un/ic/unicode-ident.tmp un/ic/unicode-ident && \
    head -47 te/mp/tempfile > te/mp/tempfile.tmp && mv te/mp/tempfile.tmp te/mp/tempfile && \
    rm -f se/rd/serde_repr && touch se/rd/serde_repr && \
    rm -f th/is/thiserror-impl && touch th/is/thiserror-impl && \
    git add -A && git -c user.email=f@l -c user.name=f commit -m f --allow-empty && \
    cd / && rm -rf /opt/crates-index.git && \
    mkdir -p $CARGO_HOME && \
    printf '[source.crates-io]\\nreplace-with = "filtered"\\n[source.filtered]\\nregistry = "file:///tmp/idx"\\n' > $CARGO_HOME/config.toml


{code}
{(chr(10) + self.clear_env) if self.clear_env else ""}
"""


class ImageDefault73to25(Image):
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
        return ImageBase73to25(self.pr, self.config)

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
git fetch origin {pr.base.sha} || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
cargo test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test

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

        return f"""FROM {name}:{tag}
{(chr(10) + self.global_env + chr(10)*2) if self.global_env else chr(10)}{copy_commands}

{prepare_commands}
{(chr(10) + self.clear_env) if self.clear_env else ""}
"""


@Instance.register("tauri-apps", "tauri_73_to_25")
class Tauri73to25(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault73to25(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (.+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (.+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (.+) \.\.\. ignored")]

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
