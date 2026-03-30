import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RustCompilerImageBase(Image):
    """Base image for rust-lang/rust (the Rust compiler itself).

    Uses ubuntu:20.04 with build tools — NOT rust:latest — because this repo
    bootstraps its own compiler via x.py which auto-downloads the correct
    stage0 rustc at build time.
    """

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
        return "ubuntu:20.04"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    cmake \\
    curl \\
    git \\
    libssl-dev \\
    ninja-build \\
    pkg-config \\
    python3 \\
    python3-pip \\
    sudo \\
    wget \\
    && ln -sf /usr/bin/python3 /usr/bin/python \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class RustCompilerImageDefault(Image):
    """Per-PR image for rust-lang/rust.

    Dynamically detects test suites from the test_patch diff headers and
    generates targeted x.py test commands.
    """

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
        return RustCompilerImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_suites(self) -> str:
        """Parse test_patch diff headers to determine which x.py test suites to run.

        Mapping of diff paths to x.py test suite arguments:
          - src/test/ui/         -> src/test/ui       (old layout, pre-2023)
          - tests/ui/            -> tests/ui          (new layout, post-2023)
          - src/test/mir-opt/    -> src/test/mir-opt
          - library/             -> library/std
          - src/tools/clippy/    -> src/tools/clippy
          - src/tools/rustfmt/   -> src/tools/rustfmt

        Falls back to src/test/ui if no suites detected.
        """
        suites = set()
        for line in self.pr.test_patch.splitlines():
            if not line.startswith("diff --git"):
                continue

            # Extract the b/ path from "diff --git a/... b/..."
            parts = line.split(" b/", 1)
            if len(parts) < 2:
                continue
            path = parts[1]

            if path.startswith("src/test/ui/"):
                suites.add("src/test/ui")
            elif path.startswith("tests/ui/"):
                suites.add("tests/ui")
            elif path.startswith("src/test/mir-opt/"):
                suites.add("src/test/mir-opt")
            elif path.startswith("library/"):
                suites.add("library/std")
            elif path.startswith("src/tools/clippy/"):
                suites.add("src/tools/clippy")
            elif path.startswith("src/tools/rustfmt/"):
                suites.add("src/tools/rustfmt")

        if not suites:
            suites.add("src/test/ui")

        return " ".join(sorted(suites))

    def files(self) -> list[File]:
        test_suites = self._test_suites()
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
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python3 x.py test {test_suites} --no-fail-fast 2>&1 || true

""".format(pr=self.pr, test_suites=test_suites),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
python3 x.py test {test_suites} --no-fail-fast 2>&1

""".format(pr=self.pr, test_suites=test_suites),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
python3 x.py test {test_suites} --no-fail-fast 2>&1

""".format(pr=self.pr, test_suites=test_suites),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python3 x.py test {test_suites} --no-fail-fast 2>&1

""".format(pr=self.pr, test_suites=test_suites),
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


@Instance.register("rust-lang", "rust")
class RustCompiler(Instance):
    """Instance handler for rust-lang/rust (the Rust compiler).

    Uses python3 x.py test with targeted test suites instead of cargo test.
    The compiletest harness output format:
      test [ui] src/test/ui/foo.rs ... ok
      test [ui] src/test/ui/bar.rs ... FAILED
      test [ui] src/test/ui/baz.rs ... ignored
    Standard library tests also emit:
      test std::fs::tests::test_name ... ok
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RustCompilerImageDefault(self.pr, self._config)

    # Inject config.toml tweaks:
    #  - verbose-tests = true: compiletest emits "test <name> ... ok" lines (not dots)
    #  - submodules = false: prevent x.py from reverting submodule checkouts after git apply
    _VERBOSE_CFG = (
        'mkdir -p /home/rust && echo -e \\"[rust]\\nverbose-tests = true\\n[build]\\nsubmodules = false\\" '
        '>> /home/rust/config.toml'
    )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f'bash -c "{self._VERBOSE_CFG} && bash /home/run.sh"'

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return f'bash -c "{self._VERBOSE_CFG} && bash /home/test-run.sh"'

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        # git apply silently skips submodule pointer changes (Subproject commit lines).
        # We decode a helper script that parses them from patch files and syncs checkouts.
        # For PRs without submodule changes, the script is a no-op.
        # Base64-encoded script avoids multi-level shell escaping issues.
        b64_sync = (
            "IyEvYmluL2Jhc2gKY2QgL2hvbWUvcnVzdApmb3IgcGF0Y2ggaW4gL2hvbWUvdGVzdC5wYXRjaCAv"
            "aG9tZS9maXgucGF0Y2g7IGRvCiAgYXdrICcvXmRpZmYgLS1naXQgYVwvL3twYXRoPSQzO3N1Yigv"
            "XmFcLy8sIiIscGF0aCl9L15cK1N1YnByb2plY3QgY29tbWl0L3twcmludCBwYXRoLCQzfScgIiRw"
            "YXRjaCIgfAogIHdoaWxlIHJlYWQgc21fcGF0aCBzbV9oYXNoOyBkbwogICAgWyAtbiAiJHNtX3Bh"
            "dGgiIF0gJiYgWyAtbiAiJHNtX2hhc2giIF0gJiYKICAgIChjZCAiJHNtX3BhdGgiICYmIGdpdCBm"
            "ZXRjaCBvcmlnaW4gIiRzbV9oYXNoIiAmJiBnaXQgY2hlY2tvdXQgLWYgIiRzbV9oYXNoIikgfHwg"
            "dHJ1ZQogIGRvbmUKZG9uZQoK"
        )

        return (
            f'bash -c "{self._VERBOSE_CFG} && '
            f'echo {b64_sync} | base64 -d > /home/sync_submodules.sh && chmod +x /home/sync_submodules.sh && '
            f'cd /home/rust && '
            f'git apply --whitespace=nowarn /home/test.patch /home/fix.patch && '
            f'bash /home/sync_submodules.sh && '
            f'eval \\"$(grep ^python3 /home/fix-run.sh)\\""'
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Compiletest format: test [ui] src/test/ui/foo.rs ... ok
        # Standard format:    test std::fs::tests::name ... ok
        # Both share the pattern: test <name> ... ok/FAILED/ignored
        re_pass_tests = [re.compile(r"test (.+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (.+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (.+) \.\.\. ignored")]

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()

            for re_pass in re_pass_tests:
                match = re_pass.search(line)
                if match:
                    test_name = match.group(1).strip()
                    # Dedup: if a test was previously failed, failure wins
                    if test_name not in failed_tests:
                        passed_tests.discard(test_name)
                        passed_tests.add(test_name)

            for re_fail in re_fail_tests:
                match = re_fail.search(line)
                if match:
                    test_name = match.group(1).strip()
                    # Failure always wins over pass
                    passed_tests.discard(test_name)
                    skipped_tests.discard(test_name)
                    failed_tests.add(test_name)

            for re_skip in re_skip_tests:
                match = re_skip.search(line)
                if match:
                    test_name = match.group(1).strip()
                    if test_name not in failed_tests and test_name not in passed_tests:
                        skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
