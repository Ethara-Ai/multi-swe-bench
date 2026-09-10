import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "jest_14139_to_13472"
_BASE_TAG = "base-13472_to_14139"
_LO, _HI = 13472, 14139

_GIT_APPLY_EXCLUDES = (
    "--exclude=*.wasm --exclude=*.png --exclude=*.jpg --exclude=*.jpeg "
    "--exclude=*.gif --exclude=*.ico --exclude=*.bmp --exclude=*.webp "
    "--exclude=*.woff --exclude=*.woff2 --exclude=*.ttf --exclude=*.eot "
    "--exclude=*.otf --exclude=*.pdf --exclude=*.mp4 --exclude=*.webm"
)


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    YARN_ENABLE_GLOBAL_CACHE=1 \
    FORCE_COLOR=1 \
    NODE_OPTIONS=--max-old-space-size=4096 \
    CI=true

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout __BASE_SHA__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
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


_RESCUE_BINARIES = r"""
mkdir -p /home/binassets
python3 - <<'PYEOF'
import os
import re
import subprocess

OUT = "/home/binassets"
recovered = []

for patch in ("/home/test.patch", "/home/fix.patch"):
    if not os.path.exists(patch):
        continue
    with open(patch, encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    for block in re.split(r"(?=^diff --git )", body, flags=re.M):
        if "Binary files" not in block and "GIT binary patch" not in block:
            continue
        header = re.match(r"diff --git a/(.*?) b/(.*?)\n", block)
        index = re.search(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", block, re.M)
        if not header or not index:
            continue
        target, blob = header.group(2), index.group(2)
        if set(blob) == {"0"}:
            continue
        destination = os.path.join(OUT, target)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            with open(destination, "wb") as handle:
                subprocess.check_call(["git", "cat-file", "blob", blob], stdout=handle)
        except subprocess.CalledProcessError:
            if os.path.exists(destination):
                os.remove(destination)
            print("=== could not rescue %s (blob %s) ===" % (target, blob))
            continue
        recovered.append(target)
        print("=== rescued %s from blob %s ===" % (target, blob))

with open(os.path.join(OUT, "MANIFEST"), "w") as handle:
    for entry in recovered:
        handle.write(entry + "\n")

if not recovered:
    print("=== no binary hunks in this PR's patches ===")
PYEOF
"""


_RESTORE_BINARIES = r"""
if [ -s /home/binassets/MANIFEST ]; then
  while IFS= read -r asset || [ -n "$asset" ]; do
    [ -n "$asset" ] || continue
    if [ -f "/home/binassets/$asset" ]; then
      mkdir -p "$(dirname "$asset")"
      cp "/home/binassets/$asset" "$asset"
      echo "=== restored binary fixture: $asset ==="
    fi
  done < /home/binassets/MANIFEST
fi
"""


_SELECT_TESTS = r"""
CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n 's|^diff --git a/\(.*\) b/.*$|\1|p' | sort -u)

printf '%s\n' "$CHANGED" \
  | grep -E '^(e2e/__tests__/[^/]+\.test\.[cm]?[jt]sx?|packages/[^/]+/src/.*__tests__/[^/]+\.test\.[cm]?[jt]sx?)$' \
  | grep -v '__typetests__' \
  | grep -v '__fixtures__' \
  | sort -u > /home/test_files.txt

echo "=== graded test files ==="
cat /home/test_files.txt
test -s /home/test_files.txt
"""


