import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# The 3 oldest PRs (Django 2.2, 2021-01) only install on Python 3.7; their
# pinned 2021-era requirements fail to build on 3.9+. The rest of this era
# (Django 3.x-4.x) needs Python 3.10 — later code uses `X | Y` type unions.
PY37_PRS = {56, 57, 63}


def parse_django_test_log(log: str) -> TestResult:
    """Parse Django test runner output (`manage.py test --verbosity=2`).

    Verbose result lines look like (Django <4.1):
        test_x (bookmarks.tests.test_mod.SomeTest) ... ok
    or (Django >=5.0, method repeated in the dotted path):
        test_x (bookmarks.tests.test_mod.SomeTest.test_x) ... ok

    Test names are normalised to the full `module.Class.method` id so they are
    stable across the run/test/fix stages regardless of the Django version."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_line = re.compile(
        r"^(test\w*)\s+\(([\w.]+)\)(?:\s+\([^)]*\))?\s+\.\.\.\s+"
        r"(ok|FAIL|ERROR|skipped|expected failure|unexpected success)"
    )
    # End-of-run summary block: "FAIL: test_x (a.b.C.test_x)" / "ERROR: ..."
    re_summary = re.compile(r"^(?:FAIL|ERROR):\s+(test\w*)\s+\(([\w.]+)\)")

    def norm(method: str, path: str) -> str:
        return path if path.endswith("." + method) else f"{path}.{method}"

    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub("", raw).strip()
        m = re_line.match(line)
        if m:
            name = norm(m.group(1), m.group(2))
            status = m.group(3)
            if status == "ok":
                passed_tests.add(name)
            elif status in ("FAIL", "ERROR", "unexpected success"):
                failed_tests.add(name)
            else:  # skipped, expected failure
                skipped_tests.add(name)
            continue
        m = re_summary.match(line)
        if m:
            failed_tests.add(norm(m.group(1), m.group(2)))

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




def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never aborts on a binary hunk
    with no full-index line (e.g. *.png/*.sqlite/*.afdesign). Safe: binary
    hunks touch no Python source and never affect test outcomes."""
    import re as _re
    sections = _re.split(r"(?=^diff --git )", patch, flags=_re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )

class LinkdingEraAImageBase(Image):
    """linkding era A (PRs 56-581, v1.1->1.20): Django app installed via
    `requirements.txt`, tests run with the Django test runner. Python is
    routed per-PR — 3.7 for the Django-2.2 trio, 3.10 for the rest."""

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
        return "python:3.7-slim" if self.pr.number in PY37_PRS else "python:3.10-slim"

    def image_tag(self) -> str:
        return "base-py37" if self.pr.number in PY37_PRS else "base-py310"

    def workdir(self) -> str:
        return self.image_tag()

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
    git build-essential ca-certificates && rm -rf /var/lib/apt/lists/*

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


class LinkdingEraAImageDefault(Image):
    """Per-PR image: checkout base commit, install requirements, run the
    targeted Django unit tests."""

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
        return LinkdingEraAImageBase(self.pr, self._config)

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
pip install --no-cache-dir -r requirements.txt || true
mkdir -p data
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
# Django module labels for the test files this PR's test patch touches
# (bookmarks/tests/ only; e2e tests need a browser and are skipped).
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_BASELINE_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.sqlite' --exclude='*.woff*' \
    --exclude='*.afdesign' --exclude='*.shortcut')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/requirements' /home/test.patch 2>/dev/null; then
    pip install --no-cache-dir -r requirements.txt || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
mkdir -p data
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.ico' --exclude='*.pdf' --exclude='*.sqlite' --exclude='*.woff*' \
    --exclude='*.afdesign' --exclude='*.shortcut')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/requirements' /home/test.patch /home/fix.patch 2>/dev/null; then
    pip install --no-cache-dir -r requirements.txt || true
fi
TEST_FILES=$({{ grep -E '^diff --git a/bookmarks/tests/' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -E '\\.py$' | grep -vE '__init__\\.py|/helpers\\.py' | sort -u; }} || true)
LABELS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then LABELS="$LABELS $(echo "${{f%.py}}" | tr '/' '.')"; fi
done
if [ -z "$LABELS" ]; then echo "NO_TEST_FILES"; exit 0; fi
python manage.py test $LABELS --verbosity=2 2>&1
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


@Instance.register("sissbruecker", "linkding_581_to_56")
class LINKDING_581_TO_56(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LinkdingEraAImageDefault(self.pr, self._config)

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
        return parse_django_test_log(log)


# --- number_interval bundle routing (prs_in_bundle dash-joined) -- PIPELINE 11b
_BUNDLE_NIS_LINKDING_A = [
    "56-60",
    "57-67-72",
    "63-66",
    "126-134-136-149",
    "159-190-197-201",
    "165-168",
    "176-183-184",
    "226-253-259-260-261",
    "229-241-242-244-248-249-250",
    "264-265-268-269-270",
    "281-282-289",
    "293-294-295-297-299-302-304-305",
    "306-307-311",
    "310-312-313-316-318-319-320-321",
    "323-330-331-332-333-334-336-339",
    "349-350",
    "354-360-368-371-379-383-384-387-388",
    "359-474-476-478-479-480-482-494-497",
    "365-398-406-432-440-446-449-455-466",
    "366-374-391-400",
    "389-630-649-650-652-653-655-656-658",
    "390-392-401-402",
    "395-407-427-429",
    "503-504-505-506",
    "513-514-515-516-517",
    "542-544-549-550-555-560-565",
    "567-569",
    "570-571-574-579",
    "581-585-601-602-603-607-612",
]
for _ni in _BUNDLE_NIS_LINKDING_A:
    Instance.register("sissbruecker", _ni)(LINKDING_581_TO_56)
