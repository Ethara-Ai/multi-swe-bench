import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL_NAME = "dayjs_1954_to_1086"
_BASE_TAG = "base-1954-to-1086"


def _title_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Dayjs1954To1086ImageBase(Image):
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
        return "node:10"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        hardening = "\n".join(
            line
            for line in Image._HARDENING_BLOCK.splitlines()
            if "git gc --prune=now" not in line and "git repack -a -d -l" not in line
        ).rstrip("\n")

        sections = [
            f"FROM {image_name}",
            self.global_env,
            "ENV CI=true",
            "WORKDIR /home/",
            fetch,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            hardening,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class Dayjs1954To1086ImageDefault(Image):
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
        return Dayjs1954To1086ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_command = """rm -f /home/jest-results.json
rc=0
npx jest --testURL http://localhost --runInBand --ci \\
    --json --outputFile=/home/jest-results.json || rc=$?"""

        emit_command = (
            "node -e '"
            'const fs=require("fs");let r={};'
            'try{r=JSON.parse(fs.readFileSync("/home/jest-results.json","utf8"))}catch(e){}'
            "const out={testResults:(r.testResults||[]).map(f=>({"
            "name:f.name,status:f.status,"
            'message:((f.message||"").trim()?"suite-failed":""),'
            "assertionResults:(f.assertionResults||[]).map(a=>({"
            'ancestorTitles:a.ancestorTitles||[],title:a.title||"",status:a.status'
            "}))}))};"
            "process.stdout.write(JSON.stringify(out));' || true"
        )

        run_script = (
            """#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC

cd /home/__REPO__
__APPLY__
__TEST__
echo "===JEST_JSON_BEGIN==="
__EMIT__
echo
echo "===JEST_JSON_END==="
exit $rc

""".replace("__REPO__", self.pr.repo)
            .replace("__TEST__", test_command)
            .replace("__EMIT__", emit_command)
        )

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true
export TZ=UTC

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

git gc --prune=now --aggressive
git repack -a -d -l --quiet

npm install --no-audit --no-fund || true

git checkout -- .
test -z "$(git status --porcelain --untracked-files=no)"

npx jest --listTests > /tmp/jest-listtests.log 2>&1 || true
if ! grep -q "test" /tmp/jest-listtests.log; then
    echo "=== JEST COLLECTED NO TESTS AT BASE COMMIT ==="
    tail -40 /tmp/jest-listtests.log
    exit 1
fi
echo "prepare: jest collects $(grep -c . /tmp/jest-listtests.log) test files"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                run_script.replace("__APPLY__", ""),
            ),
            File(
                ".",
                "test-run.sh",
                run_script.replace(
                    "__APPLY__",
                    """if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi""".format(repo=self.pr.repo),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                run_script.replace(
                    "__APPLY__",
                    """if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi""".format(repo=self.pr.repo),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        prepare_commands = "RUN bash /home/prepare.sh"

        sections = [
            f"FROM {name}:{tag}",
            self.global_env,
            copy_commands,
            prepare_commands,
            self.clear_env,
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("iamkun", _INTERVAL_NAME)
class DAYJS_1954_TO_1086(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Dayjs1954To1086ImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        begin = clean_log.rfind("===JEST_JSON_BEGIN===")
        end = clean_log.rfind("===JEST_JSON_END===")
        if begin == -1 or end <= begin:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        payload = clean_log[begin + len("===JEST_JSON_BEGIN===") : end].strip()
        try:
            report = json.loads(payload)
        except ValueError:
            report = {}

        for file_report in report.get("testResults") or []:
            path = str(file_report.get("name") or "").replace("\\", "/")
            marker = f"/home/{self.pr.repo}/"
            index = path.find(marker)
            if index != -1:
                path = path[index + len(marker) :]

            assertions = file_report.get("assertionResults") or []
            status = str(file_report.get("status") or "")
            message = str(file_report.get("message") or "").strip()
            sentinel = f"{path} > [suite loads]"
            if assertions:
                passed_tests.add(sentinel)
            elif status == "failed" or message:
                failed_tests.add(sentinel)
            else:
                skipped_tests.add(sentinel)

            occurrences: dict[str, int] = {}

            for assertion in assertions:
                titles = [
                    title
                    for title in (
                        _title_text(value)
                        for value in [
                            *(assertion.get("ancestorTitles") or []),
                            assertion.get("title"),
                        ]
                    )
                    if title and title.strip()
                ]
                if not titles:
                    continue

                name = re.sub(r"\s+", " ", f"{path} > " + " > ".join(titles)).strip()
                occurrences[name] = occurrences.get(name, 0) + 1
                if occurrences[name] > 1:
                    name = f"{name} [dup#{occurrences[name]}]"

                status = assertion.get("status") or ""
                if status == "failed":
                    failed_tests.add(name)
                elif status == "passed":
                    passed_tests.add(name)
                else:
                    skipped_tests.add(name)

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
