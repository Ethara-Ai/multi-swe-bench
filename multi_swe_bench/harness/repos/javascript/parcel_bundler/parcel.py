import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ParcelImageBase(Image):
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
        return "node:20"

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

RUN apt update && apt install -y build-essential python3 git pkg-config libssl-dev

RUN ln -sf /usr/bin/python3 /usr/bin/python
RUN npm install -g cross-env node-gyp@latest

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.77.0
ENV PATH="/root/.cargo/bin:${{PATH}}"
RUN cargo install napi-cli 2>/dev/null || true

RUN corepack enable

{code}

{self.clear_env}

"""


class ParcelImageDefault(Image):
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
        return ParcelImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pin Rust toolchain - the repo's rust-toolchain file says "stable" which
# resolves to latest Rust, which may be too new and break compilation.
# Newer Parcel (v2.13+) uses edition2024 which requires Rust 1.85+
# Older Parcel uses never-type fallback that breaks on Rust 1.78+
if grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/crates/ 2>/dev/null || grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/Cargo.toml 2>/dev/null; then
    echo "1.85.0" > /home/{pr.repo}/rust-toolchain || true
    rustup install 1.85.0 2>/dev/null || true
else
    echo "1.77.0" > /home/{pr.repo}/rust-toolchain || true
fi

corepack enable || true
yarn install || true

# Build native Rust bindings - try multiple strategies
yarn build-native-release 2>&1 || yarn build-native 2>&1 || true

if [ ! -f "/home/{pr.repo}/packages/utils/node-resolver-core/native.js" ] && [ -d "/home/{pr.repo}/packages/utils/node-resolver-core" ]; then
    cd /home/{pr.repo}
    cargo build --release 2>/dev/null || true
    for native_pkg in packages/utils/node-resolver-core packages/core/rust; do
        if [ -d "/home/{pr.repo}/$native_pkg" ] && [ -f "/home/{pr.repo}/$native_pkg/Cargo.toml" ]; then
            cd "/home/{pr.repo}/$native_pkg"
            napi build --release 2>/dev/null || true
            cd /home/{pr.repo}
        fi
    done
fi

if [ -d "/home/{pr.repo}/packages/core/rust" ]; then
    cd /home/{pr.repo}/packages/core/rust
    napi build --release 2>/dev/null || true
    cd /home/{pr.repo}
fi

if [ -d "/home/{pr.repo}/crates/node-bindings" ]; then
    cd /home/{pr.repo}/packages/core/rust 2>/dev/null || cd /home/{pr.repo}
    napi build --platform --release --cargo-cwd /home/{pr.repo}/crates/node-bindings 2>/dev/null || true
    cd /home/{pr.repo}
fi

# Rebuild all native addons via npm to ensure .node binaries exist
npm rebuild 2>/dev/null || true

# Rebuild @parcel/watcher specifically with updated node-gyp
cd /home/{pr.repo}/node_modules/@parcel/watcher 2>/dev/null && node-gyp rebuild 2>/dev/null || true
cd /home/{pr.repo}

yarn build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}

NODE_ENV=test yarn mocha --timeout 60000 || true

# Run integration tests using the ROOT mocha binary (integration-tests dir has no local mocha)
if [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test NODE_OPTIONS=--experimental-vm-modules /home/{pr.repo}/node_modules/.bin/mocha --timeout 120000 || true
    cd /home/{pr.repo}
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch || true

if grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/crates/ 2>/dev/null || grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/Cargo.toml 2>/dev/null; then
    echo "1.85.0" > rust-toolchain || true
    rustup install 1.85.0 2>/dev/null || true
else
    echo "1.77.0" > rust-toolchain || true
fi

yarn install --immutable 2>/dev/null || yarn install || true
npm rebuild 2>/dev/null || true
cd /home/{pr.repo}/node_modules/@parcel/watcher 2>/dev/null && node-gyp rebuild 2>/dev/null || true
cd /home/{pr.repo}

yarn build-native-release 2>&1 || yarn build-native 2>&1 || true
yarn build 2>/dev/null || true

NODE_ENV=test yarn mocha --timeout 60000 || true

# Run integration tests using the ROOT mocha binary
if [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test NODE_OPTIONS=--experimental-vm-modules /home/{pr.repo}/node_modules/.bin/mocha --timeout 120000 || true
    cd /home/{pr.repo}
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true

if grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/crates/ 2>/dev/null || grep -rq 'edition2024\|edition = "2024"' /home/{pr.repo}/Cargo.toml 2>/dev/null; then
    echo "1.85.0" > rust-toolchain || true
    rustup install 1.85.0 2>/dev/null || true
else
    echo "1.77.0" > rust-toolchain || true
fi

yarn install --immutable 2>/dev/null || yarn install || true
npm rebuild 2>/dev/null || true
cd /home/{pr.repo}/node_modules/@parcel/watcher 2>/dev/null && node-gyp rebuild 2>/dev/null || true
cd /home/{pr.repo}

yarn build-native-release 2>&1 || yarn build-native 2>&1 || true
yarn build 2>/dev/null || true

NODE_ENV=test yarn mocha --timeout 60000 || true

# Run integration tests using the ROOT mocha binary
if [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test NODE_OPTIONS=--experimental-vm-modules /home/{pr.repo}/node_modules/.bin/mocha --timeout 120000 || true
    cd /home/{pr.repo}
fi
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

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("parcel-bundler", "parcel")
class ParcelInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ParcelImageDefault(self.pr, self._config)

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

        # Mocha spec reporter output patterns
        # Pass: "  ✓ test name (Xms)" or "  ✓ test name"
        # Fail: "  N) test name" (numbered failures)
        # Pending/Skip: "  - test name"
        passed_res = [
            re.compile(
                r"\s*[\u2714\u2713\u2705]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
            ),
        ]
        failed_res = [
            re.compile(r"^\s*\d+\)\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
        ]
        skipped_res = [
            re.compile(r"\s*-\s+(.*?)$"),
        ]

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            stripped = line.strip()
            if not stripped:
                continue

            for passed_re in passed_res:
                m = passed_re.search(line)
                if m and m.group(1).strip() not in failed_tests:
                    passed_tests.add(m.group(1).strip())

            for failed_re in failed_res:
                m = failed_re.search(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)

            for skipped_re in skipped_res:
                m = skipped_re.search(line)
                if m:
                    test_name = m.group(1).strip()
                    if (
                        test_name
                        and test_name not in passed_tests
                        and test_name not in failed_tests
                    ):
                        skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
