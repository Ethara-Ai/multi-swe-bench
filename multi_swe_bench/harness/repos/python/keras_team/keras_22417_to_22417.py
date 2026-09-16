import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ERA_RANGE = "22417-to-22417"

_PYTHON_IMAGE = "python:3.11"
_TENSORFLOW_PIN = "2.21.0"
_GRAIN_PIN = "0.2.16"
_BACKEND = "tensorflow"


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

    def dependency(self) -> str:
        return _PYTHON_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        from multi_swe_bench.harness.image import DockerfileEnhancer

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/"
                f"{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        raw = f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""

        class _Shim:
            pr = self.pr

            @staticmethod
            def dependency():
                return image_name

            @staticmethod
            def dockerfile():
                return raw

        enhanced = DockerfileEnhancer.enhance(_Shim())

        scrub_start = enhanced.find("RUN git reset --hard")
        cmd_start = enhanced.find('CMD ["/bin/bash"]')
        if scrub_start != -1 and cmd_start != -1 and scrub_start < cmd_start:
            enhanced = enhanced[:scrub_start] + enhanced[cmd_start:]
        return enhanced


_RUN_TESTS_BODY = r"""set +e
set -uo pipefail

export CI=true
export KERAS_BACKEND="${KERAS_BACKEND:-__BACKEND__}"
TEST_TIMEOUT="${KR_TEST_TIMEOUT:-900}"
MIN_RESULTS="${KR_MIN_RESULTS:-0}"

TEST_FILES=$(python3 - /home/test.patch /home/fix.patch <<'PY_EXTRACT_TARGETS'
import re
import sys

test_files = set()
for patch_path in sys.argv[1:]:
    try:
        content = open(patch_path).read()
    except (IOError, OSError):
        continue
    for m in re.finditer(r'^diff --git a/(\S+) b/(\S+)', content, re.M):
        path = m.group(2)
        if path.endswith('_test.py') or '/test_' in path or '/tests/' in path:
            test_files.add(path)

print(' '.join(sorted(test_files)))
PY_EXTRACT_TARGETS
)

EXISTING=""
for f in $TEST_FILES; do
  [ -f "$f" ] && EXISTING="$EXISTING $f"
done
EXISTING=$(echo "$EXISTING" | xargs)

echo "KR RUNNER: backend       = $KERAS_BACKEND"
echo "KR RUNNER: target files  = [$TEST_FILES]"
echo "KR RUNNER: existing here = [$EXISTING]"

if [ -z "$EXISTING" ]; then
  echo "KR RUNNER: no target test file exists in this tree; nothing to measure"
  exit 0
fi

: > /tmp/kr-test.log
pytest --no-header -rA --tb=short -p no:cacheprovider -v \
  --timeout="$TEST_TIMEOUT" $EXISTING >> /tmp/kr-test.log 2>&1
rc=$?
cat /tmp/kr-test.log

results=$(grep -cE "::.*(PASSED|FAILED|SKIPPED|ERROR)|^(PASSED|FAILED|SKIPPED|ERROR) " /tmp/kr-test.log)
echo "KR RUNNER: pytest rc=$rc, $results result lines collected"

if [ "$rc" -ge 2 ]; then
  echo "KR RUNNER: INFRASTRUCTURE FAILURE: pytest exited $rc, which is not a test failure"
  echo "KR RUNNER: the results above are not trustworthy"
  exit "$rc"
fi

if [ "$results" -lt "$MIN_RESULTS" ]; then
  echo "KR RUNNER: INFRASTRUCTURE FAILURE: collected $results result lines, expected at least $MIN_RESULTS"
  exit 1
fi

exit 0
"""


_STAGE_HEADER = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/__REPO__
git checkout -- . 2>/dev/null || true

"""


_FILTER_PATCH_FN = r"""filter_patch() {
  python3 - "$1" <<'PY_FILTER_PATCH'
import re
import sys

if len(sys.argv) < 2:
    sys.exit(1)

try:
    content = open(sys.argv[1]).read()
except (IOError, OSError):
    sys.exit(1)

if not content.strip():
    sys.exit(1)

parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
filtered = [p for p in parts if p.strip() and 'Binary files' not in p]
result = ''.join(filtered)

if result.strip():
    sys.stdout.write(result)
else:
    sys.exit(1)
PY_FILTER_PATCH
}
"""


_APPLY_TEST = r"""filter_patch /home/test.patch > /tmp/filtered_test.patch
git apply --whitespace=nowarn /tmp/filtered_test.patch
"""


_APPLY_FIX = r"""filter_patch /home/fix.patch > /tmp/filtered_fix.patch
git apply --whitespace=nowarn /tmp/filtered_fix.patch
"""


_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _prepare_sh(pr: PullRequest) -> str:
    return "\n".join(
        [
            "#!/bin/bash",
            "set -e",
            "",
            f"cd /home/{pr.repo}",
            "git reset --hard",
            "git clean -fdx",
            "bash /home/check_git_changes.sh",
            f"git checkout --detach {pr.base.sha}",
            "bash /home/check_git_changes.sh",
            "",
            f'TF_PKG="tensorflow-cpu=={_TENSORFLOW_PIN}"',
            f'[ "$(uname -m)" = x86_64 ] || TF_PKG="tensorflow=={_TENSORFLOW_PIN}"',
            "",
            "pip install --no-cache-dir --upgrade pip setuptools || true",
            "pip install --no-cache-dir pytest pytest-timeout absl-py scipy "
            f'"grain=={_GRAIN_PIN}" "$TF_PKG" || true',
            "pip uninstall -y keras keras-nightly || true",
            "pip install --no-cache-dir . || true",
            "",
            'python3 -c "import grain; grain.MapDataset; '
            'grain.sources.RandomAccessDataSource"',
            f'KERAS_BACKEND={_BACKEND} python3 -c "import keras"',
            f'KERAS_BACKEND={_BACKEND} python3 -c "import pytest, keras; '
            'from keras.src.layers.preprocessing import discretization, '
            'index_lookup, text_vectorization"',
            "",
        ]
    )


_PR_SCRUB = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
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

    def dependency(self) -> "ImageBase":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _STAGE_HEADER.replace("__REPO__", self.pr.repo)
        body = _RUN_TESTS_BODY.replace("__BACKEND__", _BACKEND)

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
                _CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                _prepare_sh(self.pr),
            ),
            File(
                ".",
                "run.sh",
                stage + body,
            ),
            File(
                ".",
                "test-run.sh",
                stage + _FILTER_PATCH_FN + _APPLY_TEST + body,
            ),
            File(
                ".",
                "fix-run.sh",
                stage + _FILTER_PATCH_FN + _APPLY_TEST + _APPLY_FIX + body,
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

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{_PR_SCRUB}
{self.clear_env}

"""


@Instance.register("keras-team", "keras_22417_to_22417")
class KERAS_22417_TO_22417(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log_clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log)

        pattern = re.compile(
            r"^(?P<fname>\S+\.py::\S.*?)\s+(?P<fstatus>PASSED|FAILED|SKIPPED|ERROR)(?:\s|$)"
            r"|"
            r"^(?P<rstatus>PASSED|FAILED|SKIPPED|ERROR)\s+(?P<rname>\S+\.py::\S.*?)"
            r"(?:\s+-\s.*)?$",
            re.M,
        )

        for match in pattern.finditer(log_clean):
            if match.group("fname"):
                test_name = match.group("fname").strip()
                status = match.group("fstatus")
            elif match.group("rname"):
                test_name = match.group("rname").strip()
                status = match.group("rstatus")
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

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
