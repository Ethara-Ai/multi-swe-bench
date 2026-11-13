from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "unsloth_14893_to_11937"
_BASE_IMAGE = "python:3.12-slim-bookworm"
_BASE_TAG = "base-14893_to_11937"
_NODE_VERSION = "22.20.0"
_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
_TORCH_VERSION = "2.10.0"
_TORCHVISION_VERSION = "0.25.0"
_PIP_VERSION = "26.1.2"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$", re.M)

_SUITES = (
    (
        "studio/backend",
        re.compile(r"^studio/backend/tests/(?:.*/)?test_[^/]*\.py$"),
        "python -m pytest -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors --timeout=330",
    ),
    (
        ".",
        re.compile(r"^tests/(?:.*/)?test_[^/]*\.py$"),
        'PYTHONPATH="$PWD/studio" python -m pytest -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors --timeout=330',
    ),
    (
        "studio/frontend",
        re.compile(r"^studio/frontend/tests/(?:.*/)?[^/]*\.test\.ts$"),
        "node --experimental-strip-types --test --test-reporter=/home/msb-node-reporter.mjs --test-reporter-destination=stdout",
    ),
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STATUS = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_ROOT_RE = re.compile(r"^MSB_ROOT (?P<root>\S+)$")
_NODE_RE = re.compile(r"^MSB_NODE (?P<status>PASSED|FAILED|SKIPPED) (?P<name>.+)$")
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


def _suite_files(pr: PullRequest, root: str, pattern: re.Pattern[str]) -> list[str]:
    files: list[str] = []
    for header in _DIFF_HEADER_RE.finditer(pr.test_patch or ""):
        path = header.group(2)
        if pattern.match(path):
            relative = path if root == "." else path[len(root) + 1:]
            if relative not in files:
                files.append(relative)
    return sorted(files)


def _test_body(pr: PullRequest) -> str:
    blocks = ["rc=0"]
    for root, pattern, runner in _SUITES:
        files = " ".join(shlex.quote(path) for path in _suite_files(pr, root, pattern))
        blocks.append(
            f"""echo "MSB_ROOT {root}"
(
    cd {root} || exit 1
    targets=()
    for path in {files}; do
        if [ -f "$path" ]; then
            targets+=("$path")
        fi
    done
    if [ "${{#targets[@]}}" -gt 0 ]; then
        {runner} "${{targets[@]}}"
    fi
) || rc=$?"""
        )
    blocks.append('exit "$rc"')
    return "\n\n".join(blocks) + "\n"


_NODE_REPORTER_MJS = r"""import { isAbsolute, relative } from 'node:path';

const seen = new Map();

function fileLabel(file) {
    if (!file) return '<unknown file>';
    const path = isAbsolute(file) ? relative(process.cwd(), file) : file;
    return path.split('\\').join('/');
}

function unique(line) {
    const count = (seen.get(line) || 0) + 1;
    seen.set(line, count);
    return count === 1 ? line : line + ' #' + count;
}

function flagged(value) {
    return value !== undefined && value !== false;
}

export default async function* msbReporter(source) {
    const open = new Map();
    for await (const event of source) {
        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        const data = event.data;
        const file = fileLabel(data.file);
        const nesting = data.nesting || 0;
        let name = String(data.name).replace(/\s+/g, ' ').trim();
        if (data.file && data.name === data.file) name = '<file>';

        let levels = open.get(file);
        if (!levels) {
            levels = [];
            open.set(file, levels);
        }

        const own = flagged(data.skip) || flagged(data.todo)
            ? 'SKIPPED'
            : event.type === 'test:fail' ? 'FAILED' : 'PASSED';
        const children = levels[nesting + 1] || [];
        levels.length = nesting + 1;

        let leaves;
        if (children.length === 0) {
            leaves = [{ status: own, path: [name] }];
        } else {
            leaves = children.map((child) => ({ status: child.status, path: [name, ...child.path] }));
            if (own === 'FAILED' && !children.some((child) => child.status === 'FAILED')) {
                leaves.push({ status: 'FAILED', path: [name, '<hook or suite failure>'] });
            }
        }

        if (nesting === 0) {
            for (const leaf of leaves) {
                yield unique('MSB_NODE ' + leaf.status + ' ' + file + '::' + leaf.path.join(' > ')) + '\n';
            }
        } else {
            if (!levels[nesting]) levels[nesting] = [];
            levels[nesting].push(...leaves);
        }
    }
}
"""

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
python - torch torchvision torchaudio > /tmp/msb-torch-deps.txt <<'PYEOF'
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
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -r studio/backend/requirements/studio.txt
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" -e ".[huggingfacenotorch]"
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" python-multipart aiofiles sqlalchemy cryptography psutil pyyaml jinja2 mammoth unpdf requests "numpy<3" pytest pytest-asyncio pytest-xdist pytest-timeout httpx soundfile librosa torchao
python -m pip install --retries 5 --timeout 120 --uploaded-prior-to "$cutoff" "$(grep -m1 -E '^peft==' studio/backend/requirements/extras-no-deps.txt)"

cd /home/__REPO__/studio/frontend
npm ci --no-fund --no-audit
test -d node_modules

cd /home/__REPO__
UNSLOTH_IS_PRESENT=1 UNSLOTH_ALLOW_CPU=1 python -c "import torch, torchaudio, transformers, unsloth_zoo, fastapi, pytest, pytest_asyncio, pytest_timeout"
python -m pytest --version
node --version

echo "DEPS_OK"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export UNSLOTH_COMPILE_DISABLE=1
export UNSLOTH_IS_PRESENT=1
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

cd /home/__REPO__
"""


class Unsloth14893To11937ImageBase(Image):
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
    git \\
    xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "${{TARGETARCH:-amd64}}" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported TARGETARCH ${{TARGETARCH}}" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{node_arch}}.tar.xz" -o /tmp/node.tar.xz; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \\
    rm -f /tmp/node.tar.xz; \\
    node --version; \\
    npm --version

RUN python -m pip install --retries 5 --timeout 120 --no-cache-dir "pip=={_PIP_VERSION}" \\
    && python -m pip install --retries 5 --timeout 120 --no-cache-dir --no-deps --index-url {_TORCH_INDEX} "torch=={_TORCH_VERSION}" "torchvision=={_TORCHVISION_VERSION}" "torchaudio=={_TORCH_VERSION}"

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Unsloth14893To11937ImageDefault(Image):
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
        return Unsloth14893To11937ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = _test_body(self.pr)
        test_apply = _apply_command(("test.patch", self.pr.test_patch))
        fix_apply = _apply_command(
            ("test.patch", self.pr.test_patch), ("fix.patch", self.pr.fix_patch)
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "msb-node-reporter.mjs", _NODE_REPORTER_MJS),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{body}"),
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


def _qualified(root: str, name: str) -> str:
    return name if root == "." else f"{root}/{name}"


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    root = "."

    for raw_line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        marker = _ROOT_RE.match(line)
        if marker:
            root = marker.group("root")
            continue
        match = _NODE_RE.match(line) or _VERBOSE_RE.match(line)
        if not match:
            match = _SUMMARY_RE.match(line)
            if match and match.group("name").count("[") > match.group("name").count("]"):
                continue
        if not match:
            continue
        name = match.group("name")
        if "::" not in name:
            name = f"{name}::<collection error>"
        name = _qualified(root, name)
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


@Instance.register("unslothai", _INTERVAL)
class UNSLOTH_14893_TO_11937(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Unsloth14893To11937ImageDefault(self.pr, self._config)

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
