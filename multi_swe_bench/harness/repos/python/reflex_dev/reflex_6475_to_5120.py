import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks from a unified diff so `git apply` never aborts
    on a binary hunk with no full-index line (e.g. `docs/.DS_Store`, images).
    Safe: binary hunks touch no Python source and never affect test outcomes."""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )



def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v -rA output. Verbose result lines look like:

        tests/units/test_var.py::test_fstring_roundtrip PASSED [ 12%]

    Test names are kept as full pytest node ids (`path::test`)."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Anchor on the trailing "<STATUS> [ NN%]" so node ids containing spaces
    # (parametrized tests, e.g. `test_x[append then pop]`) are captured whole.
    re_line = re.compile(
        r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
    )

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if not m:
            continue
        nodeid, status = m.group(1).strip(), m.group(2)
        if status in ("PASSED", "XPASS"):
            passed_tests.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(nodeid)
        else:  # SKIPPED, XFAIL
            skipped_tests.add(nodeid)

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


class ReflexEra3ImageBase(Image):
    """reflex era 3 — modern uv era (PRs 5120-6475, v0.7->0.9): deps via
    `uv sync`, pytest unit suite under `tests/units/` (`tests/integration/`
    and `tests/benchmarks/` are skipped). Python 3.12."""

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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        return "base-era3"

    def workdir(self) -> str:
        return "base-era3"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN git config --global --add safe.directory '*'
{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ReflexEra3ImageDefault(Image):
    """Per-PR image: checkout base commit, `uv sync`, run the targeted pytest
    unit tests via `uv run`."""

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
        return ReflexEra3ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
uv sync || uv sync --no-dev || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
TEST_FILES=$({{ grep -E '^diff --git a/tests/units/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
uv run --no-sync python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/(pyproject\\.toml|uv\\.lock)' /home/test.patch 2>/dev/null; then
    uv sync || uv sync --no-dev || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/units/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
uv run --no-sync python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.woff*' --exclude='*.svg' \
    --exclude='*.lockb' --exclude='*.webp' --exclude='*.mp3')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/(pyproject\\.toml|uv\\.lock)' /home/test.patch /home/fix.patch 2>/dev/null; then
    uv sync || uv sync --no-dev || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/tests/units/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE 'conftest\\.py|__init__\\.py' | sort -u; }} || true)
EXIST=""
for f in $TEST_FILES; do if [ -f "$f" ]; then EXIST="$EXIST $f"; fi; done
if [ -z "$EXIST" ]; then echo "NO_TEST_FILES"; exit 0; fi
uv run --no-sync python -m pytest $EXIST -v --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors -o 'addopts=' 2>&1
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

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("reflex-dev", "reflex_6475_to_5120")
class REFLEX_6475_TO_5120(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReflexEra3ImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)


# ---------------------------------------------------------------------------
# number_interval bundle routing (prs_in_bundle dash-joined)  -- PIPELINE 11b
# ---------------------------------------------------------------------------
# Raw dataset leaves number_interval empty; delivery sets it to
# "-".join(prs_in_bundle). Register REFLEX_6475_TO_5120 (this era) under every bundle key so
# delivered records resolve to pubkey/<bundle>. Original era-key registration
# above is kept.
_BUNDLE_NIS_REFLEX_ERA3 = [
    "5120-5124-5125-5128-5129-5134-5135-5136-5137-5139",
    "5169-5271-5279-5288-5289-5309-5312-5314-5316-5318-5319-5322-5323-5324-5326-5335",
    "5287-5291-5293-5294-5296-5297-5298-5304-5305-5311",
    "5765-5775-5777-5779-5784",
    "5986-5991-5992-5993",
    "6001-6005-6012-6015-6017-6021-6022-6026-6027",
    "6139-6266-6321-6322-6323-6326-6327-6328-6329-6333-6334-6336-6337-6338-6346-6347-6348",
    "6170-6289-6354-6358-6371-6414-6450-6459-6460-6461-6466-6467-6470-6472-6473-6474-6476-6485-6486-6487",
    "6188-6190-6192-6201-6203-6206-6253-6254-6257-6258-6259-6261-6262-6263-6265-6271-6275-6276-6277-6279-6281-6282-6283-6284",
    "6222-6260-6339-6344-6370-6387-6391-6397-6398-6399-6400-6401-6402-6403-6406-6407-6409-6410-6412-6415-6418-6419-6420-6423-6424-6426-6430-6431-6432-6434-6435-6439-6442-6444-6445-6448-6453-6454-6455-6458",
    "6251-6267-6280-6287-6290-6291-6292-6293-6294-6297-6298-6299-6300-6302-6303-6306-6307-6308-6309-6310-6311-6313-6314-6315-6317-6318-6319",
    "6340-6342-6343-6349-6351-6352-6353-6356-6357-6359-6361-6362-6365-6366-6368-6369-6372-6374-6375-6377-6379-6381-6388-6389-6393",
    "6462-6493-6494-6498-6499-6501",
    "6475-6492",
]
for _ni in _BUNDLE_NIS_REFLEX_ERA3:
    Instance.register("reflex-dev", _ni)(REFLEX_6475_TO_5120)
