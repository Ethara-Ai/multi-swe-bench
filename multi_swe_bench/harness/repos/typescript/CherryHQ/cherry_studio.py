from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# CherryHQ/cherry-studio  —  Electron + React desktop app, TypeScript.
#
# Toolchain at the graded era (base 1cb2af57, PR #11305):
#   * node   >= 22          (package.json "engines")
#   * yarn   4.9.1 berry    (packageManager + .yarnrc.yml yarnPath ->
#                            .yarn/releases/yarn-4.9.1.cjs, nodeLinker:
#                            node-modules, enableImmutableInstalls: false)
#   * vitest 3.2.4          driven by vitest.config.ts, which declares THREE
#                           projects: main (node), renderer (jsdom), scripts.
#
# Scope: only the `renderer` project is graded. The PR's test.patch lands in
# src/renderer/**, the renderer project holds 140 test files (a healthy p2p
# body), and confining the run keeps the electron/main project — which wants a
# real electron runtime — out of the graded signal entirely.
#
# Reporting: vitest's JSON reporter, dumped between markers, NOT the console
# reporter. Console output carries per-test timing ("(123ms)") that varies
# between the run / test / fix stages, and with `projects` it also prefixes
# every line with "|renderer|". Both would make the SAME test parse to a
# DIFFERENT name in different stages, which Report.__post_init__ unions into
# two half-present entries and Report.check() then rejects as an anomalous
# NONE->FAIL. The JSON reporter carries no timing and no project tag.
#
# Test IDs are built as "<repo-relative path> > <ancestor> > ... > <title>",
# the shape report.py::_test_name_matches_files recognises for JS/TS via its
# `test_name.startswith(f + " > ")` branch — so n2p/f2p attribution back to
# test_patch_files works without relying on diff-content matching alone.
# ---------------------------------------------------------------------------

REPO_DIR = "/home/cherry-studio"
RESULTS_JSON = "/home/vitest-results.json"
BEGIN_MARKER = "===== BEGIN TEST RESULTS ====="
END_MARKER = "===== END TEST RESULTS ====="

# `|| rc=$?` (not `|| true`) — the exit code is preserved and echoed, so a
# runner that never STARTED is still visible in the log instead of being
# silently swallowed. vitest writes --outputFile before exiting non-zero on a
# failing suite, which is exactly the test-stage state we must capture.
#
# --retry=2 is load-flake insurance, not leniency. Verified against this tree:
# ShikiStreamTokenizer > streaming > "should handle a single chunk of complex
# code" passes 5/5 in isolation but failed once inside the full 1400-test
# renderer run. A flake that lands in the FIX stage alone is fatal here — it
# reads PASS(test) -> FAIL(fix), which is Report.check() rule 2 ("no new
# failures") and rejects the whole instance. Retrying costs nothing when the
# suite is green and removes a coin-flip that would silently bin the dataset.
TEST_CMD = f"""rm -f {RESULTS_JSON}
find {REPO_DIR} -name 'vitest-results.json' -delete 2>/dev/null || true

vitest_rc=0
yarn vitest run --project renderer --retry=2 \\
    --reporter=json --outputFile={RESULTS_JSON} \\
    --no-color || vitest_rc=$?
echo "VITEST_EXIT=${{vitest_rc}}"

echo '{BEGIN_MARKER}'
cat {RESULTS_JSON}
echo ''
echo '{END_MARKER}'"""


class CherryStudioImageBase(Image):
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
        # engines.node ">=22.0.0"; bookworm (not alpine) because the dependency
        # tree pulls native node-gyp addons that have no musl prebuilds.
        return "node:22-bookworm"

    def image_tag(self) -> str:
        # base-pr-<N>, matching the tag the shipped datasets and the ECR push
        # script expect (mswebench_<org>_m_<repo>_base-pr-<N>.tar).
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV HUSKY=0
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
ENV YARN_ENABLE_IMMUTABLE_INSTALLS=false
ENV YARN_NODE_LINKER=node-modules
ENV NODE_OPTIONS=--max-old-space-size=4096

