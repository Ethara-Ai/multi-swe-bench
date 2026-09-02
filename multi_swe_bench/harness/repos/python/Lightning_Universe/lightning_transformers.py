from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Base commit is 2021-03-30. CI (.github/workflows/ci_testing.yml) runs a
# python 3.6/3.8 matrix, and setup.py declares python_requires>=3.6.
PYTHON_IMAGE = "python:3.8-slim-bullseye"

# ONE command, reached by run.sh / test-run.sh / fix-run.sh through
# run_tests.sh, so the three graded stages cannot drift apart.
#
# --override-ini=addopts= : setup.cfg sets `addopts = --strict --doctest-modules`.
#     --doctest-modules would import and doctest every module in the package -
#     slow, and it turns an unrelated import error into a collection failure.
# -k "not <3 e2e names>"  : those existing tests drive REAL HuggingFace training
#     through the `script_runner` fixture (prajjwal1/bert-tiny + the `emotion`
#     dataset). They need network and minutes of compute and are unrelated to
#     this PR. The three names below are the complete set, and none of them
#     occurs in the ballast files, so a global -k is safe.
# --junitxml               : machine-readable; never parse pytest console text.
DESELECT = (
    "not test_smoke_train_e2e "
    "and not test_smoke_predict_e2e "
    "and not test_predict_from_ckpt_path"
)
TEST_CMD = (
    "python -m pytest tests/ -v --tb=short "
    "--override-ini=addopts= -p no:cacheprovider "
    "--continue-on-collection-errors "
    f'-k "{DESELECT}" '
    "--junitxml=/home/results.xml"
)

BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

# ---------------------------------------------------------------------------
# requirements.txt in the repo is almost entirely unpinned (`torch>=1.6`,
# `pytorch-lightning>=1.2.4`, bare `transformers`, bare `datasets`). Resolving
# it today yields torch 2.x / PL 2.x / transformers 4.4x, none of which work
# with this 2021 code. Upstream CI handled it with a "minimal" job that rewrote
# `>=` into `==`; that still leaves the bare entries floating, so this file
# pins the whole set to the base commit's era (March 2021) instead.
#
# torch comes from PyPI, not the PyTorch CPU index. The +cpu wheel is far
# smaller (~170 MB vs ~800 MB), but that index publishes ONLY
# cp38-linux_x86_64 / cp38-win_amd64 - no arm64 - which would break any future
# multi-arch build. PyPI's torch==1.8.1 ships cp38-manylinux2014_aarch64
# alongside manylinux1_x86_64, so it resolves on both architectures. The larger
# download is covered by PIP_DEFAULT_TIMEOUT=120 and --retries 10.
PINNED_REQUIREMENTS = """torch==1.8.1
pytorch-lightning==1.2.4
torchmetrics==0.2.0
transformers==4.5.1
datasets==1.6.0
hydra-core==1.1.0.dev5
omegaconf==2.1.0.dev24
fairscale==0.3.7
rouge-score==0.0.4
sentencepiece==0.1.95
protobuf==3.20.3
pytest==6.2.5

# Transitive deps that MUST be era-pinned too - left floating they resolve to
# modern releases that break this 2021 stack:
#   packaging - transformers 4.5.1 `require_version` calls
#     version.parse("0.10.1,<0.11") on its own dep spec. packaging <22 returned
#     a LegacyVersion for that; packaging >=22 raises InvalidVersion, so
#     `import transformers` dies. Observed with packaging-26.2.
#   numpy     - torch 1.8.1 wheels are built against the numpy 1.19/1.20 ABI,
#     and numpy >=1.24 removed the np.object/np.bool aliases this era uses.
packaging==20.9
numpy==1.20.3
#   tokenizers - transformers 4.5.1 allows >=0.10.1,<0.11 and pip picks the
#     newest, 0.10.3, which ships NO aarch64 wheel. tokenizers is Rust, so
#     arm64 would fall back to a source build and fail without a Rust
#     toolchain. 0.10.2 satisfies the same range AND publishes
#     cp38-manylinux2014_aarch64, so the pin is what makes multi-arch possible.
tokenizers==0.10.2
"""

