"""paperless-ngx / paperless-ngx -- org/repo fallback config.

WHY THIS FILE IS REGISTERED AS "paperless-ngx" AND NOT AS AN ERA NAME
---------------------------------------------------------------------
Instance.create() builds its lookup key like this (instance.py):

    if pr.number_interval != "":  key = f"{pr.org}/{pr.number_interval}"
    elif pr.tag == "":            key = f"{pr.org}/{pr.repo}"
    else:                         key = f"{pr.org}/{pr.repo}_{tag}"

The delivered dataset row for PR 2302 has NO `number_interval` and NO `tag`,
so the key it resolves to is literally "paperless-ngx/paperless-ngx".  The four
sibling era files in this directory all register interval names
(paperless_ngx_4007_to_2566, ...), none of which that key can ever reach --
and PR 2302 (below the 2566 floor) is outside every one of those ranges anyway.
Registering an era name here would leave the row unroutable: build_dataset.py
would silently find zero instances and report success.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PaperlessNgxImageBase(Image):
    """Heavy environment layer: python 3.9 + OCR toolchain + pipenv, repo at BASE_COMMIT.

    The Dockerfile below deliberately omits the BuildKit syntax directive, the
    proxy ARGs, the CA-cert symlinks, the OCI labels and the git-history
    hardening block.  DockerfileEnhancer.enhance() injects all of them at build
    time -- but ONLY when the rendered text does not already contain the syntax
    directive:

        if cls.SYNTAX_DIRECTIVE in raw:
            return raw          # enhancement skipped entirely

    Emitting our own directive here would therefore switch the hardening OFF.
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

    def dependency(self) -> str:
        # Returning a *str* is what makes build_dataset.py pass REPO_URL and
        # BASE_COMMIT through as build args, and what makes the enhancer run.
        #
        # 3.9 is the middle of the CI matrix for this era
        # (.github/workflows/ci.yml: python-version: ['3.8', '3.9', '3.10'])
        # and is the version every other job in that workflow pins.
        return "python:3.9-slim-bookworm"

    def image_tag(self) -> str:
        # Per-PR base tag, so this image can never be shared with another PR
        # whose BASE_COMMIT differs -- the hardening block prunes everything
        # unreachable from HEAD, which would destroy a co-tenant's base commit.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """FROM python:3.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Three groups, kept separate so a reviewer can tell why each package is here.
