import json as _rest_hapi_json
import re
from typing import Optional, Union

from multi_swe_bench.harness.dataset import Dataset as _RestHapiDataset
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RestHapiImageBase(Image):
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
        return "node:12-bullseye"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{self.clear_env}

RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}
"""


class RestHapiImageDefault(Image):
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
        return RestHapiImageBase(self.pr, self.config)

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
# Base commits living only on refs/pull/* are not in a plain clone.
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb 2>/dev/null | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

# npm ci uses the committed package-lock.json for reproducibility; npm install
# is a fallback if the base commit's lockfile is stale. mongodb-memory-server's
# postinstall downloads mongod (~79MB) here -- caching it at build time so test
# runs stay offline.
npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

# Invoke tape directly (not `npm test`) so we skip:
#   nyc      -- coverage overlay adds no test signal
#   posttest -- codecov upload; without a token it would taint the exit code
npx tape ./tests/unit/*.tests.js ./tests/e2e/*.tests.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch 2>/dev/null; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
fi

# Patches can bump deps; node_modules was baked at image build time.
if git diff --name-only | grep -qE '^(package\.json|package-lock\.json)$'; then
    npm install --no-audit --no-fund 2>&1 | tail -20 || true
fi

bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    patch --batch --fuzz=5 -p1 -i /home/test.patch || true
    patch --batch --fuzz=5 -p1 -i /home/fix.patch || true
fi

# Patches can bump deps; node_modules was baked at image build time.
if git diff --name-only | grep -qE '^(package\.json|package-lock\.json)$'; then
    npm install --no-audit --no-fund 2>&1 | tail -20 || true
fi

bash /home/run-tests.sh

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

WORKDIR /home

{self.clear_env}

"""


@Instance.register("JKHeadley", "rest-hapi")
class REST_HAPI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RestHapiImageDefault(self.pr, self._config)

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
        """Parse tape / TAP v13 output.

        Tape prints:
            TAP version 13
            # <group comment>
            ok N <name>
            not ok N <name>
            ok N <name> # SKIP <reason>
            1..N

        Comment lines and the plan line are ignored. Skips are detected before
        passes because `# SKIP` still starts with `ok`. ANSI colour codes are
        stripped in case a downstream terminal wrapper injects them. Crash
        diagnostics that tape reports as `not ok` (plan!=count, uncaught
        Error/TypeError, "test exited without ending") are rejected: those
        strings are error text, not test identifiers, and would otherwise
        pollute the failure set and prevent F2P transitions from being
        computed against real test names.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_skip = re.compile(r"^ok\s+\d+\s+(.+?)\s+#\s*SKIP", re.IGNORECASE)
        re_pass = re.compile(r"^ok\s+\d+\s+(.+?)\s*$")
        re_fail = re.compile(r"^not ok\s+\d+\s+(.+?)\s*$")
        re_noise = re.compile(
            r"^(plan\s*!=\s*count"
            r"|test exited without ending:"
            r"|(TypeError|SyntaxError|ReferenceError|RangeError|Error|AssertionError):\s)",
            re.IGNORECASE,
        )

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1).strip()
                if not re_noise.match(name):
                    failed_tests.add(name)
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


if not getattr(_RestHapiDataset, "_rest_hapi_dataset_fields_shim", False):
    _rest_hapi_orig_build = _RestHapiDataset.build.__func__

    def _rest_hapi_build(cls, pr, report):
        data = _rest_hapi_orig_build(cls, pr, report)

        if (
            getattr(pr, "org", "") != "JKHeadley"
            or getattr(pr, "repo", "") != "rest-hapi"
        ):
            return data

        if not getattr(data, "lang", ""):
            data.lang = "javascript"

        instance_id = (
            getattr(pr, "instance_id", "") or f"{pr.org}__{pr.repo}-{pr.number}"
        )

        _rest_hapi_row_json = data.json

        def _rest_hapi_json_with_instance_id(*args, **kwargs):
            payload = _rest_hapi_json.loads(_rest_hapi_row_json(*args, **kwargs))
            payload["instance_id"] = instance_id
            return _rest_hapi_json.dumps(payload, ensure_ascii=False)

        data.json = _rest_hapi_json_with_instance_id
        return data

    _RestHapiDataset.build = classmethod(_rest_hapi_build)
    _RestHapiDataset._rest_hapi_dataset_fields_shim = True
