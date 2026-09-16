from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "/home/cherry-studio"
RESULTS_JSON = "/home/vitest-results.json"
BEGIN_MARKER = "===== BEGIN TEST RESULTS ====="
END_MARKER = "===== END TEST RESULTS ====="

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
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

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential python3 pkg-config \\
    && rm -rf /var/lib/apt/lists/*

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        payload = self._extract_json_payload(clean)
        if payload is not None:
            self._collect_from_json(payload, passed_tests, failed_tests, skipped_tests)
        else:
            self._collect_from_console(
                clean, passed_tests, failed_tests, skipped_tests
            )

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


    @staticmethod
    def _extract_json_payload(clean_log: str) -> Optional[dict]:
        start = clean_log.find(BEGIN_MARKER)
        if start == -1:
            return None
        start += len(BEGIN_MARKER)
        end = clean_log.find(END_MARKER, start)
        blob = clean_log[start:end] if end != -1 else clean_log[start:]
        blob = blob.strip()
        if not blob:
            return None
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
