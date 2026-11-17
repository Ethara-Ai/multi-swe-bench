import os
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_PY_IMAGE = "python:3.12.11-bookworm"
_UV_VERSION = "0.9.5"
_MAX_PYTHON = "3.13"
_MIN_PYTHON = "3.8"
_VENVS = "/opt/mswe/venvs"
_PLAN = "/home/mswe-plan.json"
_CFG_DIR = "/home/mswe-cfg"
_FILE_TIMEOUT = 1500
_INSTALL_ATTEMPTS = 3
_TEST_FILE = re.compile(r"^tests/(?:.+/)?test_[^/]*\.py$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (tests/\S*?\.py(?:::.+?)?)(?: - .*)?$")
_VERBOSE_SKIP = re.compile(r"^(tests/\S*?\.py::.+?) SKIPPED")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PLAN_HELPER = r'''import configparser
import json
import os
import re
import sys

CFG = sys.argv[1]
REPO = sys.argv[2]
MAX_PY = tuple(int(x) for x in sys.argv[3].split("."))
MIN_PY = tuple(int(x) for x in sys.argv[4].split("."))
EXTRA = sys.argv[5]
TEST_FILES = sys.argv[6:]

COND = re.compile(r"^([A-Za-z0-9_.!,{}\-]+):\s+(.+)$")


def expand(text):
    m = re.search(r"\{([^{}]*)\}", text)
    if not m:
        return [text]
    out = []
    for alt in m.group(1).split(","):
        out.extend(expand(text[: m.start()] + alt.strip() + text[m.end():]))
    return out


def alternatives(cond):
    alts = []
    for part in expand(cond):
        alts.extend(a.strip() for a in part.split(",") if a.strip())
    return alts


def cond_matches(cond, factors):
    for alt in alternatives(cond):
        ok = True
        for f in alt.split("-"):
            if f.startswith("!"):
                if f[1:] in factors:
                    ok = False
            elif f not in factors:
                ok = False
        if ok:
            return True
    return False


def py_tuple(factor):
    m = re.fullmatch(r"py(\d+)\.(\d+)", factor)
    return (int(m.group(1)), int(m.group(2))) if m else None


def version_key(parts):
    if parts == ["latest"]:
        return (2, ())
    nums = re.findall(r"\d+", "-".join(parts))
    return (1 if nums else 0, tuple(int(n) for n in nums), -len(parts))


cp = configparser.RawConfigParser(strict=False, interpolation=None)
cp.optionxform = str
cp.read(os.path.join(CFG, "tox.ini"))
extra_env, extra_deps, extra_set = [], [], []
for raw in open(EXTRA).read().splitlines():
    line = raw.split(" #")[0].strip()
    if not line or line.startswith(("#", ";", "[")):
        continue
    m = COND.match(line)
    if m and re.match(r"^[A-Z_][A-Z0-9_]*=", m.group(2)):
        extra_set.append(line)
    elif m:
        extra_deps.append(line)
    elif re.match(r"^\{?py[0-9.]", line) and "=" not in line:
        extra_env.append(line)
print(f"plan: patch tox additions envs={len(extra_env)} deps={len(extra_deps)} setenv={len(extra_set)}", file=sys.stderr)

envs = []
for line in cp.get("tox", "envlist", fallback="").splitlines() + extra_env:
    line = line.split("#")[0].strip()
    names = [n.strip() for x in expand(line) for n in x.split(",")] if line else []
    for name in names:
        if not name:
            continue
        factors = name.split("-")
        py = py_tuple(factors[0])
        if py is None or not (MIN_PY <= py <= MAX_PY):
            continue
        integ = factors[1] if len(factors) > 1 else "common"
        envs.append((name, py, integ, factors[2:]))

known = {e[2] for e in envs}


def group_of(path):
    m = re.match(r"tests/integrations/([^/]+)/", path)
    if m and m.group(1) in known:
        return m.group(1)
    print(f"plan: {path} has no own tox env, using common", file=sys.stderr)
    return "common"


def choose_env(group):
    cands = [e for e in envs if e[2] == group]
    if not cands:
        cands = [e for e in envs if e[2] == "common"]
    best = max(cands, key=lambda e: (version_key(e[3]), e[1]))
    return best


deps_lines = cp.get("testenv", "deps", fallback="").splitlines() + extra_deps
setenv_lines = cp.get("testenv", "setenv", fallback="").splitlines() + extra_set
groups_cfg = cp.get("testenv", "dependency_groups", fallback="").split()


def resolve_req(req):
    req = req.replace("{toxinidir}", CFG).strip()
    if "{" in req:
        print(f"plan: dropping unresolvable dep {req}", file=sys.stderr)
        return None
    if req.startswith(("-r", "-c")):
        flag, path = req[:2], req[2:].strip()
        return f"{flag} {os.path.join(CFG, path)}"
    if req == "-e .":
        return f"-e {REPO}"
    return req


def pyproject_group(name, seen=None):
    seen = seen or set()
    path = os.path.join(CFG, "pyproject.toml")
    if not os.path.isfile(path) or name in seen:
        return []
    import tomllib

    data = tomllib.load(open(path, "rb")).get("dependency-groups", {})
    out = []
    for item in data.get(name, []):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "include-group" in item:
            out.extend(pyproject_group(item["include-group"], seen | {name}))
    return out


plan = {}
for path in TEST_FILES:
    g = group_of(path)
    if g not in plan:
        name, py, integ, rest = choose_env(g)
        factors = set(name.split("-"))
        reqs = []
        for raw in deps_lines:
            line = raw.split(" #")[0].strip()
            if not line or line.startswith("#"):
                continue
            m = COND.match(line)
            if m:
                if not cond_matches(m.group(1), factors):
                    continue
                line = m.group(2).strip()
            r = resolve_req(line)
            if r:
                reqs.append(r)
        for gname in groups_cfg:
            reqs.extend(pyproject_group(gname))
        if not any(r.startswith("-e ") for r in reqs):
            reqs.append(f"-e {REPO}")
        env = {}
        for raw in setenv_lines:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            m = COND.match(line)
            if m:
                if not cond_matches(m.group(1), factors):
                    continue
                line = m.group(2).strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"')
            if "{" in val or key.startswith("UV_") or key.startswith("COVERAGE"):
                continue
            env[key] = val
        plan[g] = {"env": name, "python": f"{py[0]}.{py[1]}", "reqs": reqs, "setenv": env, "files": []}
    plan[g]["files"].append(path)

json.dump(plan, sys.stdout, indent=1)
'''

