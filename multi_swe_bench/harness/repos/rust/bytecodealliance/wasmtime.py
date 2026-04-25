import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Rust version configuration
# ---------------------------------------------------------------------------

# Maps release_line -> minimum Rust version required (from Cargo.toml analysis).
# Early releases (pre-8.0) had no rust-version field; we pick a version that
# satisfies the edition requirement (2018 or 2021).
_RELEASE_LINE_TO_RUST_VERSION: dict[str, str] = {
    # Edition 2018 / no edition — no MSRV specified
    "unknown": "1.60.0",
    "0.26":    "1.60.0",
    "0.32":    "1.60.0",
    "0.35":    "1.60.0",
    # Edition 2021 — no MSRV specified
    "0.38":    "1.65.0",
    "0.39":    "1.65.0",
    "0.40":    "1.65.0",
    "1.0":     "1.65.0",
    "2.0":     "1.65.0",
    # Explicit rust-version in Cargo.toml
    "8.0":     "1.66.0",
    "9.0":     "1.66.0",
    "10.0":    "1.66.0",
    "11.0":    "1.66.0",
    "12.0":    "1.66.0",
    "14.0":    "1.70.0",
    "17.0":    "1.73.0",
    "18.0":    "1.73.0",
    "19.0":    "1.73.0",
    "21.0":    "1.75.0",
    "22.0":    "1.76.0",
    "23.0":    "1.77.0",
    "24.0":    "1.78.0",
    "25.0":    "1.78.0",
    "28.0":    "1.81.0",
    "29.0":    "1.81.0",
    "30.0":    "1.82.0",
    "32.0":    "1.84.0",
    "33.0":    "1.84.0",
    "34.0":    "1.85.0",
    "36.0":    "1.86.0",
    "37.0":    "1.87.0",
    "38.0":    "1.88.0",
    "39.0":    "1.89.0",
    "40.0":    "1.89.0",
    "41.0":    "1.90.0",
    "42.0":    "1.91.0",
}

_DEFAULT_RUST_VERSION = "1.80.0"


def _get_release_line(pr: PullRequest) -> str:
    ref = pr.base.ref if pr.base else ""
    if ref.startswith("release-"):
        # "release-42.0.0" -> "42.0", "release-0.32.x" -> "0.32"
        ver = ref.replace("release-", "")
        parts = ver.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return ver
    if ref.startswith("stable-v"):
        # "stable-v0.26" -> "0.26"
        return ref.replace("stable-v", "")
    return "unknown"


def _resolve_rust_version(pr: PullRequest) -> str:
    rl = _get_release_line(pr)
    return _RELEASE_LINE_TO_RUST_VERSION.get(rl, _DEFAULT_RUST_VERSION)


# ---------------------------------------------------------------------------
# Shared script fragments
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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
"""

# Patch application with fallback chain (mirrors etcd.py)
def _apply_patch_cmd(patch_files: list[str]) -> str:
    cmds = []
    for pf in patch_files:
        cmds.append(
            f'if [ -s "{pf}" ]; then\n'
            f'    git apply --whitespace=nowarn "{pf}" || \\\n'
            f'    git apply --whitespace=nowarn --3way "{pf}" || \\\n'
            f'    (git apply --whitespace=nowarn --reject "{pf}" && find . -name "*.rej" -delete) || \\\n'
            f'    patch --batch --fuzz=5 -p1 < "{pf}" || \\\n'
            f'    {{ echo "ERROR: Failed to apply {pf}"; exit 1; }}\n'
            f"fi"
        )
    return "\n".join(cmds)


# ---------------------------------------------------------------------------
# Image classes
# ---------------------------------------------------------------------------

class WasmtimeImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config, rust_version: str):
        self._pr = pr
        self._config = config
        self._rust_version = rust_version

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return f"rust:{self._rust_version}"

    def image_tag(self) -> str:
        return f"base-rust{self._rust_version.replace('.', '_')}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        rust_image = self.dependency()

        if self.config.need_clone:
            clone_code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git"
                f" /home/{self.pr.repo}"
            )
        else:
            clone_code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # Install wasm targets. Try all variants; ignore failures for targets
        # unavailable on older toolchains.
        # - wasm32-unknown-unknown: available since Rust 1.30
        # - wasm32-wasi: available < 1.78, renamed to wasm32-wasip1
        # - wasm32-wasip1: available >= 1.78
        # - wasm32-wasip2: available >= 1.82
        add_targets = (
            "RUN rustup target add wasm32-unknown-unknown 2>/dev/null || true && \\\n"
            "    rustup target add wasm32-wasi 2>/dev/null || true && \\\n"
            "    rustup target add wasm32-wasip1 2>/dev/null || true && \\\n"
            "    rustup target add wasm32-wasip2 2>/dev/null || true"
        )

        return f"""FROM {rust_image}

{self.global_env}

RUN apt-get update && \\
    apt-get install -y --no-install-recommends cmake patch && \\
    rm -rf /var/lib/apt/lists/*

{add_targets}

WORKDIR /home/

{clone_code}

{self.clear_env}

"""


class WasmtimeImageDefault(Image):

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
        rust_version = _resolve_rust_version(self.pr)
        return WasmtimeImageBase(self.pr, self.config, rust_version)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "\n"
                    f"cd /home/{self.pr.repo}\n"
                    "git reset --hard\n"
                    "bash /home/check_git_changes.sh\n"
                    f"git checkout {self.pr.base.sha}\n"
                    "bash /home/check_git_changes.sh\n"
                    "\n"
                    "git submodule update --init --recursive 2>/dev/null || true\n"
                    "\n"
                    "cargo test || true\n"
                ),
            ),
            File(
                ".",
                "run.sh",
                (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "\n"
                    f"cd /home/{self.pr.repo}\n"
                    "cargo test\n"
                ),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "\n"
                    f"cd /home/{self.pr.repo}\n"
                    + _apply_patch_cmd(["/home/test.patch"])
                    + "\n"
                    "cargo test\n"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    "#!/bin/bash\n"
                    "set -e\n"
                    "\n"
                    f"cd /home/{self.pr.repo}\n"
                    + _apply_patch_cmd(["/home/test.patch"])
                    + "\n"
                    + _apply_patch_cmd(["/home/fix.patch"])
                    + "\n"
                    "cargo test\n"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base_image = self.dependency()
        name = base_image.image_name()
        tag = base_image.image_tag()

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


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("bytecodealliance", "wasmtime")
class Wasmtime(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WasmtimeImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"test (\S+)(?: - should panic)? \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+)(?: - should panic)? \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+)(?: - should panic)? \.\.\. ignored")]

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
