import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_IMAGE = "python:3.12-slim-trixie"
_PY_BIN = "python3.12"
_BASE_TAG = "base-2906-to-2143"
_ROOT_PACKAGE = "mempalace"
_TEST_USER = "mswb"
_TEST_UID = "1000"
_EMULATED_MACHINE = "x86_64"
_LOCK_INSTALLER = "install_from_uv_lock.py"
_UV_SYNC_ARGS = "--frozen --extra dev --group dev"
_LOCK_ARGS = f"--package {_ROOT_PACKAGE} --extra dev --group dev"

_TEST_FILE_RE = re.compile(
    r"^\+\+\+ b/(tests/(?:[^\s/]+/)*test_[^\s/]+\.py)$", re.MULTILINE
)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_FILE_RE.findall(pr.test_patch or "")))


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_LOCK_INSTALLER_PY = r'''import argparse
import json
import os
import subprocess
import sys

try:
    import tomllib
except ImportError:
    from pip._vendor import tomli as tomllib

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


def collect(graph, options):
    chosen = {}
    pending = []

    def enqueue(reference, wanted_extras):
        if not marker_holds(reference.get("marker"), graph.environment):
            return
        package = graph.pick(reference)
        pending.append((package, set(wanted_extras) | set(reference.get("extra", []))))

    for name in options.package:
        root = graph.pick({"name": name})
        declared_extras = set(root.get("optional-dependencies", {}))
        declared_groups = set(root.get("dev-dependencies", {}))
        extras = declared_extras if options.all_extras else set(options.extra) & declared_extras
        groups = declared_groups if options.all_groups else set(options.group) & declared_groups
        extras -= set(options.no_extra)
        groups -= set(options.no_group)
        pending.append((root, extras))
        for group in sorted(groups):
            for reference in root.get("dev-dependencies", {}).get(group, []):
                enqueue(reference, [])

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
    parser.add_argument("--no-extra", dest="no_extra", action="append", default=[])
    parser.add_argument("--all-extras", dest="all_extras", action="store_true")
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--no-group", dest="no_group", action="append", default=[])
    parser.add_argument("--all-groups", dest="all_groups", action="store_true")
    parser.add_argument("--list", action="store_true")
    options = parser.parse_args()

    graph = LockGraph(options.lock, target_environment(options.python))
    selection = collect(graph, options)
    registry, editable = requirement_lines(selection)

    if options.list:
        for package in sorted(selection, key=lambda p: normalize(p["name"])):
            print(f"{normalize(package['name'])}=={package['version']}")
        return

    pip = [
        sys.executable,
        "-m",
        "pip",
        "--python",
        options.python,
        "install",
        "--no-cache-dir",
        "--ignore-requires-python",
    ]
    pip_environment = dict(os.environ, PIP_RETRIES="10", PIP_DEFAULT_TIMEOUT="120")
    requirements_path = "/tmp/uv-lock-requirements.txt"
    with open(requirements_path, "w") as handle:
        handle.write("\n".join(registry) + "\n")
    subprocess.check_call(
        pip + ["--no-deps", "--require-hashes", "-r", requirements_path], env=pip_environment
    )
    for path in editable:
        subprocess.check_call(pip + ["--no-deps", "-e", path], env=pip_environment)


if __name__ == "__main__":
    main()
'''