_RUNNER = r'''import json
import os
import signal
import subprocess
import sys

PLAN = sys.argv[1]
VENVS = sys.argv[2]
TIMEOUT = int(sys.argv[3])
COLLECT = len(sys.argv) > 4 and sys.argv[4] == "collect"

plan = json.load(open(PLAN))
rc = 0
collected = 0
for group, spec in plan.items():
    python = os.path.join(VENVS, group, "bin", "python")
    env = dict(os.environ)
    env.update(spec["setenv"])
    for path in spec["files"]:
        if not os.path.isfile(path):
            print(f"MSWE_SKIP_MISSING {group} {path}", flush=True)
            continue
        print(f"MSWE_FILE_START {group} {path}", flush=True)
        if COLLECT:
            cmd = [python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", path]
        else:
            cmd = [python, "-m", "pytest", "-rA", "-v", "-p", "no:cacheprovider", "--continue-on-collection-errors", path]
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = 124
        print(f"MSWE_FILE_END {group} {path} rc={code}", flush=True)
        if code in (0, 5):
            collected += 1
        else:
            rc = 1
if COLLECT:
    sys.exit(0 if collected or rc == 0 else 1)
sys.exit(rc)
'''


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _test_files(pr: PullRequest) -> list[str]:
    paths = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({path for path in paths if _TEST_FILE.match(path)})


def _tox_additions(pr: PullRequest) -> str:
    lines = []
    for patch in (pr.test_patch, pr.fix_patch):
        for section in re.split(r"^(?=diff --git )", patch, flags=re.M):
            if not re.match(r"diff --git a/tox\.ini ", section):
                continue
            lines.extend(line[1:] for line in section.splitlines() if line.startswith("+") and not line.startswith("+++"))
    return "\n".join(lines) + "\n"


def _binary_excludes(pr: PullRequest) -> str:
    paths = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for section in re.split(r"^(?=diff --git )", patch, flags=re.M):
            header = re.match(r"diff --git a/\S+ b/(\S+)", section)
            if header and re.search(r"^(?:Binary files |GIT binary patch)", section, re.M):
                paths.add(header.group(1))
    return "".join(f" --exclude={shlex.quote(path)}" for path in sorted(paths))


def _uv_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            "export UV_PYTHON_PREFERENCE=only-managed",
            "export UV_LINK_MODE=copy",
            "export PIP_DISABLE_PIP_VERSION_CHECK=1",
        )
    )


