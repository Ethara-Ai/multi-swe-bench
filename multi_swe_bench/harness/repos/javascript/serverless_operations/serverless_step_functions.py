import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JSON_BEGIN = "=====MOCHA_JSON_BEGIN====="
_JSON_END = "=====MOCHA_JSON_END====="
_SPEC_BEGIN = "=====MOCHA_SPEC_BEGIN====="
_SPEC_END = "=====MOCHA_SPEC_END====="

_TEST_CMD = f"""
REPORT=/home/mocha-report.json
SPEC_LOG=/home/mocha-spec.log
rm -f "$REPORT" "$SPEC_LOG"

set +e
npx mocha "!(node_modules)/**/*.test.js" \\
  --reporter json --reporter-option output="$REPORT" \\
  > /home/mocha-stdout.log 2>&1
MOCHA_STATUS=$?
set -e
echo "mocha exit status: $MOCHA_STATUS"

echo "{_JSON_BEGIN}"
cat "$REPORT" 2>/dev/null || true
echo ""
echo "{_JSON_END}"

echo "----- mocha stdout/stderr -----"
cat /home/mocha-stdout.log 2>/dev/null || true

if [ ! -s "$REPORT" ]; then
  echo "JSON reporter produced no report; retrying with the spec reporter." >&2
  set +e
  npx mocha "!(node_modules)/**/*.test.js" --reporter spec > "$SPEC_LOG" 2>&1
  SPEC_STATUS=$?
  set -e
  echo "spec-reporter exit status: $SPEC_STATUS"
  echo "{_SPEC_BEGIN}"
  cat "$SPEC_LOG" 2>/dev/null || true
  echo "{_SPEC_END}"

  # A mocha epilogue ("N passing" / "N failing" / "N pending") is proof the
  # runner actually executed a suite. Absent it, both reporters failed to
  # start and there is nothing for parse_log to read.
  if ! grep -qE '^[[:space:]]*[0-9]+[[:space:]]+(passing|failing|pending)' "$SPEC_LOG"; then
    echo "FATAL: mocha produced no test results (json status $MOCHA_STATUS, spec status $SPEC_STATUS)" >&2
    exit 1
  fi
fi
""".strip()


class ServerlessStepFunctionsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:14"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class ServerlessStepFunctionsImageDefault(Image):
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
        return ServerlessStepFunctionsImageBase(self.pr, self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
npm install || true
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm install || true
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm install || true
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
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


@Instance.register("serverless-operations", "serverless-step-functions")
class ServerlessStepFunctions(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServerlessStepFunctionsImageDefault(self.pr, self._config)

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

    def _relativize(self, path: str) -> str:
        """Absolute in-container test path -> repo-relative path."""
        if not path:
            return ""
        path = path.replace("\\", "/")
        marker = f"/home/{self.pr.repo}/"
        idx = path.find(marker)
        if idx != -1:
            return path[idx + len(marker) :]
        return path.lstrip("/")

    def _test_name(self, test: dict) -> str:
        """Key a mocha JSON test entry as "<rel path>::<full title>".

        Mirrors the pytest node-id shape used across the dataset, e.g.
        "fiasco/tests/test_ion.py::test_free_free_radiative_loss".

        The path prefix is what makes the key unique: this suite duplicates
        both `it` titles and root `describe` names across files. `::` is also
        report.py's primary discriminator — `_test_name_matches_files` splits
        on it first (`test_name.split("::", 1)[0]`) and compares the head to
        the patch's file list, so this shape hits the exact-path branch rather
        than the `" > "` prefix fallback.
        """
        title = (test.get("fullTitle") or test.get("title") or "").strip()
        rel = self._relativize(test.get("file") or "")
        if rel and title:
            return f"{rel}::{title}"
        return rel or title

    @staticmethod
    def _json_objects(text: str) -> list[dict]:
        """Every top-level {...} block in `text` that parses as a JSON object."""
        objects = []
        depth = 0
        start = None
        in_string = False
        escaped = False

        for i, ch in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        try:
                            obj = json.loads(text[start : i + 1])
                        except (json.JSONDecodeError, ValueError):
                            obj = None
                        if isinstance(obj, dict):
                            objects.append(obj)
                        start = None

        return objects

    def _parse_json_report(self, clean_log: str):
        """Parse the mocha JSON reporter payload. Returns None if absent."""
        begin = clean_log.find(_JSON_BEGIN)
        end = clean_log.find(_JSON_END)
        if begin != -1 and end > begin:
            region = clean_log[begin + len(_JSON_BEGIN) : end]
        else:
            region = clean_log

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        found = False

        for data in self._json_objects(region):
            if not any(k in data for k in ("passes", "failures", "pending")):
                continue
            found = True
            for test in data.get("passes") or []:
                name = self._test_name(test)
                if name:
                    passed_tests.add(name)
            for test in data.get("failures") or []:
                name = self._test_name(test)
                if name:
                    failed_tests.add(name)
            for test in data.get("pending") or []:
                name = self._test_name(test)
                if name:
                    skipped_tests.add(name)

        if not found:
            return None

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return passed_tests, failed_tests, skipped_tests

    @staticmethod
    def _parse_spec_report(clean_log: str):
        """Degraded fallback: scrape mocha's spec reporter.

        Only reached when the JSON reporter produced nothing. Names are
        describe-qualified but NOT path-qualified, so cross-file title
        collisions are possible here; this exists so a run still yields
        results rather than an empty report.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        summary_re = re.compile(r"^\s*\d+\s+(passing|failing|pending)\b")
        marker_re = re.compile(
            r"^(\s*)(?:([✓✔])|([×✗])|(-)|(\d+\)))\s+(.*?)"
            r"(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )
        plain_re = re.compile(r"^(\s*)(\S.*?)\s*$")

        indent_to_level = {}
        path = []

        for raw in clean_log.splitlines():
            if summary_re.match(raw):
                break

            match = marker_re.match(raw)
            if match:
                spaces, ok, bad, pending, numbered, name = match.groups()
            else:
                plain = plain_re.match(raw)
                if not plain:
                    continue
                spaces, name = plain.groups()
                ok = bad = pending = numbered = None
                if name.endswith(":"):
                    continue

            indent = len(spaces)
            if indent not in indent_to_level:
                shallower = sorted(i for i in indent_to_level if i < indent)
                indent_to_level[indent] = (
                    indent_to_level[shallower[-1]] + 1 if shallower else 0
                )
            level = indent_to_level[indent]

            path = path[:level]
            path.append(name)
            full = " ".join(path)

            if ok:
                passed_tests.add(full)
            elif bad or numbered:
                failed_tests.add(full)
            elif pending:
                skipped_tests.add(full)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return passed_tests, failed_tests, skipped_tests

    def parse_log(self, test_log: str) -> TestResult:
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_escape.sub("", test_log)

        parsed = self._parse_json_report(clean_log)
        if parsed is None:
            begin = clean_log.find(_SPEC_BEGIN)
            end = clean_log.find(_SPEC_END)
            region = (
                clean_log[begin + len(_SPEC_BEGIN) : end]
                if begin != -1 and end > begin
                else clean_log
            )
            parsed = self._parse_spec_report(region)

        passed_tests, failed_tests, skipped_tests = parsed

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
