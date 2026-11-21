from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_TRAILING_RE = re.compile(
    r"^(?P<name>tests/\S+?)\s+(?P<status>" + "|".join(_STATUSES) + r")\b"
)
_LEADING_RE = re.compile(
    r"^(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?(?P<status>"
    + "|".join(_STATUSES)
    + r")\s+(?P<name>tests/\S+)"
)
_SKIP_SUMMARY_RE = re.compile(r"^SKIPPED \[\d+\] (?P<name>tests/[^:\s]+:\d+)")


def _test_files(test_patch: str) -> list[str]:
    files = []
    for path in _DIFF_HEADER_RE.findall(test_patch):
        name = path.rsplit("/", 1)[-1]
        if path.startswith("tests/") and name.startswith("test_") and name.endswith(".py"):
            if path not in files:
                files.append(path)
    return files


def _test_command(test_patch: str) -> str:
    modules = " ".join(shlex.quote(path) for path in _test_files(test_patch))
    return f"""files=()
for module in {modules}; do
    if [ -f "$module" ]; then
        files+=("$module")
    fi
done
if [ ${{#files[@]}} -eq 0 ]; then
    echo "no test modules to run"
    exit 0
fi
python -m pytest -p no:cacheprovider -rA -v "${{files[@]}}"
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=dirty) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_LOCK_REQS_PY = r"""import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

SELF_NAME = "pdm"
BOOTSTRAP = ("pip", "setuptools", "wheel")
BUILD = ("pdm-pep517", "setuptools", "wheel")


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def req_name(req):
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    if not match:
        raise SystemExit(f"cannot parse requirement: {req!r}")
    return match.group(1)


def req_extras(req):
    match = re.match(r"\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]*)\]", req)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def from_table(name, spec):
    if isinstance(spec, str):
        spec = {"version": spec}
    unknown = set(spec) - {"version", "marker", "markers", "extras"}
    if unknown:
        raise SystemExit(f"unsupported dependency table for {name}: {sorted(unknown)}")
    text = name
    extras = spec.get("extras") or []
    if extras:
        text += "[" + ",".join(extras) + "]"
    version = (spec.get("version") or "").strip()
    if version and version != "*":
        text += version
    marker = spec.get("marker") or spec.get("markers")
    if marker:
        text += "; " + marker
    return text


def as_list(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [from_table(name, spec) for name, spec in value.items()]
    return list(value)


def dev_groups(data):
    tool_pdm = data.get("tool", {}).get("pdm", {})
    project = data.get("project", {})
    groups = {}
    for name, items in data.get("dependency-groups", {}).items():
        groups[name] = list(items)
    dev = tool_pdm.get("dev-dependencies")
    if isinstance(dev, dict) and dev and all(isinstance(v, list) for v in dev.values()):
        for name, items in dev.items():
            groups.setdefault(name, list(items))
    elif dev:
        groups.setdefault("dev", as_list(dev))
    if project.get("dev-dependencies"):
        groups.setdefault("dev", as_list(project["dev-dependencies"]))
    return groups


def expand(items, groups, optional, seen):
    result = []
    for item in items:
        if isinstance(item, dict):
            include = item.get("include-group")
            if include is None:
                raise SystemExit(f"unsupported group entry: {item!r}")
            if ("group", include) not in seen:
                seen.add(("group", include))
                result += expand(groups.get(include, []), groups, optional, seen)
            continue
        if canon(req_name(item)) == SELF_NAME:
            for extra in req_extras(item):
                if ("extra", extra) not in seen:
                    seen.add(("extra", extra))
                    result += expand(optional.get(extra, []), groups, optional, seen)
            continue
        result.append(item)
    return result


def main():
    lock_path, pyproject_path, out_dir = sys.argv[1:4]
    wanted = sys.argv[4:]
    pyproject = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    lock = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    tool_pdm = pyproject.get("tool", {}).get("pdm", {})
    optional = {k: as_list(v) for k, v in project.get("optional-dependencies", {}).items()}

    requirements = as_list(project.get("dependencies")) or as_list(tool_pdm.get("dependencies"))
    groups = dev_groups(pyproject)
    found = [name for name in wanted if name in groups]
    if not found:
        raise SystemExit(f"none of the groups {wanted} exist; available: {sorted(groups)}")
    seen = set()
    for name in found:
        seen.add(("group", name))
        requirements += expand(groups[name], groups, optional, seen)
    requirements = expand(requirements, groups, optional, seen)

    pins = {}
    for package in lock.get("package", []):
        if "version" not in package:
            continue
        key = canon(package["name"])
        if key == SELF_NAME:
            continue
        pins.setdefault(key, []).append((package["name"], package["version"], package.get("marker")))

    constraints = []
    for key in sorted(pins):
        entries = pins[key]
        versions = {version for _, version, _ in entries}
        if len(versions) == 1:
            constraints.append(f"{entries[0][0]}=={entries[0][1]}")
        elif all(marker for _, _, marker in entries):
            for name, version, marker in entries:
                constraints.append(f"{name}=={version}; {marker}")
        else:
            print(f"pdm_lock_reqs: {key} has several unmarked versions, left unconstrained", file=sys.stderr)

    for req in requirements:
        if canon(req_name(req)) not in pins:
            print(f"pdm_lock_reqs: {req_name(req)} is not in the lock file", file=sys.stderr)

    bootstrap = [f"{pins[k][0][0]}=={pins[k][0][1]}" for k in BOOTSTRAP if k in pins and len(pins[k]) == 1]

    build = [f"{pins[k][0][0]}=={pins[k][0][1]}" for k in BUILD if k in pins and len(pins[k]) == 1]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "requirements.txt").write_text("\n".join(dict.fromkeys(requirements)) + "\n", encoding="utf-8")
    (out / "constraints.txt").write_text("\n".join(constraints) + "\n", encoding="utf-8")
    (out / "bootstrap.txt").write_text("\n".join(bootstrap) + "\n", encoding="utf-8")
    (out / "build-constraints.txt").write_text("\n".join(build) + "\n", encoding="utf-8")
    for name in ("bootstrap.txt", "build-constraints.txt", "requirements.txt"):
        print(f"--- {name}")
        print((out / name).read_text(encoding="utf-8"), end="")
    print(f"--- constraints.txt: {len(constraints)} pins")