_PREPARE_SH = r"""#!/bin/bash
set -e

mswb_apply_patch() {
  for patch in "$@"; do
      if git apply --whitespace=nowarn "$patch" 2>/dev/null; then
          echo "apply_patch: $patch applied cleanly"
      else
          echo "apply_patch: $patch needs --3way"
          git apply --3way --whitespace=nowarn "$patch"
          echo "apply_patch: $patch applied via --3way"
      fi
  done
  if grep -rqE '^<<<<<<< ' --include='*.py' --exclude-dir=.git --exclude-dir=.venv .; then
      echo "apply_patch: CONFLICT MARKERS present after apply" >&2
      exit 1
  fi
}

mswb_present() {
  MSWB_FILES=()
  local f
  for f in "$@"; do
      if [ -f "$f" ]; then
          MSWB_FILES+=("$f")
      else
          echo "mswb: $f is not in the tree yet"
      fi
  done
}

mswb_as_user() {
  runuser -u __TEST_USER__ -- bash -c 'ulimit -n "$(ulimit -Hn)" && exec env HOME=/home/__TEST_USER__ CI=true "$@"' mswb_as_user "$@"
}

mswb_pytest() {
  mswb_present "$@"
  rm -f /tmp/mswb_pytest.xml
  if [ ${#MSWB_FILES[@]} -eq 0 ]; then
      echo "mswb: none of the test files exist yet"
      return 0
  fi
  mswb_as_user .venv/bin/python -m pytest -p no:cacheprovider -o junit_family=xunit1 \
      --continue-on-collection-errors \
      --junitxml=/tmp/mswb_pytest.xml -rfE --tb=short "${MSWB_FILES[@]}"
  echo "mswb: pytest exit $?"
}

mswb_emit() {
.venv/bin/python - <<'MSWB_EMIT'
import os
import xml.etree.ElementTree as ET

path = "/tmp/mswb_pytest.xml"
if not os.path.exists(path):
    print("mswb: no junit report was written")
    raise SystemExit(0)
rank = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
status = {}
for case in ET.parse(path).getroot().iter("testcase"):
    file = case.get("file") or ""
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    if not file.endswith(".py"):
        print("mswb: testcase without a file: %s %s" % (classname, name))
        continue
    module = file[:-3].replace("/", ".")
    if not classname and name == module:
        nodeid = file
    else:
        parts = [file]
        if classname.startswith(module + "."):
            parts.extend(classname[len(module) + 1:].split("."))
        parts.append(name)
        nodeid = "::".join(parts)
    kids = {child.tag for child in case}
    if "failure" in kids or "error" in kids:
        st = "FAILED"
    elif "skipped" in kids:
        st = "SKIPPED"
    else:
        st = "PASSED"
    if nodeid not in status or rank[st] > rank[status[nodeid]]:
        status[nodeid] = st
for nodeid in sorted(status):
    print("pytest::%s %s" % (nodeid, status[nodeid]))
MSWB_EMIT
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"

if [ "$(uname -m)" = "__EMULATED_MACHINE__" ]; then
    python3 -m venv --without-pip .venv
    python3 /home/__LOCK_INSTALLER__ --python .venv/bin/python __LOCK_ARGS__
else
    uv venv --python __PY_BIN__ .venv
    uv sync __UV_SYNC_ARGS__
    uv cache clean
fi
test -x .venv/bin/python

test "$(mswb_as_user id -u)" = "__TEST_UID__"
test "$(mswb_as_user sh -c 'ulimit -n')" -gt 1024
mswb_as_user .venv/bin/python -c "import os, mempalace, chromadb, pytest, hypothesis; assert os.geteuid() != 0"
mswb_present __TEST_FILES__
if [ ${#MSWB_FILES[@]} -gt 0 ]; then
    mswb_as_user .venv/bin/python -m pytest -p no:cacheprovider --collect-only -q "${MSWB_FILES[@]}" > /tmp/mswb_collect.log
    tail -n 1 /tmp/mswb_collect.log
fi
echo "gate: mempalace, pytest and the PR test files load as __TEST_USER__"
"""

_ACT_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

. /home/prepare.sh

__PATCH_STEP__
chown -R __TEST_USER__:__TEST_USER__ /home/__REPO__
set +e
mswb_pytest __TEST_FILES__
mswb_emit
"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class _MempalaceImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class Mempalace2906To2143ImageBase(_MempalaceImage):
    def dependency(self) -> str | Image:
        return _PY_IMAGE

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

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    PIP_ROOT_USER_ACTION=ignore \\
    UV_HTTP_TIMEOUT=180 \\
    UV_LINK_MODE=copy \\
    UV_NO_PROGRESS=1 \\
    UV_PYTHON_DOWNLOADS=never

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential pkg-config curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN useradd --create-home --uid {_TEST_UID} --shell /bin/bash {_TEST_USER}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Mempalace2906To2143ImageDefault(_MempalaceImage):
    def dependency(self) -> Image:
        return Mempalace2906To2143ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _common_values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__TEST_USER__": _TEST_USER,
            "__TEST_FILES__": " ".join(_test_files(self.pr)),
        }

    def _prepare(self) -> str:
        values = self._common_values()
        values.update(
            {
                "__SHA__": self.pr.base.sha,
                "__PY_BIN__": _PY_BIN,
                "__UV_SYNC_ARGS__": _UV_SYNC_ARGS,
                "__EMULATED_MACHINE__": _EMULATED_MACHINE,
                "__LOCK_INSTALLER__": _LOCK_INSTALLER,
                "__LOCK_ARGS__": _LOCK_ARGS,
                "__TEST_UID__": _TEST_UID,
            }
        )
        return _render(_PREPARE_SH, values)

    def _act(self, name: str, *patches: str) -> File:
        values = self._common_values()
        values["__PATCH_STEP__"] = (
            f"mswb_apply_patch {' '.join(patches)}" if patches else ""
        )
        return File(".", name, _render(_ACT_SH, values))

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", _LOCK_INSTALLER, _LOCK_INSTALLER_PY),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        copy_commands = copy_commands.rstrip("\n")

        base_commit_arg = f'ARG BASE_COMMIT="{self.pr.base.sha}"'
        hardening = Image._HARDENING_BLOCK.replace(
            "git gc --prune=now --aggressive", "git gc --prune=now"
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{base_commit_arg}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>pytest::tests/\S.*?\.py(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("MemPalace", "mempalace_2906_to_2143")
class MEMPALACE_2906_TO_2143(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Mempalace2906To2143ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
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
