import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_PYTHON_IMAGE = "python:3.11-slim"
BASE_IMAGE_TAG = "base-uv-workspace-py311"
WORKSPACE_DIR = "python"
DEFAULT_PACKAGE_DIR = "python/packages/autogen-agentchat"
TESTS_DIR_MARKER = "/tests/"
CONFTEST_BASENAME = "conftest.py"
PYPROJECT_BASENAME = "pyproject.toml"
SYNC_PACKAGES = ("autogen-ext", "autogen-agentchat")
SYNC_EXTRAS = ("openai", "azure", "mcp")
VENV_PYTHON_VERSION = "3.11"
EMULATED_MACHINE = "x86_64"
LOCK_INSTALLER_NAME = "install_from_uv_lock.py"
GATE_MODULES = (
    "autogen_core",
    "autogen_agentchat",
    "autogen_ext.models.openai",
    "autogen_ext.models.replay",
    "autogen_ext.tools.mcp",
    "pytest",
    "pytest_asyncio",
)

TEST_TARGET_PATTERN = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.M)
ADDED_REQUIREMENT_PATTERN = re.compile(
    r'^\+\s*"([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?(?:\s*[<>=!~][^"]*)?)",?\s*$'
)


def collect_test_paths(test_patch: str) -> list[str]:
    paths = set()
    for match in TEST_TARGET_PATTERN.finditer(test_patch):
        path = match.group(1)
        if not path.endswith(".py"):
            continue
        if TESTS_DIR_MARKER not in path:
            continue
        if path.rsplit("/", 1)[-1] == CONFTEST_BASENAME:
            continue
        paths.add(path)
    return sorted(paths)


def resolve_package_dir(test_paths: list[str]) -> str:
    packages = sorted({path.split(TESTS_DIR_MARKER, 1)[0] for path in test_paths})
    if not packages:
        return DEFAULT_PACKAGE_DIR
    return packages[0]


def relative_test_paths(test_paths: list[str], package_dir: str) -> list[str]:
    prefix = package_dir + "/"
    return [path[len(prefix) :] for path in test_paths if path.startswith(prefix)]


def collect_added_requirements(fix_patch: str) -> list[str]:
    requirements: set[str] = set()
    inside_manifest = False
    for line in fix_patch.splitlines():
        if line.startswith("+++ b/"):
            inside_manifest = line.endswith(PYPROJECT_BASENAME)
            continue
        if line.startswith("diff --git "):
            inside_manifest = False
            continue
        if not inside_manifest or not line.startswith("+"):
            continue
        match = ADDED_REQUIREMENT_PATTERN.match(line)
        if match:
            requirements.add(match.group(1))
    return sorted(requirements)


def render_extra_requirements(fix_patch: str) -> str:
    requirements = collect_added_requirements(fix_patch)
    if not requirements:
        return "true"
    quoted = " ".join(f'"{requirement}"' for requirement in requirements)
    return f"install_requirements_at_locked_version {quoted}"


def render_sync_command() -> str:
    packages = " ".join(f"--package {name}" for name in SYNC_PACKAGES)
    extras = " ".join(f"--extra {name}" for name in SYNC_EXTRAS)
    return f"uv sync --frozen {packages} {extras}"


def render_uv_provision(fix_patch: str) -> str:
    steps = [
        f"uv venv --python {VENV_PYTHON_VERSION} .venv",
        render_sync_command(),
        render_extra_requirements(fix_patch),
    ]
    return "\n    ".join(steps)


def render_pip_provision(fix_patch: str) -> str:
    selection = (
        [f"--package {name}" for name in SYNC_PACKAGES]
        + [f"--extra {name}" for name in SYNC_EXTRAS]
        + [f'--with "{requirement}"' for requirement in collect_added_requirements(fix_patch)]
    )
    steps = [
        "python3 -m venv --without-pip .venv",
        f"python3 /home/{LOCK_INSTALLER_NAME} --python .venv/bin/python {' '.join(selection)}",
    ]
    return "\n    ".join(steps)


