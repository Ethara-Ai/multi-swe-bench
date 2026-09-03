import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch_text: str) -> str:
    """Drop diff sections ``git apply`` cannot consume.

    ``git apply`` is atomic: a single unusable section rejects the whole patch
    and the stage produces no output at all. Two of this repo's test patches
    (PR 3182 and PR 3252) carry payload-less binary sections of the shape::

        diff --git a/test/expect/ModelTester...pkl b/test/expect/ModelTester...pkl
        new file mode 100644
        index 00000000000..9691daf18c7
        Binary files /dev/null and b/test/expect/ModelTester...pkl differ

    with no literal payload, so git aborts with ``cannot apply binary patch to
    '<f>' without full index line``. Those sections are dropped here.

    Cost of the drop: the ``mobilenet_v3`` expectation pickles never land, so
    the ``ModelTester`` cases that read them cannot pass in any stage. They are
    not the gold tests of either PR (that is the norm-layer test), and because
    they are absent from the test stage and the fix stage alike they cannot
    manufacture a spurious transition.

    The section header is parsed non-greedily so that paths containing spaces
    are split correctly and binary payload cannot leak into the previous
    section.
    """
    if not patch_text:
        return patch_text

    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    binary_marker = re.compile(r"^Binary files .* differ$")

    preamble: list[str] = []
    sections: list[list[str]] = []
    current: Optional[list[str]] = None

    for line in patch_text.split("\n"):
        if header.match(line):
            if current is not None:
                sections.append(current)
            current = [line]
            continue
        if current is None:
            preamble.append(line)
        else:
            current.append(line)

    if current is not None:
        sections.append(current)

    kept = preamble[:]
    for section in sections:
        if any(binary_marker.match(entry) for entry in section):
            continue
        kept.extend(section)

    return "\n".join(kept)


class VisionImageBase(Image):
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
        return "python:3.8-slim"

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

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ninja-build \\
    pkg-config \\
    libjpeg-dev \\
    libpng-dev \\
    zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class VisionImageDefault(Image):
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
        return VisionImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # torchvision is built from source against a contemporary CPU build of
        # torch; the era pins are written inline here because they are only
        # meaningful at this point of use.
        # The "+cpu" local version is published for x86_64 only, so the aarch64
        # half of a multi-architecture build needs the plain version instead.
        if self.pr.number <= 1911:
            torch_pin = "torch==1.5.1+cpu"
            torch_pin_aarch64 = "torch==1.5.1"
            test_files = "test/test_models_detection_utils.py test/test_models_detection_negative_samples.py"
        elif self.pr.number <= 2459:
            torch_pin = "torch==1.6.0+cpu"
            torch_pin_aarch64 = "torch==1.6.0"
            test_files = "test/test_functional_tensor.py test/test_transforms.py"
        elif self.pr.number <= 3252:
            torch_pin = "torch==1.8.0+cpu"
            torch_pin_aarch64 = "torch==1.8.0"
            test_files = "test/test_models.py"
        else:
            torch_pin = "torch==1.10.0+cpu"
            torch_pin_aarch64 = "torch==1.10.0"
            test_files = "test/test_models_detection_utils.py test/test_backbone_utils.py"

        # The pytest invocation must stay character-identical across run.sh,
        # test-run.sh and fix-run.sh. The existence filter keeps the untouched
        # run stage from aborting on a test file the test patch has not yet
        # created, without changing the command itself.
        # --continue-on-collection-errors is required because a gold test file
        # may import a module the fix patch introduces; without it pytest
        # reports "Interrupted: 1 error during collection" and abandons the
        # whole session, so the test stage records nothing at all rather than
        # the results of the files that did import.
        test_commands = """TEST_FILES="{test_files}"
TEST_TARGETS=""
for candidate in $TEST_FILES; do
  if [ -e "$candidate" ]; then
    TEST_TARGETS="$TEST_TARGETS $candidate"
  fi
done
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors $TEST_TARGETS
""".format(test_files=test_files)

        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(
                ".",
                "fix.patch",
                f"{filtered_fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{filtered_test_patch}",
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

# The base image is shared by every pull request of this repository and is
# pinned, pruned and stripped of its remote by the pipeline, so the commit of
# this pull request is not guaranteed to survive in it. Re-acquire it here.
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/{pr.org}/{pr.repo}.git
git fetch --depth 1 origin {pr.base.sha}

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export FORCE_CUDA=0
export USE_FFMPEG=0
export MAX_JOBS=8
export SETUPTOOLS_USE_DISTUTILS=stdlib

python -m pip install --no-cache-dir --upgrade "pip<24.1" "setuptools<60" "wheel" || true
if [ "$(uname -m)" = "x86_64" ]; then
python -m pip install --no-cache-dir {torch_pin} -f https://download.pytorch.org/whl/torch_stable.html || true
else
python -m pip install --no-cache-dir {torch_pin_aarch64} -f https://torch.kmtea.eu/whl/stable.html || true
fi
python -m pip install --no-cache-dir "numpy<2" "pillow<9" "scipy" "pytest==7.4.4" || true
python -m pip install --no-cache-dir --no-build-isolation -e .

bash /home/check_git_changes.sh

""".format(
                    pr=self.pr,
                    torch_pin=torch_pin,
                    torch_pin_aarch64=torch_pin_aarch64,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{test_commands}
""".format(pr=self.pr, test_commands=test_commands),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn -C1 /home/test.patch
{test_commands}
""".format(pr=self.pr, test_commands=test_commands),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn -C1 /home/test.patch /home/fix.patch
{test_commands}
""".format(pr=self.pr, test_commands=test_commands),
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


@Instance.register("pytorch", "vision")
class Vision(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VisionImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        clean_log = ansi_escape.sub("", test_log)

        # pytest -v verbose lines:
        #   test/test_models.py::TestName::test_case PASSED       [ 12%]
        # pytest -rA short-summary lines:
        #   PASSED test/test_models.py::TestName::test_case
        re_passes = [
            re.compile(r"^(\S+::\S+)\s+PASSED\b"),
            re.compile(r"^PASSED\s+(\S+::\S+)"),
        ]
        re_fails = [
            re.compile(r"^(\S+::\S+)\s+(?:FAILED|ERROR)\b"),
            re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)"),
        ]
        re_skips = [
            re.compile(r"^(\S+::\S+)\s+(?:SKIPPED|XFAIL|XPASS)\b"),
            re.compile(r"^(?:SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"),
        ]

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass in re_passes:
                pass_match = re_pass.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail in re_fails:
                fail_match = re_fail.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

            for re_skip in re_skips:
                skip_match = re_skip.match(line)
                if skip_match:
                    test = skip_match.group(1).strip()
                    skipped_tests.add(test)

        # Remove any overlap (a test name should only appear once)
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
