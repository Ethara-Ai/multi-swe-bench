import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase8009to6329(Image):
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
        return "rust:1.70.0-bookworm"

    def image_tag(self) -> str:
        return "base-rust1700-v2noxdo"

    def workdir(self) -> str:
        return "base-rust1700-v2noxdo"

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

RUN apt-get update && \
    apt-get install -y --no-install-recommends libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev && \
    rm -rf /var/lib/apt/lists/*

RUN ARCH=$(uname -m) && case "$ARCH" in x86_64) NODE_ARCH=x64;; aarch64) NODE_ARCH=arm64;; *) exit 1;; esac && \
    curl -fsSL https://nodejs.org/dist/v18.20.8/node-v18.20.8-linux-$NODE_ARCH.tar.xz | tar -xJ -C /usr/local --strip-components=1

RUN git clone --bare https://github.com/rust-lang/crates.io-index.git /opt/crates-index.git && \
    cd /opt/crates-index.git && \
    git clone . /tmp/idx && cd /tmp/idx && \
    grep -v '"vers":"0.3.47"' ti/me/time > ti/me/time.tmp && mv ti/me/time.tmp ti/me/time && \
    grep -v '"vers":"0.1.8"' ti/me/time-core > ti/me/time-core.tmp && mv ti/me/time-core.tmp ti/me/time-core && \
    grep -v '"vers":"2.14.0"' in/de/indexmap > in/de/indexmap.tmp && mv in/de/indexmap.tmp in/de/indexmap && \
    grep -v '"vers":"1.1.[01]"' se/rd/serde_spanned > se/rd/serde_spanned.tmp && mv se/rd/serde_spanned.tmp se/rd/serde_spanned && \
    grep -v '"vers":"1.1.[01]"' to/ml/toml_datetime > to/ml/toml_datetime.tmp && mv to/ml/toml_datetime.tmp to/ml/toml_datetime && \
    git add -A && git -c user.email=f@l -c user.name=f commit -m f --allow-empty && \
    cd / && rm -rf /opt/crates-index.git && \
    mkdir -p $CARGO_HOME && \
    printf '[source.crates-io]\\nreplace-with = "filtered"\\n[source.filtered]\\nregistry = "file:///tmp/idx"\\n' > $CARGO_HOME/config.toml

{code}
{(chr(10) + self.clear_env) if self.clear_env else ""}
"""


class ImageDefault8009to6329(Image):
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
        return ImageBase8009to6329(self.pr, self.config)

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

npm install || true
cargo test -p tauri || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cargo test -p tauri

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
cargo test -p tauri

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test -p tauri

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


@Instance.register("tauri-apps", "tauri_8009_to_6329")
class Tauri8009to6329(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault8009to6329(self.pr, self._config)

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