LOCK_INSTALLER_SCRIPT = r'''import argparse
import json
import os
import subprocess
import sys
import tomllib

from pip._vendor.packaging.markers import Marker


def normalize(name):
    return name.lower().replace("_", "-").replace(".", "-")


def marker_holds(expression, environment):
    if not expression:
        return True
    return Marker(expression).evaluate(environment)


def target_environment(python):
    probe = (
        "import json, platform, sys, os;"
        "print(json.dumps({"
        "'implementation_name': sys.implementation.name,"
        "'implementation_version': platform.python_version(),"
        "'os_name': os.name,"
        "'platform_machine': platform.machine(),"
        "'platform_python_implementation': platform.python_implementation(),"
        "'platform_release': platform.release(),"
        "'platform_system': platform.system(),"
        "'platform_version': platform.version(),"
        "'python_full_version': platform.python_version(),"
        "'python_version': '.'.join(platform.python_version_tuple()[:2]),"
        "'sys_platform': sys.platform,"
        "'extra': ''}))"
    )
    return json.loads(subprocess.check_output([python, "-c", probe], text=True))


class LockGraph:
    def __init__(self, lock_path, environment):
        with open(lock_path, "rb") as handle:
            data = tomllib.load(handle)
        self.environment = environment
        self.root_groups = data.get("manifest", {}).get("dependency-groups", {})
        self.by_name = {}
        for package in data["package"]:
            self.by_name.setdefault(normalize(package["name"]), []).append(package)

    def pick(self, reference):
        candidates = self.by_name.get(normalize(reference["name"]), [])
        if "version" in reference:
            candidates = [p for p in candidates if p["version"] == reference["version"]]
        if len(candidates) > 1:
            candidates = [
                p
                for p in candidates
                if any(
                    marker_holds(m, self.environment)
                    for m in p.get("resolution-markers", [""])
                )
            ]
        if len(candidates) != 1:
            raise SystemExit(
                f"install_from_uv_lock: cannot choose {reference} ({len(candidates)} candidates)"
            )
        return candidates[0]

    def lookup(self, name):
        if not self.by_name.get(normalize(name)):
            return None
        return self.pick({"name": name})


def collect(graph, packages, extras, groups, additions):
    chosen = {}
    pending = []

    def enqueue(reference, wanted_extras):
        if not marker_holds(reference.get("marker"), graph.environment):
            return
        package = graph.pick(reference)
        pending.append((package, set(wanted_extras) | set(reference.get("extra", []))))

    for name in packages:
        root = graph.pick({"name": name})
        pending.append((root, set(extras) & set(root.get("optional-dependencies", {}))))
        for group in groups:
            for reference in root.get("dev-dependencies", {}).get(group, []):
                enqueue(reference, [])
    if packages:
        for group in groups:
            for reference in graph.root_groups.get(group, []):
                enqueue({k: v for k, v in reference.items() if k != "specifier"}, [])
    for package in additions:
        pending.append((package, set()))

    while pending:
        package, wanted = pending.pop()
        key = (normalize(package["name"]), package["version"])
        seen_extras = chosen.get(key, (None, None))[1]
        if seen_extras is not None and wanted <= seen_extras:
            continue
        chosen[key] = (package, (seen_extras or set()) | wanted)
        if seen_extras is None:
            for reference in package.get("dependencies", []):
                enqueue(reference, [])
        for extra in wanted - (seen_extras or set()):
            for reference in package.get("optional-dependencies", {}).get(extra, []):
                enqueue(reference, [])
    return [value[0] for value in chosen.values()]


def requirement_lines(selection):
    registry = []
    editable = []
    for package in selection:
        source = package["source"]
        if "editable" in source:
            editable.append(source["editable"])
        elif "registry" in source:
            hashes = [wheel["hash"] for wheel in package.get("wheels", [])]
            if "hash" in package.get("sdist", {}):
                hashes.append(package["sdist"]["hash"])
            line = f"{package['name']}=={package['version']}"
            line += "".join(f" --hash={digest}" for digest in hashes)
            registry.append(line)
        elif "virtual" not in source:
            raise SystemExit(
                f"install_from_uv_lock: unsupported source {source} for {package['name']}"
            )
    return sorted(registry), sorted(editable)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="uv.lock")
    parser.add_argument("--python", required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--group", action="append", default=["dev"])
    parser.add_argument("--with", dest="additions", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    options = parser.parse_args()

    graph = LockGraph(options.lock, target_environment(options.python))

    locked_additions = []
    unlocked_additions = []
    for requirement in options.additions:
        name = requirement
        for stop in "<>=!~[ ;":
            name = name.split(stop, 1)[0]
        package = graph.lookup(name)
        if package is None:
            unlocked_additions.append(requirement)
        else:
            locked_additions.append(package)

    selection = collect(graph, options.package, options.extra, options.group, locked_additions)
    registry, editable = requirement_lines(selection)

    if options.list:
        for package in sorted(selection, key=lambda p: normalize(p["name"])):
            print(f"{normalize(package['name'])}=={package['version']}")
        for requirement in unlocked_additions:
            print(f"UNLOCKED {requirement}")
        return

    pip = [sys.executable, "-m", "pip", "--python", options.python, "install", "--no-cache-dir"]
    pip_environment = dict(os.environ, PIP_RETRIES="10", PIP_DEFAULT_TIMEOUT="120")
    requirements_path = "/tmp/uv-lock-requirements.txt"
    with open(requirements_path, "w") as handle:
        handle.write("\n".join(registry) + "\n")
    subprocess.check_call(
        pip + ["--no-deps", "--require-hashes", "-r", requirements_path], env=pip_environment
    )
    for path in editable:
        subprocess.check_call(pip + ["--no-deps", "-e", path], env=pip_environment)
    if unlocked_additions:
        subprocess.check_call(pip + unlocked_additions, env=pip_environment)


if __name__ == "__main__":
    main()
'''