_PREPARE_SH = (
    r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git checkout --detach "__BASE_SHA__"
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh
"""
    + _RESCUE_BINARIES
    + r"""
corepack enable
node --version
yarn --version

mkdir -p /home/base_manifests
python3 - <<'PYEOF'
import glob
import os
import shutil

for path in ["package.json"] + sorted(glob.glob("packages/*/package.json")):
    destination = os.path.join("/home/base_manifests", path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copyfile(path, destination)
PYEOF

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch

python3 - <<'PYEOF'
import glob
import json
import os

for path in ["package.json"] + sorted(glob.glob("packages/*/package.json")):
    baseline = os.path.join("/home/base_manifests", path)
    if not os.path.exists(baseline):
        continue
    with open(baseline) as handle:
        before = json.load(handle)
    with open(path) as handle:
        after = json.load(handle)
    restored = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (before.get(section) or {}).items():
            if name not in (after.get(section) or {}):
                after.setdefault(section, {})[name] = spec
                restored.append("%s (%s)" % (name, section))
    if restored:
        with open(path, "w") as handle:
            json.dump(after, handle, indent=2)
            handle.write("\n")
        print("=== %s: restored %s ===" % (path, ", ".join(restored)))
PYEOF

installed=0
for attempt in 1 2 3; do
    if yarn install --no-immutable --network-timeout 600000; then
        installed=1
        break
    fi
    echo "prepare: yarn install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1

git checkout -- .
git clean -fd
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

yarn build:js

test -f ./packages/jest-cli/bin/jest.js
node ./packages/jest-cli/bin/jest.js --version > /dev/null
echo "DEPS_OK"
"""
    + _SELECT_TESTS
)


_EXEC_TESTS = r"""
echo "=== graded test files ==="
cat /home/test_files.txt

STATUS=0
node ./packages/jest-cli/bin/jest.js \
    --ci \
    --verbose \
    --runInBand \
    --runTestsByPath $(tr '\n' ' ' < /home/test_files.txt) || STATUS=$?

echo "=== Test run complete ==="
exit $STATUS
"""


_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/__REPO__
"""


_REBUILD = r"""
if git diff --name-only HEAD | grep -qE '^packages/'; then
    echo "=== rebuilding packages (packages/ touched) ==="
    yarn build:js
else
    echo "=== no packages/ changes, skipping rebuild ==="
fi
"""


_APPLY_TEST_PATCH = r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch
"""


_APPLY_BOTH_PATCHES = r"""
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch
"""


_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = (
    _SCRIPT_HEADER + _APPLY_TEST_PATCH + _RESTORE_BINARIES + _REBUILD + _EXEC_TESTS
)
_FIX_RUN_SH = (
    _SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _RESTORE_BINARIES + _REBUILD + _EXEC_TESTS
)


class JestBundleImageBase(Image):

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
        return "node:18-bookworm"

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

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class JestBundleImageDefault(Image):

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
        return JestBundleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _GIT_APPLY_EXCLUDES)
        )

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

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DURATION_RE = re.compile(r"\s*\((?:\d+(?:[.,]\d+)?\s*(?:ms|s|m)\s*)+\)\s*$")

_PASS_SYMBOLS = "✓✔√"
_FAIL_SYMBOLS = "✕✗×✘"
_SKIP_SYMBOLS = "○◯✎↓"

_SUITE_RE = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)")
_SUITE_ERROR_RE = re.compile(r"^\u25cf\s+Test suite failed to run")
_TEST_RE = re.compile(
    r"^(\s+)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + r"])\s+(\S.*)$"
)
_BLOCK_TERMINATORS = (
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "console.debug",
    "at ",
)
_SKIP_PREFIXES = ("skipped ", "todo ")


def _clean_title(title: str) -> str:
    return _DURATION_RE.sub("", title.strip()).strip()


def jest_bundle_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            passed_tests.add(name)
        elif symbol in _FAIL_SYMBOLS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: Optional[str] = None
    describe_stack: list[str] = []
    in_block = False
    suite_failed_to_run: set[str] = set()

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()

        if current_file and _SUITE_ERROR_RE.match(stripped):
            suite_failed_to_run.add(current_file)

        suite = _SUITE_RE.match(stripped)
        if suite:
            current_file = suite.group(1)
            describe_stack = []
            in_block = True
            continue

        if not stripped or not in_block:
            continue

        if stripped.startswith("●") or stripped.startswith(_BLOCK_TERMINATORS):
            in_block = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_block = False
            continue

        match = _TEST_RE.match(line)
        if match:
            symbol = match.group(2)
            title = _clean_title(match.group(3))
            if symbol in _SKIP_SYMBOLS:
                for prefix in _SKIP_PREFIXES:
                    if title.startswith(prefix):
                        title = title[len(prefix) :].strip()
                        break
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(symbol, " › ".join(part for part in parts if part))
        else:
            level = max(indent // 2, 1)
            describe_stack = describe_stack[: level - 1]
            describe_stack.append(stripped)

    for path in suite_failed_to_run:
        if not any(
            name.startswith(f"{path} \u203a ")
            for name in passed_tests | failed_tests | skipped_tests
        ):
            failed_tests.add(f"{path} \u203a Test suite failed to run")

    passed_tests -= failed_tests
    skipped_tests -= passed_tests | failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("jestjs", _INTERVAL_NAME)
class JEST_14139_TO_13472(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JestBundleImageDefault(self.pr, self._config)

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
        return jest_bundle_parse_log(test_log)


_INCUMBENT = Instance._registry.get("jestjs/jest")


@Instance.register("jestjs", "jest")
class JestDispatch(Instance):

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return JEST_14139_TO_13472(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"jestjs/jest#{pr.number} is outside {_LO}-{_HI} and no other jest "
            f"adapter is registered under the bare name"
        )


for _number in range(_LO, _HI + 1):
    Instance._registry.setdefault(f"jestjs/{_number}", JEST_14139_TO_13472)
del _number