#
#  1. infrastructure           git / ca-certificates / curl / gnupg
#                              gnupg is NOT in this repo's CI apt line
#                              because GitHub's ubuntu-22.04 runner already
#                              ships gpg.  On slim Debian it is absent, and
#                              python-gnupg raises "Unable to run gpg" while
#                              pytest is COLLECTING -- which aborts the whole
#                              run, so all 612 tests are skipped and every
#                              stage logs 0 passed / 0 failed.
#  2. build deps               the Pipfile pulls psycopg2 and mysqlclient as
#                              SOURCE distributions (not -binary), so libpq-dev,
#                              pkg-config and libmariadb-dev are mandatory or
#                              `pipenv sync` dies at the compile step
#  3. the OCR runtime          exactly the list this repo's own CI installs:
#                              unpaper tesseract-ocr imagemagick ghostscript
#                              libzbar0 poppler-utils   (ci.yml)
#                              plus libmagic1 for python-magic.
#
# poppler-utils deserves a note: it is not merely a CI leftover.  The fix patch
# rewrites RasterisedDocumentParser.extract_text to shell out to `pdftotext`,
# which ships in poppler-utils.  Without it the fix cannot work and the graded
# test fails for an environment reason rather than a code reason.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl gnupg \\
        build-essential pkg-config libpq-dev libmariadb-dev \\
        unpaper tesseract-ocr tesseract-ocr-eng imagemagick ghostscript \\
        libzbar0 poppler-utils libmagic1 \\
    && rm -rf /var/lib/apt/lists/* \\
    && pdftotext -v

# NOTE on the Arabic language pack (tesseract-ocr-ara).
#
# The graded test asserts that Arabic text comes back:
#     self.assertIn("<arabic string>", parser.get_text())
# and the comment the test patch DELETES says an RTL check "would require
# tesseract-ocr-ara installed for everyone running the test suite".
#
# It is deliberately NOT installed here.  This repo's CI runs this exact test
# with only `tesseract-ocr` (no -ara) and the PR merged, which proves the text
# is read from the PDF's embedded text layer by pdftotext rather than produced
# by OCR.  Adding the pack would change what tesseract emits for that sample
# and could break an assertion that otherwise passes -- deviating from the
# configuration the upstream project actually validates is a risk, not a
# safety net.

# CI pins this exact pipenv release; newer pipenv rejects the lock format used
# by this era's Pipfile.lock.
RUN pip install --no-cache-dir "pipenv==2022.11.30" \\
    && pipenv --version

WORKDIR /home/

# The enhancer rewrites this single line into: parameterized clone, WORKDIR,
# reset --hard, checkout ${BASE_COMMIT}, the hardening block with its four
# integrity assertions, and CMD ["/bin/bash"].  Nothing may follow it.
RUN git clone https://github.com/paperless-ngx/paperless-ngx.git /home/paperless-ngx
"""


class PaperlessNgxImageDefault(Image):
    """Thin PR layer: stages the patches and the run scripts on top of the base."""

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
        return PaperlessNgxImageBase(self.pr, self._config)

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
# The ONE definition of the graded test command.  run.sh, test-run.sh and
# fix-run.sh all delegate here, so the three stages cannot drift apart -- the
# f2p signal is only meaningful if the sole difference between stages is which
# patch was applied.
#
# Deliberately NO `set -e`: the test stage is EXPECTED to fail, and the suite
# must run to completion in every stage or the test-name sets stop matching
# across stages.  The exit status is captured and re-raised at the end.
rc=0

cd /home/paperless-ngx

# Why each flag, since src/setup.cfg fights most of them:
#
#   -o addopts=""       THE CRITICAL ONE.  src/setup.cfg sets
#                         addopts = --pythonwarnings=all --cov --cov-report=html
#                                   --numprocesses auto --quiet
#                       and addopts is PREPENDED to the command line.  --quiet
#                       and -v move the SAME verbosity counter, so "--quiet -v"
#                       nets to zero: pytest prints normal dot output and NOT
#                       the per-test "<nodeid> STATUS" lines parse_log needs.
#                       Measured in-container: with plain -v the parser matched
#                       0 lines out of a 599-passing run -- every stage would
#                       have silently reported 0/0/0.  Clearing addopts drops
#                       --quiet, --cov and --numprocesses auto together, which
#                       is sturdier than relying on counter arithmetic.
#   -v                  now genuinely verbose, one line per test.
#   -p no:sugar         pytest-sugar is in [dev-packages] and REPLACES that
#                       output with a progress bar.  Left enabled, parse_log
#                       would match nothing and every stage would report 0/0/0.
#   --numprocesses 0    addopts sets --numprocesses auto (pytest-xdist).
#                       Parallel Django tests interleave and can flip a test
#                       between runs; a single PASS->FAIL flip between the test
#                       and fix stages invalidates the whole instance under
#                       Report.check() rule 2.  Determinism beats speed here.
#   --no-cov            addopts sets --cov --cov-report=html, which is slow and
#                       writes htmlcov/ into the work tree.
#   --tb=no             tracebacks add nothing the parser reads.
#   -p no:cacheprovider stops .pytest_cache being written into the work tree.
python -m pipenv run pytest -o addopts="" -v --no-header --tb=no \\
    -p no:sugar -p no:cacheprovider \\
    --numprocesses 0 --no-cov \\
    src/ || rc=$?

exit $rc
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/paperless-ngx
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh

# `|| true` per the config standard: native builds can fail on one architecture
# and still leave a usable environment.  It is followed immediately by a hard
# import check, because a bare `|| true` is exactly how an image ends up
# "built successfully" while being unable to run a single test.
python -m pipenv sync --dev || true

if ! python -m pipenv run python -c "import django, pytest, ocrmypdf, magic, psycopg2" 2>&1; then
  echo "prepare: FATAL - the dev environment did not install; retrying once"
  python -m pipenv sync --dev
  python -m pipenv run python -c "import django, pytest, ocrmypdf, magic, psycopg2"
fi
echo "prepare: python environment verified"

# pdftotext is what the fix patch calls; prove it exists at build time rather
# than discovering it inside a graded run.
pdftotext -v
tesseract --version | head -1

# Every stage must start from an identical pristine tree or `git apply` cannot
# be trusted.
git reset --hard
bash /home/check_git_changes.sh
""".replace("[[BASE_SHA]]", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PAPERLESS_TEST_SKIP_CONVERT=1

bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PAPERLESS_TEST_SKIP_CONVERT=1

cd /home/paperless-ngx
git apply --whitespace=nowarn /home/test.patch

bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export PAPERLESS_TEST_SKIP_CONVERT=1

cd /home/paperless-ngx
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/run-tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/paperless-ngx
"""


@Instance.register("paperless-ngx", "paperless-ngx")
class PAPERLESS_NGX(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PaperlessNgxImageDefault(self.pr, self._config)

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
        # ANSI first.  pytest colours its status words even when not attached to
        # a tty in some environments, and an un-stripped escape sequence sits
        # between the node id and the status, so every pattern below would miss.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Primary form -- the verbose progress line, one per test:
        #     src/documents/tests/test_api.py::TestApi::test_x PASSED    [ 12%]
        # Only the node id is captured; the percentage varies between stages and
        # would otherwise make the same test look like two different names,
        # which is what produces the PASS/NONE/FAIL anomaly Report.check()
        # rejects under rule 4.
        progress = re.compile(
            r"^(?P<name>\S+::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b",
            re.MULTILINE,
        )

        for m in progress.finditer(log):
            name = m.group("name").strip()
            status = m.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(name)

        # Fallback -- the `-r` short summary, used only if the progress lines
        # produced nothing at all (e.g. a plugin changed the live output).
        # SKIPPED is intentionally excluded here: in the summary block it is
        # rendered as "SKIPPED [1] path.py:123: reason", i.e. a file:line rather
        # than a node id, and mixing the two naming shapes would invent tests
        # that appear in one stage and not another.
        if not passed_tests and not failed_tests:
            summary = re.compile(
                r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<name>\S+::\S+)",
                re.MULTILINE,
            )
            for m in summary.finditer(log):
                name = m.group("name").strip()
                if m.group("status") == "PASSED":
                    passed_tests.add(name)
                else:
                    failed_tests.add(name)

        # TestResult.__post_init__ raises ValueError if these sets intersect.
        # A test can legitimately appear twice (a rerun, or a teardown ERROR
        # after a PASSED); failure is the authoritative outcome.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
