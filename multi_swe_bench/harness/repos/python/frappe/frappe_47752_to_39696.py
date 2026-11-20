import os
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_REPO = re.sub(r"_\d+_to_\d+$", "", _ERA)
_PY_IMAGE = "python:3.14-slim"
_APT = (
    "git ca-certificates curl build-essential pkg-config redis-server "
    "libmariadb-dev libpq-dev libcups2-dev libgl1 libglib2.0-0 libffi-dev"
)
_BENCH = "/home/frappe-bench"
_SITE = "test_site"
_DB_NAME = "test_frappe"
_REDIS = "redis://127.0.0.1:6379"
_REDIS_START = "redis-server --daemonize yes --save '' --appendonly no --port 6379"
_SITE_CONFIG = (
    f'{{"db_type":"sqlite","db_name":"{_DB_NAME}","developer_mode":0,'
    f'"allow_tests":1,"server_script_enabled":1,'
    f'"redis_cache":"{_REDIS}","redis_queue":"{_REDIS}","redis_socketio":"{_REDIS}"}}'
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CLASS = re.compile(r"^([A-Za-z_][\w.]*\.[A-Za-z_]\w*)\s*$")
_RESULT = re.compile(r"^[xu]?\s*(✔|✖|=)\s+(\S+)")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$")

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


def _sections(*parts: str) -> str:
    return "\n\n".join(filter(None, parts)) + "\n"


def _python_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            "export PYTHONUNBUFFERED=1",
            "export PIP_DISABLE_PIP_VERSION_CHECK=1",
        )
    )


def _test_modules(pr: PullRequest) -> list[str]:
    modules = set()
    path = ""
    deleted = False
    for line in (pr.test_patch or "").split("\n"):
        header = _DIFF_HEADER.match(line)
        if header:
            path = header.group(2)
            deleted = False
            continue
        if not path:
            continue
        if line.startswith("deleted file mode"):
            deleted = True
            continue
        if deleted or not path.endswith(".py") or not path.startswith(f"{_REPO}/"):
            continue
        name = path.rsplit("/", 1)[-1]
        if name.startswith("test_") or "/tests/" in path:
            modules.add(path[: -len(".py")].replace("/", "."))
    return sorted(modules)


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    modules = " ".join(_test_modules(pr))
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _python_env(),
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        _REDIS_START,
        f"""python -m pip install --no-input -q --upgrade pip setuptools wheel
python -m pip install --no-input -q .
python -m pip install --no-input -q ".[test]" responses
python -m pip uninstall -y -q {repo} || true
python --version""",
        f"""mkdir -p {_BENCH}/sites {_BENCH}/logs {_BENCH}/apps {_BENCH}/env/bin /logs
ln -sfn /home/{repo} {_BENCH}/apps/{repo}
ln -sf "$(command -v python)" {_BENCH}/env/bin/python
echo {repo} > {_BENCH}/sites/apps.txt
printf '%s\\n' '{_SITE_CONFIG}' > {_BENCH}/sites/common_site_config.json
cd {_BENCH}/sites
PYTHONPATH=/home/{repo} python -m frappe.utils.bench_helper frappe new-site {_SITE} --db-type sqlite --db-name {_DB_NAME} --admin-password admin""",
        f"""PYTHONPATH=/home/{repo} python -c "from frappe.commands.testing import main; print('DEPS_OK runner=ok')"
PYTHONPATH=/home/{repo} python -c "import frappe; frappe.init('{_SITE}'); frappe.connect(); print('DEPS_OK frappe=%s' % frappe.__version__)"
test -s {_BENCH}/sites/apps.txt
test -s {_BENCH}/sites/{_SITE}/site_config.json
test -s {_BENCH}/sites/{_SITE}/db/{_DB_NAME}.db
modules=$(echo '{modules}' | wc -w)
echo "prepare: $modules test modules"
test "$modules" -gt 0""",
        f"""cd /home/{repo}
bash /home/check_git_changes.sh""",
    )


def _test_command(pr: PullRequest) -> str:
    modules = " ".join(_test_modules(pr))
    return f"""cd {_BENCH}/sites
CI=1 FRAPPE_TEST_WEB_SERVER=0 FRAPPE_TEST_BACKGROUND_WORKER=0 FRAPPE_TEST_SMTP_SERVER=0 PYTHONPATH=/home/{pr.repo} FRAPPE_TEST_MODULES="{modules}" python -c 'import os, importlib, frappe
from frappe.commands.testing import main
frappe.init("{_SITE}")
frappe.connect()
mods = []
for m in os.environ["FRAPPE_TEST_MODULES"].split():
    try:
        importlib.import_module(m)
        mods.append(m)
    except Exception as exc:
        print("skip-unimportable " + m + ": " + str(exc))
if not mods:
    raise SystemExit("no importable test modules")
main(site="{_SITE}", app="{pr.repo}", module=mods)' 2>&1"""


def _run_script(pr: PullRequest, apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd /home/{pr.repo}", apply)))
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _python_env(),
        _REDIS_START,
        setup,
        _test_command(pr),
    )


def parse_frappe_log(log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    current = ""
    for line in _ANSI.sub("", log).splitlines():
        line = line.rstrip()
        if not line.strip():
            continue

        result = _RESULT.match(line)
        if result:
            verdict, method = result.groups()
            ident = f"{current}.{method}" if current else method
            if verdict == "✔":
                passed_tests.add(ident)
            elif verdict == "✖":
                failed_tests.add(ident)
            elif verdict == "=":
                skipped_tests.add(ident)
            continue

        header = _CLASS.match(line)
        if header and not line.startswith(" "):
            ident = header.group(1)
            if ident.startswith(f"{_REPO}."):
                current = ident

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


class Frappe47752To39696ImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends {_APT} && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' && python3 --version && git --version"""
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


class Frappe47752To39696ImageDefault(Image):
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
        return Frappe47752To39696ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(self.pr)),
            File(".", "test-run.sh", _run_script(self.pr, test_apply)),
            File(".", "fix-run.sh", _run_script(self.pr, fix_apply)),
        ]

    def dockerfile(self) -> str:
        names = " ".join(map(lambda file: file.name, self.files()))
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"COPY {names} /home/",
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _REPO)
class FRAPPE_47752_TO_39696(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Frappe47752To39696ImageDefault(self.pr, self._config)

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
        return parse_frappe_log(test_log)
