import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Shared spmc fix commands for v0.12.x.
# spmc 0.2 is completely yanked; 0.3 changes Sender::send() to require &mut self.
_SPMC_FIX = r"""
# Fix spmc: v0.2 is yanked, upgrade to 0.3 which requires &mut self for send().
sed -i 's/spmc = "0.2"/spmc = "0.3"/' Cargo.toml

if [ -f tests/server.rs ]; then
    sed -i "s|tx: &'a spmc::Sender<Reply>|tx: \&'a std::sync::Mutex<spmc::Sender<Reply>>|" tests/server.rs
    sed -i 's|self\.tx\.send(|self.tx.lock().unwrap().send(|g' tests/server.rs
    sed -i 's|reply_tx: spmc::Sender<Reply>|reply_tx: std::sync::Mutex<spmc::Sender<Reply>>|' tests/server.rs
    sed -i 's|reply_tx: reply_tx,|reply_tx: std::sync::Mutex::new(reply_tx),|' tests/server.rs
fi
"""

# Fix for __CReq/__SReq Default impl: newer http crate rejects empty method string.
_DEFAULT_IMPL_FIX = """
# Fix __CReq and __SReq Default impl: newer http crate rejects empty method string.
if [ -f tests/support/mod.rs ]; then
    python3 /home/fix_default_impl.py tests/support/mod.rs
fi
"""

_FIX_DEFAULT_IMPL_PY = (
    "#!/usr/bin/env python3\n"
    "import re\n"
    "import sys\n"
    "\n"
    "filepath = sys.argv[1] if len(sys.argv) > 1 else 'tests/support/mod.rs'\n"
    "\n"
    "with open(filepath, 'r') as f:\n"
    "    content = f.read()\n"
    "\n"
    "original = content\n"
    "\n"
    "for struct_name in ['__CReq', '__SReq']:\n"
    "    has_manual = 'impl Default for ' + struct_name in content\n"
    "\n"
    "    def remove_default_from_derive(m):\n"
    "        derive_list = m.group(1)\n"
    "        parts = [p.strip() for p in derive_list.split(',')]\n"
    "        parts = [p for p in parts if p != 'Default']\n"
    "        return '#[derive(' + ', '.join(parts) + ')]'\n"
    "\n"
    r"    pattern = r'#\[derive\(([^)]*\bDefault\b[^)]*)\)\](?=\npub struct '"
    " + struct_name + "
    r"r'\b)'"
    "\n"
    "    content = re.sub(pattern, remove_default_from_derive, content)\n"
    "\n"
    "    if not has_manual:\n"
    "        impl_block = (\n"
    r"            '\nimpl Default for ' + struct_name + ' {\n'"
    "\n"
    r"            '    fn default() -> Self {\n'"
    "\n"
    r"            '        ' + struct_name + ' {\n'"
    "\n"
    "            '            method: \"GET\",\\n'\n"
    "            '            uri: \"\",\\n'\n"
    r"            '            headers: HeaderMap::new(),\n'"
    "\n"
    r"            '            body: Vec::new(),\n'"
    "\n"
    r"            '        }\n'"
    "\n"
    r"            '    }\n'"
    "\n"
    r"            '}\n'"
    "\n"
    "        )\n"
    r"        pat = r'(pub struct ' + struct_name + r'\b.*?\n\})'"
    "\n"
    "        content = re.sub(pat, lambda m: m.group(0) + impl_block, content, count=1, flags=re.DOTALL)\n"
    "\n"
    "with open(filepath, 'w') as f:\n"
    "    f.write(content)\n"
    "\n"
    "if content != original:\n"
    "    print(f'Fixed Default impls in {filepath}')\n"
    "else:\n"
    "    print(f'No changes needed in {filepath}')\n"
)


class ImageDefault(Image):
    """v0.12.x era (PRs 1913-1538): hyper 0.12.x on rust:1.75 with spmc + Default fixes."""

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
        return "rust:1.75"

    def image_prefix(self) -> str:
        return "envagent"

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
                "fix_default_impl.py",
                _FIX_DEFAULT_IMPL_PY,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
{default_impl_fix}
cargo test --lib --tests -- --skip http2_parallel || true

""".format(pr=self.pr, spmc_fix=_SPMC_FIX, default_impl_fix=_DEFAULT_IMPL_FIX),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
{default_impl_fix}
cargo test --lib --tests -- --skip http2_parallel

""".format(pr=self.pr, spmc_fix=_SPMC_FIX, default_impl_fix=_DEFAULT_IMPL_FIX),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
{default_impl_fix}
cargo test --lib --tests -- --skip http2_parallel

""".format(pr=self.pr, spmc_fix=_SPMC_FIX, default_impl_fix=_DEFAULT_IMPL_FIX),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
export RUSTFLAGS="--cap-lints warn"
{spmc_fix}
{default_impl_fix}
cargo test --lib --tests -- --skip http2_parallel

""".format(pr=self.pr, spmc_fix=_SPMC_FIX, default_impl_fix=_DEFAULT_IMPL_FIX),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM rust:1.75

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git python3

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr, copy_commands=copy_commands)


@Instance.register("hyperium", "hyper_1913_to_1538")
class HYPER_1913_TO_1538(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_pattern = re.compile(r"test (.*) \.\.\. ok")
        failed_pattern = re.compile(r"test (.*) \.\.\. FAILED")
        ignored_pattern = re.compile(r"test (.*) \.\.\. ignored")

        for line in log.splitlines():
            if passed_match := passed_pattern.search(line):
                passed_tests.add(passed_match.group(1).strip())
            elif failed_match := failed_pattern.search(line):
                failed_tests.add(failed_match.group(1).strip())
            elif ignored_match := ignored_pattern.search(line):
                skipped_tests.add(ignored_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
