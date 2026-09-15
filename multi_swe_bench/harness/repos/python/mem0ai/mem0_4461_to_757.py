import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "mem0ai"
_REPO = "mem0"
_BASE_TAG = "base-4461_to_757"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUS_AFTER = re.compile(
    r"^(?P<name>\S.*?::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b.*$"
)
_STATUS_BEFORE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>\S+::\S+?)(?:\s+-\s.*)?$"
)
_PASS = {"PASSED", "XPASS"}
_FAIL = {"FAILED", "ERROR"}
_SKIP = {"SKIPPED", "XFAIL"}

_TEST_COMMAND = (
    "pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider "
    "--continue-on-collection-errors -o addopts= --forked"
)

_APT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "make",
    "wget",
    "pkg-config",
    "libgeos-dev",
    "libmagic1",
]

_UV_VERSION = "0.12.13"

_BASE_ENV = """ENV POETRY_VIRTUALENVS_CREATE=false
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONDONTWRITEBYTECODE=1"""

_EXTRA_PACKAGES = [
    "unstructured",
    "lxml",
    "openpyxl",
    "pymilvus",
    "weaviate-client",
    "qdrant-client",
    "pinecone",
    "pinecone-text",
    "langchain-community",
    "google-api-python-client",
    "google-auth-oauthlib",
    "google-auth-httplib2",
    "google-generativeai",
    "azure-identity",
    "deepgram-sdk",
    "validators",
    "ollama",
    "turbopuffer",
    "groq",
    "litellm",
    "together",
    "replicate",
    "gpt4all",
    "youtube-transcript-api",
    "beautifulsoup4",
    "pypdf",
    "pytube",
    "duckduckgo-search",
    "docx2txt",
    "pillow",
    "ftfy",
    "regex",
    "huggingface_hub",
    "mock",
]

_TEST_PACKAGES = ["pytest", "pytest-mock", "pytest-asyncio", "pytest-env", "pytest-forked"]

_RUN_ENV = """export CI=true
export OPENAI_API_KEY=sk-dummy0000000000000000000000000000000000000000
export ANTHROPIC_API_KEY=sk-ant-dummy0000000000000000000000000000000000
export GOOGLE_API_KEY=dummy
export GEMINI_API_KEY=dummy
export COHERE_API_KEY=dummy
export GROQ_API_KEY=dummy
export TOGETHER_API_KEY=dummy
export HUGGINGFACE_API_KEY=dummy
export HUGGINGFACEHUB_API_TOKEN=dummy
export MISTRAL_API_KEY=dummy
export DEEPSEEK_API_KEY=dummy
export XAI_API_KEY=dummy
export OPENROUTER_API_KEY=dummy
export AZURE_OPENAI_API_KEY=dummy
export AWS_ACCESS_KEY_ID=dummy
export AWS_SECRET_ACCESS_KEY=dummy
export AWS_DEFAULT_REGION=us-east-1
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export HTTP_PROXY=http://127.0.0.1:9
export HTTPS_PROXY=http://127.0.0.1:9
export no_proxy=
export NO_PROXY="""

_DERIVE = r"""python - <<'DERIVE'
import pathlib
import re
import tomllib

TEST_PACKAGES = __TEST_PACKAGES__

data = tomllib.load(open("pyproject.toml", "rb"))
poetry = data.get("tool", {}).get("poetry", {})


def to_pip(name, spec):
    if isinstance(spec, dict):
        spec = spec.get("version", "*")
    if not isinstance(spec, str) or spec in ("*", ""):
        return name
    if spec.startswith("^"):
        parts = [int(x) for x in re.findall(r"\d+", spec[1:])[:3]]
        while len(parts) < 3:
            parts.append(0)
        major, minor, patch = parts
        if major:
            upper = f"{major + 1}.0.0"
        elif minor:
            upper = f"0.{minor + 1}.0"
        else:
            upper = f"0.0.{patch + 1}"
        return f"{name}>={spec[1:]},<{upper}"
    if spec.startswith((">", "<", "=", "!", "~")):
        return f"{name}{spec}"
    return f"{name}=={spec}"


required = [
    to_pip(name, spec)
    for name, spec in poetry.get("dependencies", {}).items()
    if name != "python" and not (isinstance(spec, dict) and spec.get("optional"))
]
pathlib.Path("/tmp/legacy-reqs.txt").write_text("\n".join(required) + "\n")

declared = {}
poetry_groups = [poetry.get("dev-dependencies", {})] + [group.get("dependencies", {}) for group in poetry.get("group", {}).values()]
for group in poetry_groups:
    for name, spec in group.items():
        declared.setdefault(name.lower(), to_pip(name, spec))
pep_groups = list(data.get("project", {}).get("optional-dependencies", {}).values()) + list(data.get("dependency-groups", {}).values())
for group in pep_groups:
    for requirement in group:
        if isinstance(requirement, str):
            declared.setdefault(re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower(), requirement)
pathlib.Path("/tmp/test-reqs.txt").write_text("\n".join(declared.get(name, name) for name in TEST_PACKAGES) + "\n")
DERIVE""".replace("__TEST_PACKAGES__", repr(_TEST_PACKAGES))

