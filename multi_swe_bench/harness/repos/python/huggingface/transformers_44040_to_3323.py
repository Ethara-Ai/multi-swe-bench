import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


def _strip_binary_diffs(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(s for s in sections if s and "Binary files " not in s)


_TEST_BODY = """
set +e
TEST_FILES=$(grep -E '^\\+\\+\\+ b/' /home/test.patch \\
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \\
    | grep -E '(^|/)(test_[^/]*\\.py|[^/]*_test\\.py)$' \\
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        TEST_TARGETS="$TEST_TARGETS $f"
    fi
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

echo "running pytest on:$TEST_TARGETS"
python -m pytest -v -rA --no-header --tb=short -p no:cacheprovider -p no:rich -o log_cli=false --continue-on-collection-errors $TEST_TARGETS
"""

_PREPARE = """#!/bin/bash
set -e

git clone https://github.com/[[ORG]]/[[REPO]].git /home/[[REPO]]
cd /home/[[REPO]]
git cat-file -e [[SHA]]^{commit} 2>/dev/null \\
    || git fetch --no-tags origin "+refs/pull/[[NUMBER]]/head:refs/remotes/pull/[[NUMBER]]"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout [[SHA]]
bash /home/check_git_changes.sh

set -eux
git checkout --detach [[SHA]]
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
set +ux

pip install --no-deps --no-cache-dir -e . || true
python /home/install_deps.py || true
pip install --no-cache-dir [[PYTEST_PIN]] pytest-xdist timeout-decorator psutil parameterized || true
pip install --no-cache-dir 'huggingface-hub' || true
pip install --no-cache-dir boto3 sentencepiece importlib_metadata sacremoses tokenizers accelerate torchvision || true
pip install --no-cache-dir jax jaxlib fire pydantic nltk timm pytorch_lightning onnxruntime 'pytest-asyncio<0.22' openai || true
pip install --no-deps --no-cache-dir datasets || true
pip install --no-cache-dir --no-deps evaluate || true
pip install --no-cache-dir scikit-learn librosa phonemizer || true
cp /home/hub_compat.py "$(python -c 'import site; print(site.getsitepackages()[0])')/sitecustomize.py" || true
find src/ tests/ -name '*.py' -exec sed -i 's/from collections import Sequence/from collections.abc import Sequence/g; s/from collections import Mapping/from collections.abc import Mapping/g; s/from collections import MutableMapping/from collections.abc import MutableMapping/g' {} + || true
if [ -f src/transformers/dependency_versions_check.py ]; then
    python -c "import pathlib; p=pathlib.Path('src/transformers/dependency_versions_check.py'); t=p.read_text(); p.write_text(t.replace('require_version_core(deps[pkg])', 'pass  # require_version_core(deps[pkg])'))" || true
fi
pip install --no-cache-dir 'safetensors' || true
pip install --no-cache-dir 'tensorflow-cpu' || pip install --no-cache-dir 'tensorflow' || true
pip install --no-cache-dir 'tf-keras' || true
pip install --no-cache-dir [[TORCH_PIN]] --index-url https://download.pytorch.org/whl/cpu \\
    || pip install --no-cache-dir [[TORCH_PIN]] \\
    || true
[[SAVE_STATE_PATCH]]
pip install --no-cache-dir [[PYTEST_PIN]] || true
python -c "import transformers; print(transformers.__version__)" || true

"""

_SAVE_STATE_PATCH = """if grep -rq 'SAVE_STATE_WARNING' src/transformers/ 2>/dev/null; then
    find src/transformers/ -name '*.py' -exec sed -i \\
        's/from torch.optim.lr_scheduler import SAVE_STATE_WARNING/SAVE_STATE_WARNING = ""/' {} + || true
fi"""


class HuggingFaceTransformersImageBase(Image):
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
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "envagent"

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

        return f"""FROM {image_name}

{self.global_env}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
ENV HF_HUB_DISABLE_TELEMETRY=1
ENV TOKENIZERS_PARALLELISM=false

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    libffi-dev \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

{self.clear_env}

CMD ["/bin/bash"]
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
        return HuggingFaceTransformersImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_script(self) -> str:
        if self.pr.number <= 25000:
            torch_pin = "'torch<2.4'"
            save_state_patch = _SAVE_STATE_PATCH
        else:
            torch_pin = "'torch'"
            save_state_patch = ""

        return (
            _PREPARE.replace("[[ORG]]", self.pr.org)
            .replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[NUMBER]]", str(self.pr.number))
            .replace("[[PYTEST_PIN]]", "'pytest<8.0'")
            .replace("[[TORCH_PIN]]", torch_pin)
            .replace("[[SAVE_STATE_PATCH]]", save_state_patch)
        )

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        return [
            File(".", "fix.patch", _strip_binary_diffs(self.pr.fix_patch)),
            File(".", "test.patch", _strip_binary_diffs(self.pr.test_patch)),
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
            File(".", "prepare.sh", self._prepare_script()),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
""".replace("[[REPO]]", repo_name)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo_name)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi
""".replace("[[REPO]]", repo_name)
                + _TEST_BODY,
            ),
            File(
                ".",
                "install_deps.py",
                'import re, subprocess, pathlib\n'
                'content = ""\n'
                'for f in ["setup.py", "setup.cfg", "pyproject.toml"]:\n'
                '    p = pathlib.Path(f)\n'
                '    if p.exists(): content += p.read_text()\n'
                "deps = re.findall(r'[\"\\x27]([a-zA-Z][a-zA-Z0-9_.-]*(?:[><=!~]+[^\"\\x27,\\]\\)]+)?)[\"\\x27]', content)\n"
                'known = {"accelerate","datasets","tokenizers","numpy","packaging","filelock","requests",'
                '"tqdm","regex","sacremoses","pyyaml","PyYAML","importlib_metadata","importlib-metadata",'
                '"sentencepiece","safetensors","boto3","protobuf","scipy","scikit-learn","Pillow",'
                '"huggingface-hub","huggingface_hub"}\n'
                'norm = lambda d: d.split(">")[0].split("<")[0].split("=")[0].split("!")[0].split("~")[0].strip().lower().replace("-","_")\n'
                'known_n = {k.lower().replace("-","_") for k in known}\n'
                'seen = set()\n'
                'for d in deps:\n'
                '    n = norm(d)\n'
                '    if n in known_n and n not in seen:\n'
                '        seen.add(n)\n'
                '        print(f"Installing: {d}")\n'
                '        subprocess.run(["pip", "install", "--no-cache-dir", d], check=False)\n',
            ),
            File(
                ".",
                "hub_compat.py",
                'import huggingface_hub\n'
                'import os\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfFolder"):\n'
                '    class _HfFolder:\n'
                '        @staticmethod\n'
                '        def get_token():\n'
                '            return os.environ.get("HF_TOKEN", None)\n'
                '        @staticmethod\n'
                '        def save_token(token):\n'
                '            pass\n'
                '    huggingface_hub.HfFolder = _HfFolder\n'
                '\n'
                'if not hasattr(huggingface_hub, "Repository"):\n'
                '    huggingface_hub.Repository = type("Repository", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if not hasattr(huggingface_hub, "set_access_token"):\n'
                '    huggingface_hub.set_access_token = lambda *a, **kw: None\n'
                '\n'
                'if not hasattr(huggingface_hub, "delete_repo"):\n'
                '    huggingface_hub.delete_repo = lambda *a, **kw: None\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfFileSystem"):\n'
                '    huggingface_hub.HfFileSystem = type("HfFileSystem", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfApi"):\n'
                '    huggingface_hub.HfApi = type("HfApi", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if hasattr(huggingface_hub, "constants"):\n'
                '    if not hasattr(huggingface_hub.constants, "HF_HUB_CACHE"):\n'
                '        huggingface_hub.constants.HF_HUB_CACHE = os.path.expanduser("~/.cache/huggingface/hub")\n'
                '\n'
                'try:\n'
                '    from huggingface_hub import utils as _hub_utils\n'
                '    if not hasattr(_hub_utils, "OfflineModeIsEnabled"):\n'
                '        class _OfflineModeIsEnabled(ConnectionError):\n'
                '            pass\n'
                '        _hub_utils.OfflineModeIsEnabled = _OfflineModeIsEnabled\n'
                'except Exception:\n'
                '    pass\n'
                '\n'
                'try:\n'
                '    import pydantic\n'
                '    if not hasattr(pydantic, "TypeAdapter"):\n'
                '        pydantic.TypeAdapter = type("TypeAdapter", (), {"__init__": lambda self, *a, **kw: None, "validate_python": lambda self, *a, **kw: None})\n'
                'except Exception:\n'
                '    pass\n'
                '\n'
                'try:\n'
                '    import builtins\n'
                '    if not hasattr(builtins, "TypeAdapter"):\n'
                '        try:\n'
                '            from pydantic import TypeAdapter\n'
                '            builtins.TypeAdapter = TypeAdapter\n'
                '        except Exception:\n'
                '            builtins.TypeAdapter = type("TypeAdapter", (), {"__init__": lambda self, *a, **kw: None})\n'
                'except Exception:\n'
                '    pass\n',
            ),        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_VERBOSE_LINE = re.compile(
    r"^(\S.*?\.py::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)"
)

_PASSED_OUTCOMES = {"PASSED", "XPASS"}
_FAILED_OUTCOMES = {"FAILED", "ERROR"}
_SKIPPED_OUTCOMES = {"SKIPPED", "XFAIL"}


def parse_pytest_verbose_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        match = _VERBOSE_LINE.match(line.rstrip())
        if not match:
            continue

        name, outcome = match.group(1), match.group(2)
        if outcome in _PASSED_OUTCOMES:
            passed_tests.add(name)
        elif outcome in _FAILED_OUTCOMES:
            failed_tests.add(name)
        elif outcome in _SKIPPED_OUTCOMES:
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


@Instance.register("huggingface", "transformers_44040_to_3323")
class HuggingFaceTransformers(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_verbose_log(log)
