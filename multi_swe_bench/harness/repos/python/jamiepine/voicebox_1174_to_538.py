from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "voicebox_1174_to_538"
_BASE_IMAGE = "python:3.12-slim-bookworm"
_BASE_TAG = "base-1174_to_538"
_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_TORCH_VERSION = "2.11.0"
_PIPER_VERSION = "1.3.0"
_PIP_CONSTRAINTS = "pedalboard<0.9.21"
_PIP_VERSION = "26.1.2"
_PIPER_FIND_LINKS = "https://k2-fsa.github.io/icefall/piper_phonemize.html"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$", re.M)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STATUS = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_VERBOSE_RE = re.compile(
    rf"^(?P<name>\S+\.py::.+?)\s+(?P<status>{_STATUS})(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_SUMMARY_RE = re.compile(
    rf"^(?P<status>{_STATUS})\s+(?P<name>\S+\.py(?:::.+?)?)(?:\s+-\s.*)?$"
)


def _binary_stub_paths(*patches: str) -> list[str]:
    paths: list[str] = []
    for patch in patches:
        text = patch or ""
        headers = list(_DIFF_HEADER_RE.finditer(text))
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            body = text[header.end():end]
            if _BINARY_STUB_RE.search(body) and "GIT binary patch" not in body:
                for path in (header.group(1), header.group(2)):
                    if path not in paths:
                        paths.append(path)
    return paths


def _apply_command(*patches: tuple[str, str]) -> str:
    excludes = _binary_stub_paths(*(content for _, content in patches))
    parts = ["git apply --whitespace=nowarn"]
    parts.extend(f"--exclude={shlex.quote(path)}" for path in excludes)
    parts.extend(f"/home/{name}" for name, _ in patches)
    return " ".join(parts)


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

export CI=true
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1

cutoff="$(git show -s --format=%cI HEAD)"
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" --upgrade setuptools wheel
python - torch torchaudio > /tmp/msb-torch-deps.txt <<'PYEOF'
import re
import sys
from importlib.metadata import requires

names = set(sys.argv[1:])
for dist in sys.argv[1:]:
    for requirement in requires(dist) or []:
        name = re.match(r"[A-Za-z0-9._-]+", requirement).group(0).lower()
        if "extra ==" in requirement or name in names:
            continue
        print(requirement)
PYEOF
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -r /tmp/msb-torch-deps.txt

python - "$cutoff" backend/requirements.txt > /tmp/msb-requirements.txt <<'PYEOF'
import re
import subprocess
import sys

cutoff, path = sys.argv[1], sys.argv[2]
pattern = re.compile(r"^(?P<name>[A-Za-z0-9_.\\-\\[\\],]+)\\s*@\\s*git\\+(?P<url>https://[^\\s@;]+?)(?P<rest>\\s*(;.*)?)$")
for index, line in enumerate(open(path).read().splitlines()):
    stripped = line.strip()
    if stripped.startswith("--find-links"):
        continue
    match = pattern.match(stripped)
    if match:
        target = f"/tmp/msb-pin-{index}"
        subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", match["url"], target], check=True)
        sha = subprocess.run(["git", "-C", target, "rev-list", "-1", "--first-parent", f"--before={cutoff}", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        if not sha:
            sys.exit(f"no commit of {match['url']} before {cutoff}")
        print(f"{match['name']} @ git+{match['url']}@{sha}{match['rest']}")
        print(f"pinned {match['name']} to {sha}", file=sys.stderr)
        continue
    print(line)
PYEOF
printf '%s\\n' "__PIP_CONSTRAINTS__" > /tmp/msb-constraints.txt

python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -c /tmp/msb-constraints.txt -r /tmp/msb-requirements.txt
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -c /tmp/msb-constraints.txt --no-deps chatterbox-tts hume-tada
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -c /tmp/msb-constraints.txt pytest pytest-asyncio openai pyinstaller

python -c "import pytest, pytest_asyncio, fastapi, httpx, numpy, torch, torchaudio, transformers, sqlalchemy, soundfile, librosa, PyInstaller, piper_phonemize"
python -m pytest backend/tests --rootdir=. --collect-only -q -p no:cacheprovider --continue-on-collection-errors > /tmp/msb-collect.log 2>&1 || true
grep -Eq "^backend/tests/\\S+\\.py::" /tmp/msb-collect.log || { tail -50 /tmp/msb-collect.log; exit 1; }

echo "DEPS_OK"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1

cd /home/__REPO__
"""

_TEST_CMD = """python -m pytest backend/tests --rootdir=. -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors --deselect backend/tests/test_offline_guard.py::test_nesting_opposite_mode_on_same_thread_raises_instead_of_deadlocking
"""


class Voicebox1174To538ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    espeak-ng \\
    ffmpeg \\
    git \\
    libsndfile1 \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --retries 5 --timeout 120 --no-cache-dir "pip=={_PIP_VERSION}" \\
    && python -m pip install --retries 5 --timeout 120 --no-cache-dir --no-deps --index-url {_TORCH_INDEX} "torch=={_TORCH_VERSION}" "torchaudio=={_TORCH_VERSION}" \\
    && python -m pip install --retries 5 --timeout 120 --no-cache-dir --find-links {_PIPER_FIND_LINKS} "piper_phonemize=={_PIPER_VERSION}"

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Voicebox1174To538ImageDefault(Image):
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
        return Voicebox1174To538ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__PIP_CONSTRAINTS__", _PIP_CONSTRAINTS)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        test_apply = _apply_command(("test.patch", self.pr.test_patch))
        fix_apply = _apply_command(
            ("test.patch", self.pr.test_patch), ("fix.patch", self.pr.fix_patch)
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{_TEST_CMD}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{_TEST_CMD}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{_TEST_CMD}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        match = _VERBOSE_RE.match(line)
        if not match:
            match = _SUMMARY_RE.match(line)
            if match and match.group("name").count("[") > match.group("name").count("]"):
                continue
        if not match:
            continue
        name = match.group("name")
        if "::" not in name:
            name = f"{name}::<collection error>"
        status = match.group("status")
        if status in ("PASSED", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
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


@Instance.register("jamiepine", _INTERVAL)
class VOICEBOX_1174_TO_538(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Voicebox1174To538ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)