_INSTALL = r"""if grep -q "setuptools.build_meta" pyproject.toml && ! grep -q "^\[project\]" pyproject.toml; then
uv pip install --system -r /tmp/legacy-reqs.txt
printf '[options]\npackages = find:\n\n[options.packages.find]\ninclude = embedchain*\n' > setup.cfg
uv pip install --system --no-deps -e .
rm -f setup.cfg
else
uv pip install --system -e ".[test,dev]" || uv pip install --system -e .
fi"""

_GATE = r"""python -P - <<'GATE'
import importlib
import sys

import pytest
import pytest_asyncio
import pytest_forked
import pytest_mock

loaded = []
for name in ("mem0", "embedchain"):
    try:
        importlib.import_module(name)
        loaded.append(name)
    except Exception:
        pass
if not loaded:
    sys.exit("neither mem0 nor embedchain is importable")
print("pytest", pytest.__version__, "package", ",".join(loaded))
GATE
http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 no_proxy= NO_PROXY= python - <<'COLLECT'
import sys

import pytest


class Counter:
    total = 0

    def pytest_collection_modifyitems(self, items):
        Counter.total += len(items)


pytest.main(
    ["tests/", "--collect-only", "-qq", "-p", "no:cacheprovider", "--continue-on-collection-errors", "-o", "addopts="],
    plugins=[Counter()],
)
print("collected", Counter.total)
sys.exit(0 if Counter.total > 0 else "no tests collected")
COLLECT"""


def parse_log(log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    for raw_line in log.splitlines():
        line = _ANSI.sub("", raw_line).strip()
        if not line:
            continue
        match = _STATUS_AFTER.match(line) or _STATUS_BEFORE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        status = match.group("status")
        if status in _PASS:
            passed.add(name)
        elif status in _FAIL:
            failed.add(name)
        elif status in _SKIP:
            skipped.add(name)
    passed -= failed
    skipped -= failed
    passed -= skipped
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


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
        return "python:3.11-slim-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages = " \\\n    ".join(_APT_PACKAGES)
        apt_command = self._get_apt_update_command(packages, base_img)
        infra = DockerfileEnhancer._infrastructure_block(self, base_img, True)
        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

{infra}
{_BASE_ENV}

WORKDIR /home/

{apt_command}

RUN pip install --no-cache-dir uv=={_UV_VERSION}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{_REPO} && \\
    cd /home/{_REPO} && git rev-parse HEAD >/dev/null

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        extras = " ".join(f'"{name}"' for name in _EXTRA_PACKAGES)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: not inside a git work tree" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: work tree is dirty" >&2
  git status --porcelain >&2
  exit 1
fi

echo "check_git_changes: clean"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
export CI=true

cd /home/{_REPO}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

CUTOFF="$(date -u -d "$(git log -1 --format=%cI HEAD) + 30 days" +%Y-%m-%dT%H:%M:%SZ)"
export UV_EXCLUDE_NEWER="$CUTOFF"

{_DERIVE}
{_INSTALL}
for spec in {extras}; do uv pip install --system "$spec" || true; done
uv pip uninstall --system pinecone-plugin-inference || true
python -P -c "import nltk; [nltk.download(p, quiet=True) for p in ('punkt', 'punkt_tab', 'averaged_perceptron_tagger', 'averaged_perceptron_tagger_eng')]" || true
python -P -m spacy download en_core_web_sm || true
uv pip install --system -r /tmp/test-reqs.txt

{_GATE}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{_REPO}
{_RUN_ENV}
{_TEST_COMMAND}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{_REPO}
git apply --whitespace=nowarn /home/test.patch
{_RUN_ENV}
{_TEST_COMMAND}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{_REPO}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_RUN_ENV}
{_TEST_COMMAND}
""",
            ),
        ]

    def dockerfile(self) -> str:
        sha = self.pr.base.sha
        return f"""FROM {self.dependency().image_full_name()}

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{_REPO}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{_REPO}/.gitmodules ]; then \\
        cd /home/{_REPO} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register(_ORG, _REPO)
class MEM0(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_log(log)


if not getattr(Instance, "_mem0_plain_key_hook", False):
    _previous_create = Instance.create.__func__

    def _mem0_plain_key_create(cls, pr, config, *args, **kwargs):
        interval = getattr(pr, "number_interval", "") or ""
        if (
            not interval
            and getattr(pr, "org", "") == _ORG
            and getattr(pr, "repo", "") == _REPO
        ):
            return cls._registry[f"{_ORG}/{_REPO}"](pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_mem0_plain_key_create)
    Instance._mem0_plain_key_hook = True
