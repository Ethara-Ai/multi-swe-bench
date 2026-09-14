import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "python:3.8-bullseye"


def _diff_paths(patch: str):
    for line in (patch or "").split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        p = parts[3]
        yield p.removeprefix("b/")


def _test_files(pr: PullRequest) -> list[str]:
    files = set()
    for path in _diff_paths(pr.test_patch):
        name = path.rsplit("/", 1)[-1]
        if (
            "/tests/" in f"/{path}"
            and name.startswith("test_")
            and name.endswith(".py")
        ):
            files.add(path)
    return sorted(files)


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

_ENV_SH = """cd /home/{repo}
ST2_COMPONENTS="$(ls -d st2*/ contrib/runners/*/ 2>/dev/null | sed 's:/$::')"
PYTHONPATH=""
for c in $ST2_COMPONENTS; do
    PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}/home/{repo}/$c"
done
for a in contrib/*/actions; do
    [ -d "$a" ] && PYTHONPATH="$PYTHONPATH:/home/{repo}/$a"
done
export PYTHONPATH
"""

_PREPARE_SH = """#!/bin/bash
set -e

sed -i '/debian-security/d' /etc/apt/sources.list
apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl gnupg wget build-essential \\
    libldap2-dev libsasl2-dev libssl-dev libffi-dev libyaml-dev ldap-utils \\
    rabbitmq-server
wget -qO - https://www.mongodb.org/static/pgp/server-4.4.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-4.4.gpg
echo 'deb [signed-by=/usr/share/keyrings/mongodb-server-4.4.gpg arch=arm64,amd64] http://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse' > /etc/apt/sources.list.d/mongodb-org-4.4.list
apt-get update && apt-get install -y --no-install-recommends mongodb-org
mkdir -p /data/db
rm -rf /var/lib/apt/lists/*

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

if grep -q "^mongoengine==0.18" requirements.txt; then
    pip install --upgrade "pip<23.0" "setuptools<58" wheel
    pip install --use-deprecated=legacy-resolver \\
        "pyOpenSSL==19.1.0" \\
        "dnspython<2.0.0,>=1.16.0" \\
        "importlib-metadata<5.0" \\
        "markupsafe==2.0.1" \\
        -r requirements.txt
else
    pip install --upgrade "pip<23.0" "setuptools<58" wheel
    pip install --use-deprecated=legacy-resolver \\
        "pyOpenSSL<=21.0.0" \\
        "MarkupSafe<2.1.0,>=0.23" \\
        -r requirements.txt
fi
pip install --use-deprecated=legacy-resolver -r test-requirements.txt

{env}
for c in $ST2_COMPONENTS; do
    if [ -f "$c/setup.py" ]; then
        (cd "/home/{repo}/$c" && python setup.py develop --no-deps >/dev/null 2>&1) || true
    fi
done

git reset --hard
git clean -fd -e '*.egg-info'
python -c "import st2tests; print('st2tests import OK')"
"""

_RUN_SH = """#!/bin/bash
set -e
{env}
{patch_step}
mongod --fork --logpath /var/log/mongodb.log >/dev/null 2>&1 || true
rabbitmq-server -detached >/dev/null 2>&1 || true
sleep 3

for t in {test_files}; do
    if [ -f "$t" ]; then
        echo "=== Running $t ==="
        python -c "from pymongo import MongoClient; MongoClient('127.0.0.1', 27017).drop_database('st2-test')" || true
        out="$(nosetests -s -v "$t" 2>&1 || true)"
        echo "$out"
        # Module failed to import (e.g. test.patch references code added by
        # fix.patch): nose emits a single "Failure: ImportError" pseudo-test, so
        # report every statically discovered test in the file as ERROR instead.
        if echo "$out" | grep -qE "^Failure: [A-Za-z]*Error .* \\.\\.\\. ERROR"; then
            python /home/list_tests.py "$t" | sed 's/$/ ... ERROR/'
        fi
    fi
done
"""

# Lists "test_name (dotted.module.Class)" for a test file without importing it,
# using the same package-dotted module path nose prints.
_LIST_TESTS_PY = """import ast
import os
import sys

path = sys.argv[1]
module = os.path.splitext(os.path.basename(path))[0]
d = os.path.dirname(path)
while d and os.path.isfile(os.path.join(d, "__init__.py")):
    module = os.path.basename(d) + "." + module
    d = os.path.dirname(d)

tree = ast.parse(open(path).read())
for node in tree.body:
    if not isinstance(node, ast.ClassDef):
        continue
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
            print("%s (%s.%s)" % (item.name, module, node.name))
"""

_PATCH_STEP = """
if ! git -C /home/{repo} apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
"""


class _Img(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class St2_5037To5605ImageBase(_Img):
    def dependency(self) -> str | Image:
        return BASE_IMAGE

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class St2_5037To5605ImageDefault(_Img):
    def dependency(self) -> Image:
        return St2_5037To5605ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run(self, name: str, *patches: str) -> File:
        repo = self.pr.repo
        patch_step = (
            _PATCH_STEP.format(repo=repo, patches=" ".join(patches)) if patches else ""
        )
        return File(
            ".",
            name,
            _RUN_SH.format(
                env=_ENV_SH.format(repo=repo),
                patch_step=patch_step,
                test_files=" ".join(_test_files(self.pr)),
            ),
        )

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "list_tests.py", _LIST_TESTS_PY),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=repo, sha=self.pr.base.sha, env=_ENV_SH.format(repo=repo)
                ),
            ),
            self._run("run.sh"),
            self._run("test-run.sh", "/home/test.patch"),
            self._run("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        copy_commands = copy_commands.rstrip("\n")

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")

_LINE_RE = re.compile(
    r"^(?P<test>\S+)\s+\((?P<cls>[^)]+)\)\s+\.\.\.\s+"
    r"(?P<status>ok|passed|FAIL|FAILED|ERROR|SKIP|skipped)\b"
)


@Instance.register("StackStorm", "st2_5037_to_5605")
@Instance.register("StackStorm", "5037_to_5605")
@Instance.register("StackStorm", "st2")
class ST2_5037_TO_5605(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return St2_5037To5605ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.strip())
            if not m:
                continue
            name = f"{m.group('cls').strip()}.{m.group('test')}"
            status = m.group("status")
            if status in ("ok", "passed"):
                passed.add(name)
            elif status in ("FAIL", "FAILED", "ERROR"):
                failed.add(name)
            else:
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