def _retry_block() -> str:
    return f"""retry() {{
    for attempt in $(seq 1 {_INSTALL_ATTEMPTS}); do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -eq {_INSTALL_ATTEMPTS} ]; then
            return 1
        fi
        sleep 15
    done
}}"""


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    files = " ".join(shlex.quote(path) for path in _test_files(pr))
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _uv_env(),
        _retry_block(),
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e "{sha}^{{commit}}" || retry git fetch --no-tags origin {sha}
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "uv --version\ngit --version\njq --version",
        """cutoff=$(git log -1 --format=%cd --date=iso-strict HEAD)
echo "prepare: dependency cutoff $cutoff\"""",
        f"""rm -rf {_CFG_DIR}
mkdir -p {_CFG_DIR}
git ls-files -z -- tox.ini pyproject.toml setup.py setup.cfg '*requirements*.txt' | xargs -0 -r cp --parents -t {_CFG_DIR}
python3 /home/mswe_plan.py {_CFG_DIR} /home/{repo} {_MAX_PYTHON} {_MIN_PYTHON} /home/mswe-tox-extra.txt {files} > {_PLAN}
test "$(jq length {_PLAN})" -gt 0
jq -r 'to_entries[] | "prepare: group \\(.key) env \\(.value.env) python \\(.value.python) files \\(.value.files | length)"' {_PLAN}""",
        f"""rm -rf {_VENVS}
mkdir -p {_VENVS}
for group in $(jq -r 'keys[]' {_PLAN}); do
    py=$(jq -r --arg g "$group" '.[$g].python' {_PLAN})
    reqs={_CFG_DIR}/mswe-$group.txt
    jq -r --arg g "$group" '.[$g].reqs[]' {_PLAN} > "$reqs"
    echo "prepare: installing $group on python $py"
    retry uv python install "$py"
    uv venv --python "$py" "{_VENVS}/$group"
    sed -i "s#^-e /home/{repo}#/home/{repo}#" "$reqs"
    retry uv pip install --python "{_VENVS}/$group/bin/python" --exclude-newer "$cutoff" -r "$reqs" || uv pip install --python "{_VENVS}/$group/bin/python" --exclude-newer "$cutoff" --prerelease allow -r "$reqs"
    retry uv pip install --python "{_VENVS}/$group/bin/python" --no-deps --reinstall-package sentry-sdk -e "/home/{repo}"
done
rm -rf {_CFG_DIR}""",
        f"""for group in $(jq -r 'keys[]' {_PLAN}); do
    "{_VENVS}/$group/bin/python" -c "import sentry_sdk, pytest, sys; print('prepare:', sys.argv[1], sys.version.split()[0], 'pytest', pytest.__version__, sentry_sdk.__file__)" "$group"
    "{_VENVS}/$group/bin/python" -c "import os, sentry_sdk, sys; sys.exit(0 if os.path.realpath(sentry_sdk.__file__).startswith(os.path.realpath(sys.argv[1])) else 1)" "/home/{repo}"
done
python3 /home/mswe_run.py {_PLAN} {_VENVS} {_FILE_TIMEOUT} collect | grep -E "^MSWE_|error|Error" | tail -n 60""",
    )


def _run_script(pr: PullRequest, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{pr.repo}", apply) if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _uv_env(),
        setup,
        'redis-server --daemonize yes --save "" --appendonly no > /dev/null',
        f"python3 /home/mswe_run.py {_PLAN} {_VENVS} {_FILE_TIMEOUT}",
    )


class SentryPythonImageBase(Image):
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
        return _PY_IMAGE

    def image_tag(self) -> str:
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    LC_ALL=C.UTF-8 \\
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
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates jq build-essential pkg-config libpq-dev redis-server nodejs \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv=={_UV_VERSION}" && \\
    git config --system --add safe.directory '*' && \\
    python3 --version && uv --version && git --version && redis-server --version && node --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class SentryPythonImageDefault(Image):
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
        return SentryPythonImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        excludes = _binary_excludes(self.pr)
        test_apply = f"git apply --whitespace=nowarn{excludes} /home/test.patch"
        fix_apply = f"git apply --whitespace=nowarn{excludes} /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswe_plan.py", _PLAN_HELPER),
            File(".", "mswe_run.py", _RUNNER),
            File(".", "mswe-tox-extra.txt", _tox_additions(self.pr)),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(self.pr)),
            File(".", "test-run.sh", _run_script(self.pr, test_apply)),
            File(".", "fix-run.sh", _run_script(self.pr, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class SENTRY_PYTHON_7930_TO_1441(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SentryPythonImageDefault(self.pr, self._config)

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
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            summary = _SUMMARY.match(line)
            if summary:
                status, name = summary.group(1), summary.group(2).strip()
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            skipped = _VERBOSE_SKIP.match(line)
            if skipped:
                skipped_tests.add(skipped.group(1).strip())
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