def render_gate_command() -> str:
    modules = ", ".join(GATE_MODULES)
    return f".venv/bin/python -c \"import {modules}; print('DEPS_OK')\""


class AutogenUvWorkspaceBaseImage(Image):
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
        return BASE_PYTHON_IMAGE

    def image_tag(self) -> str:
        return BASE_IMAGE_TAG

    def workdir(self) -> str:
        return BASE_IMAGE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_INPUT=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    UV_HTTP_TIMEOUT=300 \\
    UV_LINK_MODE=copy \\
    UV_NO_PROGRESS=1 \\
    UV_PYTHON_DOWNLOADS=never

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    ca-certificates \\
    pkg-config \\
    libffi-dev \\
    libssl-dev \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class AutogenUvWorkspacePullRequestImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return AutogenUvWorkspaceBaseImage(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        test_paths = collect_test_paths(self.pr.test_patch)
        package_dir = resolve_package_dir(test_paths)
        rendered_paths = " ".join(
            f'"{path}"' for path in relative_test_paths(test_paths, package_dir)
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
            File(
                ".",
                "strip_binaries.sh",
                r"""#!/bin/bash
awk '
BEGIN { skip = 0 }
/^diff --git / {
  skip = 0
  if ($0 ~ /\.(ico|icns|png|jpe?g|gif|bmp|webp|woff2?|ttf|eot|otf|pdf|zip|tar|tgz|tbz2?|txz|bz2|xz|gz|class|jar|war|ear|enc|gpg|asc|p7s|der|crt|key|pem|sig|odt|ods|odp|docx|xlsx|pptx|msg|vsdx|db|sqlite3?|bin|dat|so|dll|dylib|a|o|obj|exe|wasm|mp[34]|wav|ogg|flac|webm|mov|avi|mkv|faiss|pkl|npy|npz|joblib|model|onnx|pt|pth|safetensors|h5|parquet|arrow|feather|index)( |$)/) skip = 1
}
{ if (!skip) print }
' "$1"
""",
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
set +e
export CI=true

cd /home/[[REPO]]/[[PACKAGE]] || exit 1

PATHS=()
for candidate in [[TEST_PATHS]]; do
    [ -e "$candidate" ] && PATHS+=("$candidate")
done

if [ ${#PATHS[@]} -eq 0 ]; then
    echo "run_tests: none of this PR's test files are present yet" >&2
    exit 0
fi

/home/[[REPO]]/[[WORKSPACE]]/.venv/bin/python -m pytest -o addopts= --no-header -rA --tb=no \
    -p no:cacheprovider --continue-on-collection-errors "${PATHS[@]}" 2>&1 \
    | sed -E "s#^(PASSED|FAILED|ERROR|XFAIL|XPASS)[[:space:]]+#\1 pytest::[[PACKAGE]]/#; s#^(SKIPPED \[[0-9]+\])[[:space:]]+#\1 pytest::[[PACKAGE]]/#"
exit 0
""".replace("[[TEST_PATHS]]", rendered_paths)
                .replace("[[PACKAGE]]", package_dir)
                .replace("[[WORKSPACE]]", WORKSPACE_DIR)
                .replace("[[REPO]]", repo),
            ),
            File(".", LOCK_INSTALLER_NAME, LOCK_INSTALLER_SCRIPT),
            File(
                ".",
                "prepare.sh",
                r"""#!/bin/bash
set -e

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git rev-parse --verify --quiet "[[SHA]]^{commit}" >/dev/null 2>&1 \
    || git fetch --depth=1 origin [[SHA]] 2>/dev/null \
    || git fetch origin 2>/dev/null || true
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

locked_version_of () {
    .venv/bin/python - "$1" <<'LOCKED_EOF'
import re
import sys

name = sys.argv[1]
with open("uv.lock", encoding="utf-8") as handle:
    lock_text = handle.read()
pattern = r'^\[\[package\]\]\nname = "%s"\nversion = "([^"]+)"' % re.escape(name)
found = re.search(pattern, lock_text, re.M)
print(found.group(1) if found else "")
LOCKED_EOF
}

install_requirements_at_locked_version () {
    local requirement
    local target
    local name
    local version
    for requirement in "$@"; do
        target="${requirement%%[<>=!~]*}"
        name="${target%%[[]*}"
        version="$(locked_version_of "$name")"
        if [ -n "$version" ]; then
            uv pip install --python .venv/bin/python "${target}==${version}"
        else
            uv pip install --python .venv/bin/python "$requirement"
        fi
    done
}

cd /home/[[REPO]]/[[WORKSPACE]]
if [ "$(uname -m)" = "[[EMULATED_MACHINE]]" ]; then
    [[PIP_PROVISION]]
else
    [[UV_PROVISION]]
fi
test -x .venv/bin/python

[[GATE_COMMAND]]
""".replace("[[TEST_PATHS]]", rendered_paths)
                .replace("[[PIP_PROVISION]]", render_pip_provision(self.pr.fix_patch))
                .replace("[[UV_PROVISION]]", render_uv_provision(self.pr.fix_patch))
                .replace("[[EMULATED_MACHINE]]", EMULATED_MACHINE)
                .replace("[[GATE_COMMAND]]", render_gate_command())
                .replace("[[PACKAGE]]", package_dir)
                .replace("[[WORKSPACE]]", WORKSPACE_DIR)
                .replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /tmp/test.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
bash /home/strip_binaries.sh /home/test.patch > /tmp/test.filtered.patch
bash /home/strip_binaries.sh /home/fix.patch  > /tmp/fix.filtered.patch
if ! git -C /home/[[REPO]] apply --whitespace=nowarn /tmp/test.filtered.patch /tmp/fix.filtered.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run_tests.sh
""".replace("[[REPO]]", repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{sha}"

RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("microsoft", "autogen_6845_to_5844")
class Autogen6845To5844(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AutogenUvWorkspacePullRequestImage(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^PASSED\s+(.+?)\s*$")
        re_fail = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_error = re.compile(r"^ERROR\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_skip = re.compile(
            r"^SKIPPED\s+\[\d+\]\s+((?:\[\S+?\]\s+)?\S+?:\d+)(?::\s.*)?\s*$"
        )
        re_xfail = re.compile(r"^XFAIL\s+(.+?)(?:\s+-\s.*)?\s*$")
        re_xpass = re.compile(r"^XPASS\s+(.+?)(?:\s+-\s.*)?\s*$")

        for raw_line in test_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for pattern, bucket in (
                (re_pass, passed_tests),
                (re_fail, failed_tests),
                (re_error, failed_tests),
                (re_skip, skipped_tests),
                (re_xfail, skipped_tests),
                (re_xpass, passed_tests),
            ):
                match = pattern.match(line)
                if match:
                    bucket.add(match.group(1))
                    break

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
