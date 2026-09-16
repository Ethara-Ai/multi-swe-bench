
import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JSON_BEGIN = "-----MSB-JEST-JSON-BEGIN-----"
_JSON_END = "-----MSB-JEST-JSON-END-----"

_REPO_ROOT = "/home/svelte-preprocess/"

_TEST_BODY = """\
rm -f /tmp/jest-results.json /tmp/jest-stdio.log

set +e
./node_modules/.bin/jest \\
    --ci \\
    --no-cache \\
    --runInBand \\
    --silent \\
    --coverage=false \\
    --json \\
    --outputFile=/tmp/jest-results.json \\
    > /tmp/jest-stdio.log 2>&1
JEST_RC=$?
set -e

cat /tmp/jest-stdio.log

if [ ! -s /tmp/jest-results.json ]; then
    echo "Error: jest wrote no result file (exit ${{JEST_RC}})" >&2
    exit 1
fi

echo "{begin}"
cat /tmp/jest-results.json
echo
echo "{end}"
""".format(begin=_JSON_BEGIN, end=_JSON_END)


class SveltePreprocessImageBase(Image):

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
        return "node:14-bullseye"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git python2 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SveltePreprocessImageDefault(Image):

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
        return SveltePreprocessImageBase(self.pr, self._config)

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
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --non-interactive || true

./node_modules/.bin/jest \\
    --ci --no-cache --runInBand --silent --coverage=false \\
    --json --outputFile=/tmp/jest-warmup.json > /tmp/jest-warmup.log 2>&1 || true
tail -n 6 /tmp/jest-warmup.log
test -s /tmp/jest-warmup.json

bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
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


_SKIPPED_STATUSES = frozenset({"pending", "todo", "disabled", "skipped"})

_JSON_SPAN = re.compile(
    re.escape(_JSON_BEGIN) + r"\s*(.*?)\s*" + re.escape(_JSON_END),
    re.DOTALL,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_jest_json_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    matches = _JSON_SPAN.findall(log)
    if not matches:
        return TestResult(0, 0, 0, set(), set(), set())

    try:
        report = json.loads(_ANSI_ESCAPE.sub("", matches[-1]))
    except json.JSONDecodeError:
        return TestResult(0, 0, 0, set(), set(), set())

    for suite in report.get("testResults") or []:
        path = suite.get("name") or ""
        if _REPO_ROOT in path:
            path = path.split(_REPO_ROOT, 1)[1]

        seen: dict[str, int] = {}

        for case in suite.get("assertionResults") or []:
            parts = [path]
            parts.extend(case.get("ancestorTitles") or [])
            parts.append(case.get("title") or "")
            name = " > ".join(p for p in parts if p)

            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [#{seen[name]}]"

            status = case.get("status")
            if status == "passed":
                passed_tests.add(name)
            elif status == "failed":
                failed_tests.add(name)
            elif status in _SKIPPED_STATUSES:
                skipped_tests.add(name)

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


@Instance.register("sveltejs", "svelte-preprocess")
class SveltePreprocess(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SveltePreprocessImageDefault(self.pr, self._config)

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
        return parse_jest_json_log(log)
