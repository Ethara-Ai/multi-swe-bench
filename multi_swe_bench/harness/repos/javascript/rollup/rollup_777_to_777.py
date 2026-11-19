import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-777_to_777"
_TEST_ENTRYPOINT = "test/test.js"


_PR_PINS = {}


_BASE_DOCKERFILE = r"""FROM __BASE_IMAGE__

ENV LC_ALL=C.UTF-8 \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

RUN node --version && npm --version && git --version

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_CLONE_CODE = r"""RUN git clone https://github.com/__ORG__/__REPO__.git /home/__REPO__"""


_COPY_CODE = r"""COPY __REPO__ /home/__REPO__"""


_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

WORKDIR /home/__REPO__

__HARDENING__
__CLEAR_ENV__
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_BUILD_BLOCK = r"""npm run build
if node -e "process.exit(require('./package.json').scripts['build:cli'] ? 0 : 1)"; then npm run build:cli; fi"""


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true

node --version
npm --version

before="$(git log -1 --format=%cI HEAD)"
for attempt in 1 2 3; do
    npm install --ignore-scripts --before="${before}" && break || true
    echo "prepare: npm install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done

__FIX_DEPS__test -x node_modules/.bin/mocha || { echo "prepare: mocha missing" >&2; exit 1; }

__BUILD_BLOCK__
"""


_TEST_BLOCK = r"""__BUILD_BLOCK__

node_modules/.bin/mocha --reporter json
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --3way --exclude=package-lock.json --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --3way --exclude=package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


class ROLLUP_777_TO_777_ImageBase(Image):

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
        return "node:8-buster"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class ROLLUP_777_TO_777_ImageDefault(Image):

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
        return ROLLUP_777_TO_777_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__BUILD_BLOCK__", _BUILD_BLOCK)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__FIX_DEPS__", self._pins())
        )

    def _pins(self) -> str:
        pins = _PR_PINS.get(self.pr.number)
        if not pins:
            return ""
        return f"npm install --no-save --ignore-scripts --legacy-peer-deps {pins}\n\n"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = (
            Image._HARDENING_BLOCK
            .replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', self.pr.base.sha)
        )

        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NOISE_RE = re.compile(r"^(=+|Writing .*|npm ERR!.*|npm WARN.*|npm notice.*)$", re.MULTILINE)


def _parse_log(test_log: str, repo: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    test_log = _ANSI_RE.sub("", test_log)
    test_log = _NOISE_RE.sub("", test_log)
    test_log = test_log.replace("\r\n", "").replace("\n", "")

    repo_prefix = f"/home/{repo}/"

    def test_name(test: dict) -> str:
        title = test.get("fullTitle") or test.get("title") or ""
        path = test.get("file") or _TEST_ENTRYPOINT
        if path.startswith(repo_prefix):
            path = path[len(repo_prefix):]
        return f"{path}::{title}" if title else ""

    decoder = json.JSONDecoder()
    pos = 0
    while True:
        start = test_log.find("{", pos)
        if start == -1:
            break
        try:
            result, index = decoder.raw_decode(test_log[start:])
        except ValueError:
            pos = start + 1
            continue
        if isinstance(result, dict) and "stats" in result:
            for bucket, key in (
                (passed_tests, "passes"),
                (failed_tests, "failures"),
                (skipped_tests, "pending"),
            ):
                for test in result.get(key, []):
                    name = test_name(test)
                    if name:
                        bucket.add(name)
        pos = start + index

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


@Instance.register("rollup", "rollup_777_to_777")
class ROLLUP_777_TO_777(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ROLLUP_777_TO_777_ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log, self.pr.repo)
