import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_CONFIG_NAME = "oils_1037"
_PR_NUMBER = 1037
_RUN_USER = "tester"

_ADDED_PY_RE = re.compile(r"^diff --git a/(\S+\.py) b/", re.MULTILINE)


def _script_targets(pr: PullRequest) -> list[str]:
    return sorted(
        {
            path
            for path in _ADDED_PY_RE.findall(pr.test_patch or "")
            if path.startswith("test/")
        }
    )


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
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/certs/ca-bundle.crt; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/cert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/tls/cacert.pem; \
    ln -sf ${CA_CERT_PATH} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \
    ln -sf ${CA_CERT_PATH} /etc/ssl/certs/ca-bundle.crt
RUN set -eux; \
    if ! apt-get update 2>/dev/null; then \
        codename="$(. /etc/os-release; echo "$VERSION_CODENAME")"; \
        printf 'deb http://archive.debian.org/debian %s main\ndeb http://archive.debian.org/debian-security %s/updates main\n' "$codename" "$codename" > /etc/apt/sources.list; \
        printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99no-check-valid-until; \
        apt-get update; \
    fi; \
    apt-get install -y --no-install-recommends git ca-certificates build-essential libreadline-dev gawk procps time; \
    rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pexpect
RUN id -u __RUN_USER__ > /dev/null 2>&1 || useradd -m -s /bin/bash __RUN_USER__
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

test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

if [ -f .gitmodules ]; then
    initialised=0
    for attempt in 1 2 3; do
        if git submodule update --init --recursive; then
            initialised=1
            break
        fi
        echo "prepare: submodule attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done
    test "$initialised" -eq 1
fi

python --version
python -c "import pexpect; print('PEXPECT_OK')"

build/dev.sh minimal

test -x bin/osh
echo 'echo BUILD_OK' | bin/osh
echo "DEPS_OK"
"""


_SUITE_BODY = r"""cd /home/__REPO__

export OSH_TEST_INTERACTIVE_TIMEOUT="${OSH_TEST_INTERACTIVE_TIMEOUT:-15}"
export TERM=dumb

run_suite() {
    label="$1"
    shift
    echo "===== oils-suite: ${label} ====="
    "$@" || echo "===== oils-suite-failed: ${label} (exit $?) ====="
}

as_unprivileged() {
    if [ "$(id -u)" = "0" ] && id -u __RUN_USER__ > /dev/null 2>&1; then
        chown -R __RUN_USER__ /home/__REPO__ 2>/dev/null || true
        runuser -u __RUN_USER__ -- env \
            HOME="/home/__RUN_USER__" \
            TERM="$TERM" \
            OSH_TEST_INTERACTIVE_TIMEOUT="$OSH_TEST_INTERACTIVE_TIMEOUT" \
            "$@"
    else
        "$@"
    fi
}

if [ -f test/unit.sh ]; then
    run_suite unit bash test/unit.sh minimal
fi

for target in __SCRIPT_TARGETS__; do
    if [ -f "$target" ]; then
        run_suite "$target" as_unprivileged python "$target"
    else
        echo "===== oils-suite-missing: ${target} ====="
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


class OilsImageBase(Image):
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
        return "python:2.7-buster"

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
            .replace("__RUN_USER__", _RUN_USER)
        )


class OilsImageDefault(Image):
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
        return OilsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        targets = " ".join(f"'{path}'" for path in _script_targets(self.pr))
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__RUN_USER__", _RUN_USER)
            .replace("__SCRIPT_TARGETS__", targets)
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
_SUITE_RE = re.compile(r"^=+\s*oils-suite:\s*(.+?)\s*=+$")
_SCRIPT_RESULT_RE = re.compile(r"^(?P<name>.*\S)\s+\.\.\.\s*(?P<status>OK|Fail)\s*$")
_MODULE_HEADER_RE = re.compile(r"^\[(?P<name>\S+\.py)\]\s*$")
_MODULE_OK_RE = re.compile(r"^OK(?:\s*\(.*\))?\s*$")
_MODULE_FAIL_RE = re.compile(r"^FAILED(?:\s*\(.*\))?\s*$")
_PY_OK_RE = re.compile(r"^(?P<name>\S+)\s+\.\.\.\s*ok\s*$")
_PY_FAIL_RE = re.compile(r"^(?P<name>\S+)\s+\.\.\.\s*(?:FAIL|ERROR)\s*$")
_PY_SKIP_RE = re.compile(r"^(?P<name>\S+)\s+\.\.\.\s*skipped")


def oils_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)
    suite = ""
    module = ""

    def qualify(name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip()
        return f"{suite} > {name}" if suite else name

    for raw_line in clean_log.splitlines():
        line = raw_line.strip()

        banner = _SUITE_RE.match(line)
        if banner:
            suite = banner.group(1)
            module = ""
            continue

        header = _MODULE_HEADER_RE.match(line)
        if header:
            module = header.group("name")
            continue

        if module:
            if _MODULE_FAIL_RE.match(line):
                failed_tests.add(qualify(module))
                module = ""
                continue
            if _MODULE_OK_RE.match(line):
                passed_tests.add(qualify(module))
                module = ""
                continue

        match = _PY_SKIP_RE.match(line)
        if match:
            skipped_tests.add(qualify(match.group("name")))
            continue

        match = _PY_FAIL_RE.match(line)
        if match:
            failed_tests.add(qualify(match.group("name")))
            continue

        match = _PY_OK_RE.match(line)
        if match:
            passed_tests.add(qualify(match.group("name")))
            continue

        match = _SCRIPT_RESULT_RE.match(line)
        if match:
            name = qualify(match.group("name"))
            if match.group("status") == "OK":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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


@Instance.register("oils-for-unix", _CONFIG_NAME)
class OILS_1037(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OilsImageDefault(self.pr, self._config)

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
        return oils_parse_log(test_log)


Instance._registry.setdefault(f"oils-for-unix/{_PR_NUMBER}", OILS_1037)
