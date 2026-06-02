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
        return "python:3.11-slim"

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
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_NO_INPUT=1
ENV POETRY_VIRTUALENVS_IN_PROJECT=true
ENV POETRY_NO_INTERACTION=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    ca-certificates \\
    pkg-config \\
    libffi-dev \\
    libssl-dev \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry<1.5"

WORKDIR /home/

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

    def dependency(self) -> Optional[Image]:
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

""",
            ),
            File(
                ".",
                "strip_binaries.sh",
                r"""#!/bin/bash
# Drop diff sections for binary files. The dataset's test_patch/fix_patch
# include hunks for PDFs, images, .xlsx, .zip, .db, etc. that lack the
# "full index line" git apply needs for binary blobs, which aborts the
# whole `git apply` and leaves the working tree unpatched. These binary
# files never affect pytest outcomes, so dropping their diff sections is
# safe and turns the apply back into a no-op for them.
awk '
BEGIN { skip = 0 }
/^diff --git / {
  skip = 0
  if ($0 ~ /\.(ico|icns|png|jpe?g|gif|bmp|webp|woff2?|ttf|eot|otf|pdf|zip|tar|tgz|tbz2?|txz|bz2|xz|gz|class|jar|war|ear|enc|gpg|asc|p7s|der|crt|key|pem|sig|odt|ods|odp|docx|xlsx|pptx|msg|vsdx|db|sqlite3?|bin|dat|so|dll|dylib|a|o|obj|exe|wasm|mp[34]|wav|ogg|flac|webm|mov|avi|mkv|ipynb|faiss|pkl|npy|npz|joblib|model|onnx|pt|pth|safetensors|h5|parquet|arrow|feather|index)( |$)/) skip = 1
}
{ if (!skip) print }
' "$1"
""",
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Run pytest unit_tests for every libs/* subpackage that has both a
# pyproject.toml and tests/unit_tests/. Pytest IDs are emitted with a
# package prefix (e.g. "[libs/core] tests/unit_tests/foo.py::bar")
# so parse_log can disambiguate IDs that collide between packages.
set +e

cd /home/{pr.repo}
for pkg in libs/langchain libs/core libs/community libs/experimental libs/text-splitters libs/partners/*; do
    [ -f "$pkg/pyproject.toml" ] || continue
    [ -d "$pkg/tests/unit_tests" ] || continue
    echo "================= PYTEST PKG: $pkg ================="
    pushd "$pkg" >/dev/null || continue
    # Install once per package (idempotent under poetry).
    poetry install --with test --no-interaction >/dev/null 2>&1 || true
    # pytest-socket lives in some libs/* test groups but not others
    # (notably libs/core). Only pass the socket-restriction flags when
    # the plugin is importable, otherwise pytest errors out on unknown
    # CLI args and the whole package's run is lost.
    SOCKET_FLAGS=""
    if poetry run python -c "import pytest_socket" >/dev/null 2>&1; then
        SOCKET_FLAGS="--disable-socket --allow-unix-socket"
    fi
    # -o addopts= overrides per-package inifile addopts (snapshot/socket/etc.)
    # so pytest doesn't error on flags whose plugin failed to install.
    poetry run pytest -o addopts= --no-header -rA --tb=no -p no:cacheprovider \\
        --continue-on-collection-errors $SOCKET_FLAGS tests/unit_tests/ 2>&1 \\
        | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\\1 [$pkg] #; s#^(SKIPPED \\[[0-9]+\\])[[:space:]]+#\\1 [$pkg] #"
    popd >/dev/null
done
exit 0
""".format(pr=self.pr),
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

# Pre-install langchain + every libs/* subpackage that exists so the run
# scripts find cached venvs. Failures are tolerated because optional
# extras frequently fail on stripped images. Pip-based fallbacks ensure
# the project + its core test plugins are still installed when a single
# optional sub-dep build fails midway.
for pkg in libs/langchain libs/core libs/community libs/experimental libs/text-splitters libs/partners/*; do
    [ -f "$pkg/pyproject.toml" ] || continue
    [ -d "$pkg/tests/unit_tests" ] || continue
    (
        cd "$pkg"
        poetry install --with test --no-interaction || true
        poetry run pip install -e . --no-deps 2>/dev/null || true
        # Only install pytest plugins if missing; pin pytest<8 so the
        # resolver doesn't pull in pytest 9 (whose deprecations error out
        # under the era's PytestRemovedIn9Warning-as-error suite).
        if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
            # pytest-asyncio<0.24 is the last release compatible with pytest<8;
            # later pytest-asyncio versions need pytest>=8.2 and would pull
            # pytest 9 back in, re-triggering the deprecation errors.
            poetry run pip install "pytest<8" "pytest-asyncio<0.24" \
                pytest-socket pytest-mock pytest-cov pytest-dotenv freezegun \
                responses syrupy 2>/dev/null || true
        fi
        poetry run pip install -e . 2>/dev/null || true
    )
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /tmp/test.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
bash /home/strip_binaries.sh /home/fix.patch  > /tmp/fix.filtered.patch
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /tmp/test.filtered.patch /tmp/fix.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh

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

@Instance.register("langchain-ai", "langchain")
class Langchain(Instance):
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

        # Pytest with -rA emits parametrized test ids that may contain spaces
        # (e.g. test_foo[foo bar baz]), so capture everything up to the
        # optional " - <reason>" trailer rather than the next whitespace.
        re_pass = re.compile(r"^PASSED\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_error = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s.*)?\s*$")
        # SKIPPED format: "SKIPPED [N] [optional-pkg-prefix] file:line: reason"
        # — keep "(pkg )?file:line" as the unique identifier so per-line skips
        # in the same file don't collapse, and same-name files across libs/*
        # subpackages remain distinct.
        re_skip = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+((?:\[\S+?\]\s+)?\S+?:\d+)(?::\s.*)?\s*$"
        )
        re_xfail = re.compile(r"^XFAIL\s+(.+?)\s*$")
        re_xpass = re.compile(r"^XPASS\s+(.+?)\s*$")

        for raw in test_log.splitlines():
            line = raw.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_error.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_xfail.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_xpass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

        common_pf = passed_tests & failed_tests
        passed_tests -= common_pf
        common_ps = passed_tests & skipped_tests
        skipped_tests -= common_ps
        common_fs = failed_tests & skipped_tests
        failed_tests -= common_fs

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
