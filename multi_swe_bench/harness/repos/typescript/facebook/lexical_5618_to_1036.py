"""facebook/lexical harness for NPM + Node 16 era (PRs #1036-#5618).

Routes bundles whose anchor PR (lowest number in prs_in_bundle) is in 942..5618.

Uses node:16 as the Docker base image.
Package manager: npm. Tests: jest unit + Playwright e2e.
All packages use lexical-* naming convention.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _filter_binary_patches(patch_content: str) -> str:
    if not patch_content:
        return patch_content
    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)


class _NpmImageBase(Image):
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base-npm"

    def workdir(self) -> str:
        return "base-npm"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y \\
    wget gnupg ca-certificates chromium \\
    fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg \\
    fonts-khmeros fonts-kacst fonts-freefont-ttf libxss1 dbus dbus-x11 \\
    --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMIUM_FLAGS="--no-sandbox"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \\
    export NVM_DIR="$HOME/.nvm" && \\
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

{self.clear_env}

"""


class _NpmImageDefault(Image):
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
        return _NpmImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{_filter_binary_patches(self.pr.fix_patch)}"),
            File(".", "test.patch", f"{_filter_binary_patches(self.pr.test_patch)}"),
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
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{pr.repo}
git reset --hard

npm ci || npm install || true
npx playwright install chromium || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}

npm ci || npm install || true
npx playwright install chromium || true
npm run build || true
npm run test-unit || true
npm run start & npm run test-e2e-chromium -- --retries=5 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

npm ci || npm install || true
npx playwright install chromium || true
npm run build || true
npm run test-unit || true
npm run start & npm run test-e2e-chromium -- --retries=5 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

npm ci || npm install || true
npx playwright install chromium || true
npm run build || true
npm run test-unit || true
npm run start & npm run test-e2e-chromium -- --retries=5 || true

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
        repo = self.pr.repo
        return f"""FROM {name}:{tag}

{self.global_env}

ARG REPO_URL="https://github.com/{self.pr.org}/{repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR /home
RUN git clone "$REPO_URL" /home/{repo}
WORKDIR /home/{repo}
RUN git fetch --no-tags origin "${{BASE_COMMIT}}" || true
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{copy_commands}
{prepare_commands}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("facebook", "lexical_5618_to_1036")
class LexicalNpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _NpmImageDefault(self.pr, self._config)

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
        return _parse_lexical_log(test_log)


def _parse_lexical_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    passed_res = [
        re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$"),
        re.compile(
            r"^\s*[✔✓]\s+(?:\d+\s+)?(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        ),
    ]

    failed_res = [
        re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$"),
        re.compile(
            r"\s*[×✗✘✖]\s+(?:\d+\s+)?(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        ),
        re.compile(r"\s*\d+\)\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
    ]

    skipped_res = [
        re.compile(r"SKIP:?\s?(.+?)\s"),
        re.compile(r"^[\s]*[-]\s+(.*?)(?:\s+\([\d\.]+\s*\w+\))?$"),
    ]
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    # Collapse Playwright retry attempts onto the same test id so a test that
    # fails then passes on retry (flaky) is scored as its final PASS -- not a
    # spurious FAIL. Without this the trailing "N) <name>" failure-detail echo
    # (printed even for a flaky test that ultimately passed) invalidates an
    # otherwise-good report. Last authoritative status wins; sets stay disjoint.
    retry_suffix = re.compile(r"\s*\(retry #\d+\)\s*$")

    def _norm(name: str) -> str:
        return retry_suffix.sub("", name.strip()).strip()

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()

        matched_pass = False
        for passed_re in passed_res:
            m = passed_re.match(line)
            if m:
                name = _norm(m.group(1))
                passed_tests.add(name)
                failed_tests.discard(name)   # a later pass (e.g. retry) wins
                skipped_tests.discard(name)
                matched_pass = True
                break
        if matched_pass:
            continue

        matched_fail = False
        for failed_re in failed_res:
            m = failed_re.match(line)
            if m:
                name = _norm(m.group(1))
                if name in passed_tests:
                    # already passing (flaky retry) -- a trailing failure-detail
                    # line must not flip it back to failed.
                    matched_fail = True
                    break
                failed_tests.add(name)
                skipped_tests.discard(name)
                matched_fail = True
                break
        if matched_fail:
            continue

        for skipped_re in skipped_res:
            m = skipped_re.match(line)
            if m:
                name = _norm(m.group(1))
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                break

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# number_interval routing (dash-joined prs_in_bundle) -- registry-scoped logic.
#
# The delivered dataset carries `number_interval` as the dash-joined PR bundle
# (e.g. "1036-1041-1047-..."), not an era key. Instead of hardcoding every
# bundle string, routing is expressed as LOGIC: a bundle belongs to the era
# whose PR upper-bound first covers the bundle's anchor (its lowest PR number).
# Each era file contributes only its own (upper_bound -> Instance class) entry;
# the shared router assembled on Instance dispatches any dash-joined value by
# range, so no interval list ever needs to live in the registry.
#
# Two idempotent, lexical-scoped shims are installed at import time:
#   1. PullRequest.from_json -- fill an empty number_interval from prs_in_bundle
#      (dash-joined) so the value flows into the output dataset and drives routing.
#   2. Instance.create -- when number_interval is a dash-joined bundle (not a
#      registered key), derive the anchor and dispatch to the covering era.
# ---------------------------------------------------------------------------
import json as _lex_json
from multi_swe_bench.harness.pull_request import PullRequest as _LexPullRequest

# This era covers bundles whose anchor PR number is <= 5618.
Instance._lexical_eras = getattr(Instance, "_lexical_eras", {})
Instance._lexical_eras[5618] = LexicalNpm


def _lexical_pick_era(pr):
    eras = getattr(Instance, "_lexical_eras", {})
    if not eras:
        return None
    ni = getattr(pr, "number_interval", "") or ""
    anchors = [int(tok) for tok in ni.split("-") if tok.isdigit()]
    if not anchors:
        return None
    anchor = min(anchors)
    for hi in sorted(eras):            # ascending PR upper-bound order
        if anchor <= hi:
            return eras[hi]
    return eras[max(eras)]             # newest era for anything past the last bound


if not getattr(_LexPullRequest, "_lexical_ni_shim", False):
    _lex_orig_from_json = _LexPullRequest.from_json.__func__

    def _lex_from_json(cls, json_str):
        pr = _lex_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "facebook"
                and getattr(pr, "repo", "") == "lexical"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_lex_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    _LexPullRequest.from_json = classmethod(_lex_from_json)
    _LexPullRequest._lexical_ni_shim = True


if not getattr(Instance, "_lexical_route_shim", False):
    _lex_orig_create = Instance.create.__func__

    def _lex_create(cls, pr, config, *args, **kwargs):
        try:
            return _lex_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == "facebook" and getattr(pr, "repo", "") == "lexical":
                era = _lexical_pick_era(pr)
                if era is not None:
                    return era(pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_lex_create)
    Instance._lexical_route_shim = True
