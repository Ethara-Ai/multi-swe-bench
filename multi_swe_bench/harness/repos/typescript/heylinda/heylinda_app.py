import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:14"
_BASE_TAG = "base"
_RESULTS_MARKER = "----- per-test results -----"

_PASSED = re.compile(r"^(jest::.+?)\s+PASSED$", re.M)
_FAILED = re.compile(r"^(jest::.+?)\s+FAILED$", re.M)
_SKIPPED = re.compile(r"^(jest::.+?)\s+SKIPPED$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RUN_TESTS_BODY = r"""RESULTS=/tmp/jest-results.txt
: > "$RESULTS"
SUITES=$(npx jest --listTests < /dev/null | sed "s#^$PWD/##" | sort -u)
for suite in $SUITES; do
  out="/tmp/jest-$(echo "$suite" | tr / _).json"
  rm -f "$out"
  npx jest --runTestsByPath "$suite" --json --outputFile="$out" < /dev/null
  SUITE="$suite" OUT="$out" node >> "$RESULTS" <<'JS'
const fs = require('fs');
const file = process.env.SUITE;
const status = { passed: 'PASSED', failed: 'FAILED' };
let results = [];
try { results = JSON.parse(fs.readFileSync(process.env.OUT, 'utf8')).testResults; } catch (e) {}
const lines = results
  .flatMap((r) => r.assertionResults)
  .map((a) => `jest::${file}::${a.fullName.replace(/\n/g, ' ')} ${status[a.status] || 'SKIPPED'}`);
const errors = results
  .filter((r) => r.status === 'failed' && !r.assertionResults.some((a) => a.status === 'failed'))
  .map(() => `jest::${file}::SUITE_ERROR FAILED`);
const missing = [`jest::${file}::SUITE_ERROR FAILED`].slice(Math.min(1, lines.length + errors.length));
console.log([...lines, ...errors, ...missing].join('\n'));
JS
done
"""


def _run_tests_sh(repo: str) -> str:
    return (
        "#!/bin/bash\n"
        f"cd /home/{repo}\n"
        + _RUN_TESTS_BODY
        + f'echo "{_RESULTS_MARKER}"\n'
        + 'cat "$RESULTS"\n'
    )


class HeyLindaAppImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class HeyLindaAppImageDefault(Image):
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
        return HeyLindaAppImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "yarn install --frozen-lockfile\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
            File(".", "run_tests.sh", _run_tests_sh(repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("heylinda", "heylinda-app")
class HeyLindaApp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HeyLindaAppImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        section = _ANSI.sub("", test_log).rsplit(_RESULTS_MARKER, 1)[-1]
        failed_tests = set(_FAILED.findall(section))
        passed_tests = set(_PASSED.findall(section)) - failed_tests
        skipped_tests = set(_SKIPPED.findall(section)) - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