PARSE_JUNIT_PY = '''import os
import xml.etree.ElementTree as ET

PATH = "/home/results.xml"

if os.path.exists(PATH):
    try:
        root = ET.parse(PATH).getroot()
    except ET.ParseError:
        root = None

    if root is not None:
        for tc in root.iter("testcase"):
            # The @file attribute is essential here: test_model_can_be_created
            # and test_datamodule_has_correct_cfg recur across all 7 task test
            # files, so a bare test name would collapse them into one id.
            path = tc.get("file")
            if not path:
                classname = tc.get("classname") or ""
                path = classname.replace(".", "/") + ".py"
            name = tc.get("name") or ""
            name = name.replace("\\r", " ").replace("\\n", " ")

            status = "PASSED"
            for child in tc:
                if child.tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if child.tag == "skipped":
                    status = "SKIPPED"
                    break

            print("TESTCASE " + path + "::" + name + " " + status)
'''

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

    def dependency(self) -> str | Image:
        return PYTHON_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # Per-PR, never shared: the injected hardening block checks out
        # ${BASE_COMMIT} then deletes every git ref, so a shared base tag would
        # stay pinned to whichever PR built it first.
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

        # Do NOT set DEBIAN_FRONTEND or LANG - DockerfileEnhancer injects both.
        # ca-certificates is listed explicitly rather than inherited silently.
        # build-essential is genuinely needed: several era-pinned deps
        # (sentencepiece, rouge-score, tokenizers) have no cp38 wheel for every
        # platform and fall back to a source build.
        return f"""FROM {image_name}

{self.global_env}

ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
 && rm -rf /var/lib/apt/lists/*

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
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "pinned-requirements.txt", PINNED_REQUIREMENTS),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "parse_junit.py", PARSE_JUNIT_PY),
            File(
                ".",
                "print_test_detail.sh",
                "#!/bin/bash\n"
                f'echo "{BEGIN_MARKER}"\n'
                "python /home/parse_junit.py\n"
                f'echo "{END_MARKER}"\n',
            ),
            File(
                ".",
                "run_tests.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                "export CI=true\n"
                "\n"
                f"cd /home/{repo}\n"
                "\n"
                "# never inherit the previous stage's results\n"
                "rm -f /home/results.xml\n"
                "\n"
                # -e is lifted only around the test call: at the test stage the
                # suite is SUPPOSED to fail, and dying here would report zero
                # tests and satisfy report.py's "fix something" check vacuously.
                "set +e\n"
                f"{TEST_CMD}\n"
                "RC=$?\n"
                "set -e\n"
                'echo "TEST_EXIT_CODE=$RC"\n'
                "\n"
                "bash /home/print_test_detail.sh\n",
            ),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                f"cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fdx\n"
                "bash /home/check_git_changes.sh\n"
                f"git checkout {sha}\n"
                "bash /home/check_git_changes.sh\n"
                "\n"
                "python --version\n"
                # pip >=24.1 REJECTS the omegaconf 2.1.0.dev24 wheel that
                # hydra-core 1.1.0.dev5 requires - its metadata declares
                # `PyYAML (>=5.1.*)`, which the newer resolver treats as
                # invalid. pip itself prints "Please use pip<24.1". Verified by
                # a dry-run: pip 23.0.1 resolves the set, pip 25 does not.
                'pip install --no-cache-dir "pip<24.1"\n'
                "python -m pip --version\n"
                "\n"
                # --pre is required: hydra-core 1.1.0.dev5 / omegaconf
                # 2.1.0.dev24 are pre-releases.
                "python -m pip install --no-cache-dir --pre --retries 10 "
                "-r /home/pinned-requirements.txt\n"
                "\n"
                # --no-deps: the pinned set above is authoritative. Letting
                # setup.py re-resolve requirements.txt would drag in modern
                # torch/transformers and undo the era pinning.
                f"python -m pip install --no-cache-dir --no-deps -e /home/{repo}\n"
                "\n"
                # HARD GATE - not tolerant. If the base tree cannot import or
                # collect, no stage can produce results, and that must fail HERE
                # rather than surfacing as an unexplained empty report later.
                'python -c "import torch, pytorch_lightning, transformers, datasets, hydra; '
                'import lightning_transformers; print(\'imports ok\')"\n'
                f"cd /home/{repo} && python -m pytest tests/ --collect-only -q "
                "--override-ini=addopts= -p no:cacheprovider > /dev/null\n"
                "\n"
                'echo "DEPS_OK"\n',
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "bash /home/run_tests.sh\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch; then\n"
                '    echo "Error: git apply test.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "bash /home/run_tests.sh\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                '    echo "Error: git apply test.patch+fix.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "bash /home/run_tests.sh\n",
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


@Instance.register("Lightning-Universe", "lightning-transformers")
class LightningTransformers(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
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

        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Greedy id capture with the status as the FINAL token; pytest
        # parametrized ids can contain spaces, which a \S+ capture would
        # silently truncate.
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()

            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            # Everything outside the markers is raw pytest output and is
            # ignored, so the "FAILED <id> - <error>" summary lines can never
            # pollute a test id.
            if not in_detail:
                continue

            m = case_re.match(stripped)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Failure wins; each test lands in exactly one bucket.
        passed_tests -= failed_tests
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
