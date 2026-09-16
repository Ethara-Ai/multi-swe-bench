import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TEST_BODY = r"""
set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '(^|/)(test_[^/]*\.py|[^/]*_test\.py)$' \
    | sort -u)
set -e

TEST_TARGETS=""
for f in $TEST_FILES; do
    [ -f "$f" ] && TEST_TARGETS="$TEST_TARGETS $f"
done

if [ -z "$TEST_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

pip install --no-cache-dir -q -e ".[test]"

set +e
echo "running pytest on:$TEST_TARGETS"
timeout -k 60 900 python -m pytest -v -rA --no-header --tb=short \
    -p no:cacheprovider --timeout=120 --continue-on-collection-errors \
    $TEST_TARGETS 2>&1
"""


class OpsdroidEraImageBase(Image):
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
        return "python:3.8-buster"

    def image_tag(self) -> str:
        return "base-1654_to_1650"

    def workdir(self) -> str:
        return "base-1654_to_1650"

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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

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

RUN rm -f /etc/apt/sources.list.d/debian.sources && \\
    printf 'deb http://archive.debian.org/debian buster main\\n' > /etc/apt/sources.list && \\
    printf 'Acquire::Check-Valid-Until "false";\\n' > /etc/apt/apt.conf.d/99no-check-valid

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    build-essential \\
    pkg-config \\
    libssl-dev \\
    libffi-dev \\
    libolm-dev \\
    ffmpeg \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade "pip<24" "setuptools<68" wheel

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class OpsdroidEraImageDefault(Image):
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
        return OpsdroidEraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        prepare = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --no-tags --depth=1 origin [[SHA]] 2>/dev/null || git fetch --no-tags origin 2>/dev/null || true
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

EXTRAS=$(python - <<'PYEOF'
import configparser
c = configparser.ConfigParser()
c.read("setup.cfg")
s = "options.extras_require"
names = [k for k in c[s]] if c.has_section(s) else []
print(",".join(n for n in names if n != "docs"))
PYEOF
)
echo "extras declared by this commit: ${EXTRAS:-<none>}"

pip install --no-cache-dir -e ".[test]"
for E in $(echo "${EXTRAS}" | tr ',' ' '); do
    [ "$E" = "test" ] && continue
    pip install --no-cache-dir -e ".[${E}]" || echo "optional extra ${E} unavailable for this era"
done

python -c "import opsdroid, pytest, pytest_asyncio, pytest_timeout; print('DEPS_OK', pytest.__version__)"
python -m pytest --version
"""

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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
                "prepare.sh",
                prepare.replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
""" + _TEST_BODY).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
""" + _TEST_BODY).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
""" + _TEST_BODY).replace("[[REPO]]", repo),
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
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
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


@Instance.register("opsdroid", "opsdroid_1654_to_1650")
class Opsdroid1654To1650(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpsdroidEraImageDefault(self.pr, self._config)

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

        inline = re.compile(
            r"^(?P<name>\S+::\S+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<name>\S+::\S+)"
        )

        for line in clean.split("\n"):
            stripped = line.strip()
            m = inline.match(stripped) or summary.match(stripped)
            if not m:
                continue
            name = m.group("name").strip()
            status = m.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
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