# node-gyp needs a C toolchain + python3 for registry-js / selection-hook,
# which do build here. Electron's own GTK/NSS runtime libs are deliberately
# NOT installed: the graded project is `renderer`, which vitest runs under
# jsdom, and ELECTRON_SKIP_BINARY_DOWNLOAD keeps the runtime out entirely.
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential python3 pkg-config \\
    && rm -rf /var/lib/apt/lists/*

# yarn 4.9.1 is resolved by corepack from packageManager/.yarnrc.yml yarnPath;
# the repo ships .yarn/releases/yarn-4.9.1.cjs so no network pin is needed.
RUN corepack enable

{code}

{self.clear_env}

"""


class CherryStudioImageDefault(Image):
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
        return CherryStudioImageBase(self.pr, self._config)

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

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# `|| true`: native node-gyp addons in this tree (libsecret bindings, swc,
# esbuild) routinely fail to compile on arm64 while the jsdom renderer suite
# that we actually grade does not import them. A hard failure here would kill
# the image build for a dependency the graded tests never touch.
yarn install --mode=skip-build || true
yarn install || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{test_cmd}
""".format(repo_dir=REPO_DIR, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("CherryHQ", "cherry-studio")
class CHERRY_STUDIO(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CherryStudioImageDefault(self.pr, self._config)

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

        # ANSI first — --no-color is passed, but a wrapper (yarn, tsx) can still
        # colourise, and a stray escape breaks every anchor below.
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        payload = self._extract_json_payload(clean)
        if payload is not None:
            self._collect_from_json(payload, passed_tests, failed_tests, skipped_tests)
        else:
            # Fallback: vitest console reporter. Only reached when the JSON
            # reporter produced nothing parseable (e.g. vitest died before
            # writing --outputFile); keeps a partial signal rather than none.
            self._collect_from_console(
                clean, passed_tests, failed_tests, skipped_tests
            )

        # TestResult.__post_init__ rejects overlapping sets. A test that is
        # retried can legitimately appear twice; failure wins, then skip.
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

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _extract_json_payload(clean_log: str) -> Optional[dict]:
        """Pull the JSON object printed between the run-script markers."""
        start = clean_log.find(BEGIN_MARKER)
        if start == -1:
            return None
        start += len(BEGIN_MARKER)
        end = clean_log.find(END_MARKER, start)
        blob = clean_log[start:end] if end != -1 else clean_log[start:]
        blob = blob.strip()
        if not blob:
            return None
        # The marker block holds exactly one JSON document, but be defensive
        # about trailing shell noise by cutting at the outermost braces.
        first = blob.find("{")
        last = blob.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None
        try:
            parsed = json.loads(blob[first : last + 1])
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _relative_path(name: str) -> str:
        """Absolute in-container path -> repo-relative, forward-slashed."""
        p = (name or "").replace("\\", "/")
        prefix = REPO_DIR + "/"
        if p.startswith(prefix):
            p = p[len(prefix) :]
        return p.lstrip("./")

    @classmethod
    def _collect_from_json(
        cls,
        payload: dict,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        for suite in payload.get("testResults") or []:
            if not isinstance(suite, dict):
                continue
            rel = cls._relative_path(suite.get("name") or "")
            assertions = suite.get("assertionResults") or []

            if not assertions:
                # A file that failed to collect (import/transform error) has no
                # assertions. Record the FILE as failed so the stage is not
                # silently empty; a file-level ID can never collide with the
                # "path > title" IDs below.
                if (suite.get("status") or "").lower() == "failed" and rel:
                    failed.add(rel)
                continue

            for a in assertions:
                if not isinstance(a, dict):
                    continue
                title = (a.get("title") or "").strip()
                ancestors = [
                    str(x).strip() for x in (a.get("ancestorTitles") or []) if str(x).strip()
                ]
                if not title and not ancestors:
                    continue
                parts = ([rel] if rel else []) + ancestors + ([title] if title else [])
                test_id = " > ".join(parts)
                status = (a.get("status") or "").lower()
                if status in ("failed", "error"):
                    failed.add(test_id)
                elif status in ("pending", "skipped", "todo", "disabled"):
                    skipped.add(test_id)
                elif status == "passed":
                    passed.add(test_id)

    @staticmethod
    def _collect_from_console(
        clean_log: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        # " ✓ |renderer| src/a/b.test.ts > suite > case 12ms"
        # The project tag and the trailing duration are both stripped: keeping
        # either would make the same test parse differently across stages.
        line_re = re.compile(
            r"^\s*(?P<mark>[✓√✔×✗❌↓○⊘])\s+"
            r"(?:\|[^|]*\|\s+)?"
            r"(?P<name>\S+\.(?:test|spec)\.[cm]?[jt]sx?\s*>\s*.+?)"
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
        )
        for line in clean_log.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            mark = m.group("mark")
            name = re.sub(r"\s*>\s*", " > ", m.group("name").strip())
            if mark in "✓√✔":
                passed.add(name)
            elif mark in "×✗❌":
                failed.add(name)
            else:
                skipped.add(name)
