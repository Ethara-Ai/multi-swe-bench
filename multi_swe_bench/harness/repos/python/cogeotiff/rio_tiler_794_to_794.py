import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

_VERBOSE_RE = re.compile(
    r"^(?P<nodeid>\S+\.py::.+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)",
    re.MULTILINE,
)

_SUMMARY_RE = re.compile(
    r"^(?P<status>FAILED|ERROR)\s+(?P<nodeid>\S+\.py::.+?)(?:\s+-\s|\s*$)",
    re.MULTILINE,
)


def _gold_test_exclude_flags(test_patch: str) -> str:
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


_RUN_PYTEST = """pytest_status=0
timeout -k 60 1800 python -m pytest -v --color=no \\
    -p no:cacheprovider --continue-on-collection-errors 2>&1 || pytest_status=$?
printf '##### MSWEB-PYTEST-EXIT: %s\\n' "$pytest_status\""""


class RioTilerImageBase(Image):
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
        return "python:3.11"

    def image_tag(self) -> str:
        return "base-794-to-794"

    def workdir(self) -> str:
        return "base-794-to-794"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class RioTilerImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return RioTilerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gold_excludes = _gold_test_exclude_flags(self.pr.test_patch)

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
                "prepare.sh",
                """#!/bin/bash
set -euxo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
bash /home/check_git_changes.sh

python -m pip install --upgrade pip

pip install --no-cache-dir -e ".[test]" \\
    || pip install --no-cache-dir -e ".[test]" \\
    || true

pip install --no-cache-dir \\
    'numpy<2.3' \\
    'pandas<3' \\
    'xarray<2025.4' \\
    'h5py' \\
    || true

python -c "import rio_tiler; print(rio_tiler.__version__)"
python -c "import rasterio, pydantic, pystac, numpy, pandas, xarray, h5py, morecantile"
python -m pytest --version

bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(pr=self.pr, run_pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
""".format(
                    pr=self.pr,
                    gold_excludes=gold_excludes,
                    run_pytest=_RUN_PYTEST,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {self.pr.base.sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"; \\
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

RUN bash /home/prepare.sh

{self.clear_env}

"""


def parse_pytest_verbose(test_log: str) -> TestResult:
    text = ANSI_ESCAPE.sub("", test_log or "")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    def record(nodeid: str, status: str) -> None:
        if nodeid in passed or nodeid in failed or nodeid in skipped:
            return
        if status in ("PASSED", "XPASS"):
            passed.add(nodeid)
        elif status in ("FAILED", "ERROR"):
            failed.add(nodeid)
        elif status in ("SKIPPED", "XFAIL"):
            skipped.add(nodeid)

    for m in _VERBOSE_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    for m in _SUMMARY_RE.finditer(text):
        record(m.group("nodeid"), m.group("status"))

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("cogeotiff", "rio_tiler_794_to_794")
class RIO_TILER_794_TO_794(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RioTilerImageDefault(self.pr, self._config)

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
        return parse_pytest_verbose(test_log)


Instance.register("cogeotiff", "rio-tiler")(RIO_TILER_794_TO_794)