if __name__ == "__main__":
    main()
"""

_DEV_INSTALL_PY = r"""import importlib
import sys
import sysconfig
from pathlib import Path


def main():
    backend_name = sys.argv[1]
    build_constraints = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    repo = Path.cwd().resolve()
    sys.path.insert(0, str(repo))
    backend = importlib.import_module(backend_name)
    purelib = Path(sysconfig.get_paths()["purelib"])
    for stale in purelib.glob("pdm-*.dist-info"):
        raise SystemExit(f"pdm_dev_install: unexpected existing metadata {stale}")
    dist_name = backend.prepare_metadata_for_build_wheel(str(purelib))
    dist_dir = purelib / dist_name
    if not (dist_dir / "METADATA").is_file():
        raise SystemExit(f"pdm_dev_install: backend wrote no METADATA into {dist_dir}")
    (dist_dir / "INSTALLER").write_text("pip\n", encoding="utf-8")
    (dist_dir / "direct_url.json").write_text(
        '{"url": "' + repo.as_uri() + '", "dir_info": {"editable": true}}\n',
        encoding="utf-8",
    )
    (purelib / "pdm_editable.pth").write_text(str(repo) + "\n", encoding="utf-8")
    if build_constraints is not None:
        Path(sys.prefix, "pip.conf").write_text(
            f"[global]\nconstraint = {build_constraints}\n", encoding="utf-8"
        )
    print(f"pdm_dev_install: {dist_name} -> {repo}")


if __name__ == "__main__":
    main()
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

export PATH="/home/venv/bin:$PATH"

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
git lfs pull
bash /home/check_git_changes.sh

python -m venv /home/tools
/home/tools/bin/python -m pip install --no-cache-dir "setuptools==56.0.0" "tomli==1.2.3"
/home/tools/bin/python -I /home/pdm_lock_reqs.py pdm.lock pyproject.toml /home/pdm_reqs test

python -m venv /home/venv
/home/venv/bin/python -m pip install --no-cache-dir -r /home/pdm_reqs/bootstrap.txt
/home/venv/bin/python -m pip install --no-cache-dir -c /home/pdm_reqs/constraints.txt -r /home/pdm_reqs/requirements.txt
/home/venv/bin/python -I /home/pdm_dev_install.py pdm.pep517.api /home/pdm_reqs/build-constraints.txt

/home/venv/bin/python -I -c "import pdm.core as m, pytest; assert m.__file__.startswith('/home/__REPO__/pdm/'), m.__file__; print(m.Core().version)"
/home/venv/bin/python -m pytest --version
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export PATH="/home/venv/bin:$PATH"

cd /home/__REPO__
"""


class Pdm734To528ImageBase(Image):
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
        return "python:3.8.20-slim-bookworm"

    def image_tag(self) -> str:
        interval = self.pr.number_interval.removeprefix(f"{self.pr.repo}_")
        return f"base-{interval}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {self.pr.number_interval} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git git-lfs build-essential libffi-dev libssl-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git lfs install --system

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Pdm734To528ImageDefault(Image):
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
        return Pdm734To528ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return template.replace("__REPO__", self.pr.repo).replace(
            "__BASE_SHA__", self.pr.base.sha
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        test_cmd = _test_command(self.pr.test_patch)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "pdm_lock_reqs.py", _LOCK_REQS_PY),
            File(".", "pdm_dev_install.py", _DEV_INSTALL_PY),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{test_cmd}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{test_cmd}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{test_cmd}"),
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

    test_log = _ANSI_RE.sub("", test_log)
    for raw in test_log.splitlines():
        line = raw.strip()
        status = None
        name = None
        match = _TRAILING_RE.match(line) or _LEADING_RE.match(line)
        if match:
            status = match.group("status")
            name = match.group("name")
        else:
            skip = _SKIP_SUMMARY_RE.match(line)
            if skip:
                status = "SKIPPED"
                name = skip.group("name")
        if not name or ("::" not in name and status != "SKIPPED"):
            continue
        if status in ("PASSED", "XFAIL", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
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


@Instance.register("pdm-project", "pdm_734_to_528")
class PDM_734_TO_528(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Pdm734To528ImageDefault(self.pr, self._config)

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
