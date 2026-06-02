import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

class LangchainRootImageBase(Image):
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
        return "base-root"

    def workdir(self) -> str:
        return "base-root"

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

class LangchainRootImageDefault(Image):
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
        return LangchainRootImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        # gen_report.py:357 only collects workdirs whose name starts with
        # "pr-", so we prefix with pr- and suffix -root for era disambiguation.
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
# Discover EVERY tests/unit_tests/ directory in the repo after patches and
# run pytest against each from a venv that owns the working langchain
# install. The "set up experimental" PR's fix_patch restructures the repo
# (moves langchain/ -> libs/langchain/ + adds libs/experimental/) so we
# can't bake one TEST_ROOT into the script.
#
# Strategy:
#   1. Pick the venv directory whose poetry env has langchain importable.
#      That's always /home/{pr.repo}/.venv on the pre-restructure base,
#      and after fix_patch it may need re-creation in libs/langchain/.
#   2. From that venv, run pytest against every tests/unit_tests/ dir.
#   3. Prefix output lines with [pkg] so parse_log keeps IDs unique.
set +e

# Find the venv that has langchain installed. Default to /home/{pr.repo}.
VENV_ROOT=/home/{pr.repo}
for candidate in /home/{pr.repo} /home/{pr.repo}/libs/langchain; do
    [ -d "$candidate/.venv" ] || continue
    if "$candidate/.venv/bin/python" -c "import langchain" >/dev/null 2>&1; then
        VENV_ROOT="$candidate"
        break
    fi
done

# If no working venv exists yet (fix_patch restructure left libs/langchain
# without one), bootstrap it now.
if [ -f /home/{pr.repo}/libs/langchain/pyproject.toml ] && \\
   ! "$VENV_ROOT/.venv/bin/python" -c "import langchain" >/dev/null 2>&1; then
    (
        cd /home/{pr.repo}/libs/langchain
        poetry install --with test --no-interaction >/dev/null 2>&1 || true
        poetry run pip install -e . --no-deps >/dev/null 2>&1 || true
        if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
            poetry run pip install "pytest<8" "pytest-asyncio<0.24" \\
                pytest-socket pytest-mock pytest-cov pytest-dotenv \\
                freezegun responses syrupy >/dev/null 2>&1 || true
        fi
        poetry run pip install -e . >/dev/null 2>&1 || true
    )
    VENV_ROOT=/home/{pr.repo}/libs/langchain
fi

PYTEST="$VENV_ROOT/.venv/bin/pytest"
PYBIN="$VENV_ROOT/.venv/bin/python"
[ ! -x "$PYTEST" ] && PYTEST=$(command -v pytest)

# pytest-socket support is per-venv.
SOCKET_FLAGS=""
"$PYBIN" -c "import pytest_socket" >/dev/null 2>&1 && \\
    SOCKET_FLAGS="--disable-socket --allow-unix-socket"

ran_any=0
while IFS= read -r tdir; do
    # tdir is .../X/tests/unit_tests — we want X as the parent and the
    # relative tests/unit_tests/ as the pytest target.
    parent=$(dirname "$(dirname "$tdir")")
    pkg=$(echo "$parent" | sed "s#^/home/{pr.repo}/##; s#^/home/{pr.repo}#root#")
    [ -z "$pkg" ] && pkg=root
    echo "================= PYTEST PKG: $pkg ================="
    (
        cd "$parent"
        "$PYTEST" -o addopts= --no-header -rA --tb=no -p no:cacheprovider \\
            --continue-on-collection-errors $SOCKET_FLAGS tests/unit_tests/ 2>&1 \\
            | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\\1 [$pkg] #; s#^(SKIPPED \\[[0-9]+\\])[[:space:]]+#\\1 [$pkg] #"
    )
    ran_any=1
done < <(find /home/{pr.repo} -maxdepth 5 -type d -name unit_tests -path '*/tests/unit_tests' -not -path '*/.venv/*' 2>/dev/null | sort)

[ "$ran_any" = "0" ] && echo "run_tests: no tests/unit_tests directory found" >&2
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

cd /home/{pr.repo}
poetry install --with test --no-interaction || true
# Fallback: a single dep build failure (e.g. duckdb-engine on ARM) cuts
# the install short and leaves langchain itself uninstalled. Re-run via
# pip with --no-deps for the project, then runtime deps individually so
# unrelated build errors don't hide langchainplus_sdk / langsmith / etc.
poetry run pip install -e . --no-deps 2>/dev/null || true
# Only install pytest plugins if a plugin is genuinely missing — pinning
# pytest<8 so the resolver doesn't drag in pytest 9 (whose deprecations
# break the era's PytestRemovedIn9Warning-as-error suite).
if ! poetry run python -c "import pytest_socket, pytest_asyncio, pytest_mock" 2>/dev/null; then
    # Pin pytest-asyncio<0.24 (older release that still works with pytest<8);
    # later pytest-asyncio versions need pytest>=8.2 and pull pytest 9 back in.
    poetry run pip install "pytest<8" "pytest-asyncio<0.24" \\
        pytest-socket pytest-mock pytest-cov pytest-dotenv freezegun \\
        responses syrupy 2>/dev/null || true
fi
poetry run pip install -e . 2>/dev/null || true

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

@Instance.register("langchain-ai", "langchain_root")
class LangchainRoot(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LangchainRootImageDefault(self.pr, self._config)

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
        # SKIPPED format: "SKIPPED [N] file:line: reason" — keep file:line as
        # the unique identifier so per-line skips don't collapse to one entry.
        re_skip = re.compile(r"^SKIPPED\s+\[\d+\]\s+(\S+?:\d+)(?::\s.*)?\s*$")
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
