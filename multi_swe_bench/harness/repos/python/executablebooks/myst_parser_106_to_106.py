import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_TAG = "base-106_to_106"
PY_IMAGE = "python:3.7-bookworm"
UV_VERSION = "0.12.13"
ERA_CUTOFF = "2020-03-04T09:07:25Z"
ATOMICWRITES_CUTOFF = "2022-07-09T00:00:00Z"
MISTLETOE_EBP_CUTOFF = "2020-03-05T18:30:00Z"
MISTLETOE_EBP_VERSION = "0.8.2"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

CHECK_GIT_CHANGES = r"""#!/bin/bash
set -euo pipefail

cd "$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

EMIT_RESULTS = r"""import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]

try:
    tree = ET.parse(path)
except Exception as exc:
    sys.stdout.write("MSWEBENCH_EMIT_ERROR %s\n" % exc)
    sys.exit(0)

total = 0
for tc in tree.getroot().iter("testcase"):
    classname = tc.get("classname") or ""
    name = tc.get("name") or ""
    filename = tc.get("file") or ""

    if not filename and classname:
        filename = classname.replace(".", "/") + ".py"
    if not filename or not name:
        continue

    cls = ""
    if classname:
        module = classname.replace(".", "/")
        if filename.endswith(".py") and module.startswith(filename[:-3]):
            tail = module[len(filename) - 3:].lstrip("/")
            cls = tail

    status = "PASSED"
    for child in tc:
        if child.tag in ("failure", "error"):
            status = "FAILED"
            break
        if child.tag == "skipped":
            status = "SKIPPED"
            break

    name = name.replace("\r", " ").replace("\n", " ")
    node_id = filename + "::" + (cls + "::" if cls else "") + name
    sys.stdout.write("TESTCASE " + node_id + " " + status + "\n")
    total += 1

sys.stdout.write("MSWEBENCH_TOTAL %d\n" % total)
"""

PROVISION_BODY = r"""export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore

python -m pip install --ignore-requires-python --target /opt/uv --python-version 3.8 --only-binary=:all: --no-deps "uv==${UV_VERSION}"
export PATH="/opt/uv/bin:${PATH}"

PYTHON_BIN="$(command -v python)"
UV_INSTALL=(uv pip install --python "${PYTHON_BIN}" --exclude-newer "${ERA_CUTOFF}" --exclude-newer-package "atomicwrites=${ATOMICWRITES_CUTOFF}" --exclude-newer-package "mistletoe-ebp=${MISTLETOE_EBP_CUTOFF}")

"${UV_INSTALL[@]}" --reinstall setuptools wheel
"${UV_INSTALL[@]}" "mistletoe-ebp==${MISTLETOE_EBP_VERSION}"

python - > /home/requirements-myst.txt <<'MSWEBENCH_PY_EOF'
import runpy
import setuptools

captured = {}
setuptools.setup = lambda **kwargs: captured.update(kwargs)
runpy.run_path("setup.py", run_name="__main__")
for extra in ("testing", "sphinx"):
    for requirement in captured["extras_require"][extra]:
        print(requirement)
MSWEBENCH_PY_EOF

"${UV_INSTALL[@]}" -r /home/requirements-myst.txt
python -m pip install --no-deps --no-build-isolation -e .
"""

GATE_BODY = r"""python -m pytest --version
python -c "import mistletoe, myst_parser, sphinx, docutils, yaml, bs4, pytest, pytest_cov, pytest_regressions; from myst_parser.html_renderer import HTMLRenderer; from myst_parser.block_tokens import Document; assert myst_parser.__file__.startswith('/home/MyST-Parser/'), myst_parser.__file__; print('DEPS_OK')"
"""

TEST_BODY = r"""export CI=true

cd "$REPO_DIR"

rm -f /home/results.xml

set +e
python -m pytest -v --cov=myst_parser --cov-report= --junitxml=/home/results.xml
set -e

test -s /home/results.xml

echo "===== BEGIN TEST DETAIL ====="
python /home/emit_results.py /home/results.xml
echo "===== END TEST DETAIL ====="
"""


class MystParserImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return PY_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

WORKDIR /home/

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class MystParserImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "str | Image":
        return MystParserImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        prepare = "#!/bin/bash\n"
        prepare += "set -euo pipefail\n"
        prepare += "\n"
        prepare += f'REPO_DIR="{repo_dir}"\n'
        prepare += f'UV_VERSION="{UV_VERSION}"\n'
        prepare += f'ERA_CUTOFF="{ERA_CUTOFF}"\n'
        prepare += f'ATOMICWRITES_CUTOFF="{ATOMICWRITES_CUTOFF}"\n'
        prepare += f'MISTLETOE_EBP_CUTOFF="{MISTLETOE_EBP_CUTOFF}"\n'
        prepare += f'MISTLETOE_EBP_VERSION="{MISTLETOE_EBP_VERSION}"\n'
        prepare += "\n"
        prepare += f"cd {repo_dir}\n"
        prepare += "\n"
        prepare += "git reset --hard\n"
        prepare += "git clean -fdx\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += f"git checkout --detach {self.pr.base.sha}\n"
        prepare += f"bash /home/check_git_changes.sh {repo_dir}\n"
        prepare += "\n"
        prepare += "cat > /home/emit_results.py <<'MSWEBENCH_PY_EOF'\n"
        prepare += EMIT_RESULTS
        prepare += "MSWEBENCH_PY_EOF\n"
        prepare += "\n"
        prepare += PROVISION_BODY
        prepare += "\n"
        prepare += GATE_BODY

        run = "#!/bin/bash\n"
        run += "set -eo pipefail\n"
        run += f'REPO_DIR="{repo_dir}"\n'
        run += "\n"
        run += TEST_BODY

        test_run = "#!/bin/bash\n"
        test_run += "set -eo pipefail\n"
        test_run += f'REPO_DIR="{repo_dir}"\n'
        test_run += "\n"
        test_run += f'cd "{repo_dir}"\n'
        test_run += "git apply --whitespace=nowarn /home/test.patch\n"
        test_run += "\n"
        test_run += TEST_BODY

        fix_run = "#!/bin/bash\n"
        fix_run += "set -eo pipefail\n"
        fix_run += f'REPO_DIR="{repo_dir}"\n'
        fix_run += "\n"
        fix_run += f'cd "{repo_dir}"\n'
        fix_run += "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        fix_run += "\n"
        fix_run += TEST_BODY

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
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

{self.clear_env}
"""


class MystParserInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MystParserImageDefault(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            if not in_detail:
                continue

            match = case_re.match(stripped)
            if not match:
                continue
            name = match.group(1).strip()
            status = match.group(2)
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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


@Instance.register("executablebooks", "myst_parser_106_to_106")
class MYST_PARSER_106_TO_106(MystParserInstance):
    pass
