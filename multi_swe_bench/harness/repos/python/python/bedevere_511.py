import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_CONFIG_NAME = "bedevere_511"

_TEST_PATH_RE = re.compile(r"^diff --git a/(\S+\.py) b/", re.MULTILINE)


def _test_roots(pr: PullRequest) -> list[str]:
    roots = []
    for path in _TEST_PATH_RE.findall(pr.test_patch or ""):
        head = path.split("/", 1)[0]
        if head and head not in roots:
            roots.append(head)
    return roots


_BASE_DOCKERFILE = r"""FROM __BASE_IMAGE__
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
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}
LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"
WORKDIR /home/
RUN set -eux; \
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/ssl/certs /etc/pki/ca-trust/extracted/pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/certs/ca-bundle.crt; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/cert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/cacert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/certs/ca-bundle.crt
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential procps && rm -rf /var/lib/apt/lists/*
RUN git clone "${REPO_URL}" /home/__REPO__
CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__
WORKDIR /home/__REPO__
RUN git reset --hard
RUN git checkout __BASE_SHA__
RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "__BASE_SHA__")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
RUN if [ -f .gitmodules ]; then \
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
WORKDIR /home/
__COPY_COMMANDS__RUN bash /home/prepare.sh
"""


_CHECK_GIT_CHANGES_SH = r"""set -e

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


_PREPARE_SH = r"""set -eo pipefail

cd /home/__REPO__

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore

test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

python -m pip install --quiet --upgrade pip setuptools wheel

requirements=""
for candidate in dev-requirements.txt requirements-dev.txt requirements.txt; do
    if [ -f "$candidate" ]; then
        requirements="$candidate"
        break
    fi
done
test -n "$requirements"

python - "$requirements" /tmp/resolved-requirements.txt <<'PY'
import pathlib
import sys


def flatten(path, seen):
    path = pathlib.Path(path).resolve()
    if path in seen:
        return []
    seen.add(path)
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            lines.extend(flatten(path.parent / line.split(None, 1)[1].strip(), seen))
            continue
        lines.append(line)
    return lines


pathlib.Path(sys.argv[2]).write_text("\n".join(flatten(sys.argv[1], set())) + "\n", encoding="utf-8")
PY

installed=0
for attempt in 1 2 3 4 5 6 7 8; do
    if python -m pip install --quiet -r /tmp/resolved-requirements.txt > /tmp/pip-install.log 2>&1; then
        installed=1
        break
    fi
    cat /tmp/pip-install.log >&2
    missing="$(grep -oE 'No matching distribution found for [^ ]+' /tmp/pip-install.log | head -n1 | awk '{print $NF}')"
    if [ -n "$missing" ]; then
        name="${missing%%[=<>!~]*}"
        python - "$name" /tmp/resolved-requirements.txt <<'PY'
import pathlib
import re
import sys

name = sys.argv[1]
path = pathlib.Path(sys.argv[2])
pattern = re.compile(r"^" + re.escape(name) + r"\s*[=<>!~]", re.IGNORECASE)
lines = [name if pattern.match(line) else line for line in path.read_text(encoding="utf-8").splitlines()]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
        echo "prepare: relaxed unavailable pin for ${name}" >&2
    else
        echo "prepare: install attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    fi
done
test "$installed" -eq 1

python -m pytest --version
echo "DEPS_OK"
"""


_SUITE_BODY = r"""cd /home/__REPO__

run_suite() {
    label="$1"
    shift
    echo "===== suite: ${label} ====="
    "$@" || echo "===== suite-failed: ${label} (exit $?) ====="
}

for root in __TEST_ROOTS__; do
    if [ -d "$root" ]; then
        run_suite "$root" python -m pytest -v -rA -p no:cacheprovider "$root"
    else
        echo "===== suite-missing: ${root} ====="
    fi
done

exit 0
"""


_APPLY_TEST_PATCH = r"""cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

"""


_APPLY_FIX_PATCH = r"""if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi

"""


class BedevereImageBase(Image):
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
        return "python:3.10-slim"

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

        return DockerfileEnhancer.SYNTAX_DIRECTIVE + "\n" + (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class BedevereImageDefault(Image):
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
        return BedevereImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        roots = " ".join(f"'{root}'" for root in _test_roots(self.pr))
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__TEST_ROOTS__", roots)
        )

    def files(self) -> list[File]:
        header = "set -uo pipefail\n\n"
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(header + _SUITE_BODY)),
            File(
                ".",
                "test-run.sh",
                self._render(header + _APPLY_TEST_PATCH + _SUITE_BODY),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(
                    header + _APPLY_TEST_PATCH + _APPLY_FIX_PATCH + _SUITE_BODY
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return DockerfileEnhancer.SYNTAX_DIRECTIVE + "\n" + (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_SUITE_RE = re.compile(r"^=+\s*suite(?:-failed|-missing)?:\s*(.+?)\s*=+$")
_STATUS = r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
_NODE = r"(?P<name>\S+\.py::\S+)"
_VERBOSE_RE = re.compile(rf"^{_NODE}\s+{_STATUS}\b")
_SUMMARY_RE = re.compile(rf"^{_STATUS}\s+{_NODE}")

_PASS_STATUS = {"PASSED", "XPASS"}
_FAIL_STATUS = {"FAILED", "ERROR"}
_SKIP_STATUS = {"SKIPPED", "XFAIL"}


def bedevere_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)

    def record(name: str, status: str) -> None:
        if status in _FAIL_STATUS:
            failed_tests.add(name)
        elif status in _PASS_STATUS:
            passed_tests.add(name)
        elif status in _SKIP_STATUS:
            skipped_tests.add(name)

    for raw_line in clean_log.splitlines():
        line = raw_line.strip()

        if _SUITE_RE.match(line):
            continue

        match = _VERBOSE_RE.match(line)
        if match:
            record(match.group("name"), match.group("status"))
            continue

        match = _SUMMARY_RE.match(line)
        if match:
            record(match.group("name"), match.group("status"))

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


@Instance.register("python", _CONFIG_NAME)
class BEDEVERE_511(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BedevereImageDefault(self.pr, self._config)

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
        return bedevere_parse_log(test_log)


Instance._registry.setdefault("python/bedevere", BEDEVERE_511)
