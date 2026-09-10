import re
from typing import Union

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
        return "python:3.11"

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

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
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

    def dependency(self) -> Image:
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {pr.base.sha} \\
    || git fetch --no-tags origin "+refs/pull/{pr.number}/head:refs/remotes/origin/pr-{pr.number}"

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1
python -V

python -m pip install --no-cache-dir --upgrade pip setuptools wheel || true
# .[all,dev] not just .[dev]: ~40 of the 81 PRs' target tests live under feature areas
# gated behind optional extras (messaging/telegram/discord/slack, matrix, mcp, modal, cron,
# ...). Installing only [dev] would leave those test modules ImportError-ing at collection
# (like scrapy's Pillow/boto). [all] is a real extra in this repo's pyproject and pulls every
# feature dep so every PR's target test can import. Fall back to [dev] if [all] can't resolve.
python -m pip install --no-cache-dir -e ".[all,dev]" || \
    python -m pip install --no-cache-dir -e ".[dev]" || true
python -m pip install --no-cache-dir pytest-xdist pytest-timeout || true

# NOTE: do NOT pass `-p <plugin>` to pytest here. hermes_cli/main.py runs
# _apply_profile_override() at import time, which pre-parses sys.argv for `-p`/
# `--profile` BEFORE argparse. At these base commits it has no guard for pytest's
# `-p no:...`, so it treats "no:cacheprovider" as a profile name, fails validation
# ("Invalid profile name"), and sys.exit(1) -> any test module importing
# hermes_cli.main raises SystemExit at collection -> pytest INTERNALERROR aborts the
# WHOLE run -> 0 results. Keeping the pytest cmd `-p`-free avoids it entirely.
python -m pytest tests --collect-only -q -n 0 \
    --continue-on-collection-errors > /home/collect.txt 2>&1 || true
tail -3 /home/collect.txt
grep -qE "[0-9]+ (tests|test) collected" /home/collect.txt

# ---- BASELINE DESELECT ---------------------------------------------------
# Run the suite with NO patch and record every test that FAILS/ERRORS at baseline.
# Such tests are environment-broken (no audio device / systemd / live gateway) or
# flaky (async timing) - they are NOT this PR's graded signal, but when they flip
# pass<->fail across the run/test/fix stages they spuriously invalidate the PR
# ("test passed before fix, failed after"). We deselect them in all three graded
# stages so only stable, gradeable tests count. Tests in files the test.patch
# touches are NEVER deselected - those are the graded target (f2p/n2p). --timeout
# caps hangers so a single stuck async test can't stall the whole stage.
python -m pytest tests -n 0 --timeout=300 --timeout-method=thread \
    -rA --no-header --tb=no --continue-on-collection-errors \
    > /home/baseline.txt 2>&1 || true
# target test files (never deselect anything defined in them)
grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null | sed 's#^+++ b/##' | grep -E '\\.py$' \
    | sort -u > /home/target_files.txt || true
: > /home/baseline_deselect.txt
grep -E '^(FAILED|ERROR) ' /home/baseline.txt | awk '{{print $2}}' | sort -u | while read -r nid; do
    f="${{nid%%::*}}"
    grep -qxF "$f" /home/target_files.txt && continue
    echo "$nid" >> /home/baseline_deselect.txt
done
echo "baseline deselect: $(wc -l < /home/baseline_deselect.txt) tests"
# --------------------------------------------------------------------------

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -euo pipefail
export CI=true

cd /home/{pr.repo}
DES=()
if [ -s /home/baseline_deselect.txt ]; then
    while IFS= read -r n; do [ -n "$n" ] && DES+=(--deselect "$n"); done < /home/baseline_deselect.txt
fi
python -m pytest tests -n 0 --timeout=300 --timeout-method=thread "${{DES[@]}}" \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -euo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "PATCH_APPLY_FAILED: test.patch does not apply at {pr.base.sha}" >&2
    exit 1
fi
DES=()
if [ -s /home/baseline_deselect.txt ]; then
    while IFS= read -r n; do [ -n "$n" ] && DES+=(--deselect "$n"); done < /home/baseline_deselect.txt
fi
python -m pytest tests -n 0 --timeout=300 --timeout-method=thread "${{DES[@]}}" \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -euo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "PATCH_APPLY_FAILED: test.patch + fix.patch do not apply at {pr.base.sha}" >&2
    exit 1
fi
DES=()
if [ -s /home/baseline_deselect.txt ]; then
    while IFS= read -r n; do [ -n "$n" ] && DES+=(--deselect "$n"); done < /home/baseline_deselect.txt
fi
python -m pytest tests -n 0 --timeout=300 --timeout-method=thread "${{DES[@]}}" \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

{hardening}

RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("NousResearch", "hermes-agent")
class NousResearchHermesAgent(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_standard = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"(?:\s+\(.*\))?(?:\s+\[.*\])?\s*$"
        )

        re_xdist = re.compile(
            r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+(\S+::\S+)"
        )

        re_summary = re.compile(
            r"^(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+"
            r"(?:\[\d+\]\s+)?(\S+::\S+)"
        )

        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_xdist.match(line) or re_summary.match(line)
            if m:
                status = m.group(1)
                test_name = m.group(2)
            else:
                m = re_standard.match(line)
                if m:
                    test_name = m.group(1)
                    status = m.group(2)
                else:
                    continue

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

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
