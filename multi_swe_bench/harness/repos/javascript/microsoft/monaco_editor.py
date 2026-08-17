import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


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
        return "node:16-bullseye"

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
ENV TZ=Etc/UTC
ENV HUSKY=0

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git curl tzdata \\
    && rm -rf /var/lib/apt/lists/*

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

    def dependency(self) -> Image | None:
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
                _CHECK_GIT_CHANGES_SH,
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

# HUSKY=0 makes husky exit(0) as a no-op, satisfying the repo's
# postinstall script (build/postinstall.ts) which unconditionally
# runs `husky install` and would otherwise fail under Docker.
export HUSKY=0

# `npm ci` is deterministic + fast: package.json and package-lock.json
# agree at base.sha, so no resolution round-trips needed.
npm ci --prefer-offline --no-audit --no-fund

# Compile src/**/*.ts (including src/basic-languages/*/*.test.ts) into
# AMD .js files at out/languages/amd-tsc/. Mocha's test loader
# (test/unit/all.js) glob-loads the compiled .test.js files via
# requirejs, so this step is MANDATORY.
npm run build

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Baseline run: prepare.sh already built src/**/*.ts, so we go
# straight to mocha.
#
# `.mocharc.json` sets `{{"delay": true, "ui": "tdd"}}`. `delay: true`
# means mocha waits for a global `run()` call — `test/unit/all.js`
# invokes it after requirejs finishes async loading of compiled AMD
# modules. `--reporter-option output=<path>` writes structured JSON
# to a file (bypassing stdout, which requirejs dirties with its own
# console.log statements). `timeout 600` guards against a silent
# requirejs load failure that would leave mocha hung forever.
export HUSKY=0
rm -f /tmp/mocha-results.json
timeout 600 node_modules/.bin/mocha test/unit/all.js \\
    --reporter json \\
    --reporter-option output=/tmp/mocha-results.json || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  # Empty-but-valid mocha-shaped JSON so parse_log has well-formed
  # input to walk (distinguishes "runner crashed before writing"
  # from "runner ran but nothing matched").
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply only test.patch. It adds private-identifier tokenization
# test cases to src/basic-languages/{{javascript,typescript}}/*.test.ts.
# fix.patch is NOT applied here, so the tokenizer regex still rejects
# `#` at the start of identifiers and the added cases are expected
# to FAIL.
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# test.patch modifies .test.ts under src/, so we MUST re-run the
# build to regenerate the compiled .test.js files that requirejs
# loads. Without this, mocha runs the OLD .test.js from prepare.sh
# and never sees the new assertions.
export HUSKY=0
npm run build

rm -f /tmp/mocha-results.json
timeout 600 node_modules/.bin/mocha test/unit/all.js \\
    --reporter json \\
    --reporter-option output=/tmp/mocha-results.json || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply fix.patch first, then test.patch. They touch disjoint files
# (fix->typescript.ts, test->{{javascript,typescript}}.test.ts) so
# order is conventional, matching other harness repos.
git apply --whitespace=nowarn /home/fix.patch || {{
    echo "Warning: fix.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/fix.patch || true
    find . -name '*.rej' -delete
}}
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# Both patches touch .ts under src/, so re-build to regenerate
# out/languages/amd-tsc/*/*.js with the new regex AND the new
# .test.js assertions. Skipping this leaves the OLD tokenizer active
# despite patches on disk, producing an identical result to
# test-run.sh (the "no discrimination" failure mode).
export HUSKY=0
npm run build

rm -f /tmp/mocha-results.json
timeout 600 node_modules/.bin/mocha test/unit/all.js \\
    --reporter json \\
    --reporter-option output=/tmp/mocha-results.json || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
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


@Instance.register("microsoft", "monaco-editor")
class MonacoEditor(Instance):
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

        # Strip ANSI escapes defensively; Mocha's JSON reporter does not
        # emit colour codes, but wrapping tooling (harness log capture,
        # docker layer, tee, etc.) has been observed to re-inject them.
        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        cleaned_log = ansi_escape.sub("", test_log)

        # Balanced-JSON walker. Tracks brace depth OUTSIDE strings only;
        # string state is tracked so `{` / `}` inside test titles cannot
        # confuse the depth counter, and backslash-escapes inside strings
        # are honoured so `\"` doesn't close a string prematurely.
        json_blocks: list[str] = []
        n = len(cleaned_log)
        i = 0
        while i < n:
            if cleaned_log[i] != "{":
                i += 1
                continue
            depth = 0
            in_string = False
            escape = False
            end = -1
            j = i
            while j < n:
                ch = cleaned_log[j]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                j += 1
            if end >= 0:
                json_blocks.append(cleaned_log[i : end + 1])
                i = end + 1
            else:
                i += 1

        def _title_of(entry: dict) -> str:
            if not isinstance(entry, dict):
                return ""
            full = entry.get("fullTitle")
            if isinstance(full, str) and full:
                return full
            leaf = entry.get("title")
            return leaf if isinstance(leaf, str) else ""

        for block in json_blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            # Mocha --reporter json top-level schema:
            #   {"stats":{...}, "tests":[...], "pending":[...],
            #    "failures":[...], "passes":[...]}
            # `tests` is redundant with the other three combined.
            for entry in data.get("passes", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                if name not in failed_tests:
                    passed_tests.add(name)

            for entry in data.get("failures", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                failed_tests.add(name)
                passed_tests.discard(name)

            for entry in data.get("pending", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
