import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_CONFIG_NAME = "publiclab_editor_521"


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
RUN set -eux; \
    installed=0; \
    for attempt in 1 2 3 4 5; do \
        if apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 install -y --no-install-recommends git ca-certificates chromium fonts-liberation curl xz-utils procps; then \
            installed=1; \
            break; \
        fi; \
        sleep 20; \
    done; \
    test "$installed" -eq 1; \
    rm -rf /var/lib/apt/lists/*
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

export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

node_major=""
if [ -f .travis.yml ]; then
    node_major="$(awk '/^node_js:/{flag=1;next} flag && /^[^ -]/{flag=0} flag' .travis.yml | grep -oE '[0-9]+' | sort -n | tail -1 || true)"
fi
if [ -z "$node_major" ] && [ -f .nvmrc ]; then
    node_major="$(grep -oE '[0-9]+' .nvmrc | head -1 || true)"
fi
if [ -z "$node_major" ] && [ -f package.json ]; then
    node_major="$(sed -n 's/.*"node" *: *"[^0-9]*\([0-9][0-9]*\).*/\1/p' package.json | head -1 || true)"
fi
test -n "$node_major"
echo "prepare: node major ${node_major}"

case "$(dpkg --print-architecture)" in
    amd64) node_arch=x64 ;;
    arm64) node_arch=arm64 ;;
    *) node_arch="$(dpkg --print-architecture)" ;;
esac

node_tarball=""
for attempt in 1 2 3 4 5; do
    node_tarball="$(curl -fsSL "https://nodejs.org/dist/latest-v${node_major}.x/SHASUMS256.txt" | grep "linux-${node_arch}.tar.xz" | awk '{print $2}' || true)"
    if [ -n "$node_tarball" ]; then
        break
    fi
    sleep 15
done
test -n "$node_tarball"

node_installed=0
for attempt in 1 2 3 4 5; do
    if curl -fsSL "https://nodejs.org/dist/latest-v${node_major}.x/${node_tarball}" | tar -xJ -C /usr/local --strip-components=1; then
        node_installed=1
        break
    fi
    sleep 15
done
test "$node_installed" -eq 1
node --version
npm --version

git config --global url."https://github.com/".insteadOf "git://github.com/"

npm_command="install"
if [ -f package-lock.json ]; then
    npm_command="ci"
fi

deps_installed=0
for attempt in 1 2 3; do
    if npm "$npm_command" --ignore-scripts; then
        deps_installed=1
        break
    fi
    echo "prepare: npm ${npm_command} attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$deps_installed" -eq 1

test -x node_modules/.bin/jest
echo "DEPS_OK"
"""


_SUITE_BODY = r"""cd /home/__REPO__

export CI=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
PUPPETEER_EXECUTABLE_PATH="$(command -v chromium || command -v chromium-browser || true)"
export PUPPETEER_EXECUTABLE_PATH

run_suite() {
    label="$1"
    shift
    echo "===== suite: ${label} ====="
    "$@" || echo "===== suite-failed: ${label} (exit $?) ====="
}

run_suite jest node node_modules/.bin/jest --ci --verbose

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


class PublicLabEditorImageBase(Image):
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
        return "debian:bookworm-slim"

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


class PublicLabEditorImageDefault(Image):
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
        return PublicLabEditorImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
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
_BANNER_RE = re.compile(r"^=+\s*suite(?:-failed)?:\s*(.+?)\s*=+$")
_FILE_RE = re.compile(r"^(PASS|FAIL)\s+(?:\S+\s+)?(\S+\.[cm]?[jt]sx?)\b")
_CASE_RE = re.compile(
    r"^\s+(?P<mark>[✓✔✕×✗○])\s+(?:skipped\s+)?(?P<name>.+?)"
    r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
)


def publiclab_editor_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)
    current_file = ""
    current_file_failed = False
    current_file_has_cases = False

    def close_file() -> None:
        if current_file and current_file_failed and not current_file_has_cases:
            failed_tests.add(current_file)

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()

        if _BANNER_RE.match(line.strip()):
            continue

        file_match = _FILE_RE.match(line)
        if file_match:
            close_file()
            current_file = file_match.group(2)
            current_file_failed = file_match.group(1) == "FAIL"
            current_file_has_cases = False
            continue

        case_match = _CASE_RE.match(line)
        if not case_match:
            continue

        current_file_has_cases = True
        name = case_match.group("name").strip()
        test_id = f"{current_file} > {name}" if current_file else name
        mark = case_match.group("mark")
        if mark in "✓✔":
            passed_tests.add(test_id)
        elif mark in "✕×✗":
            failed_tests.add(test_id)
        else:
            skipped_tests.add(test_id)

    close_file()

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


@Instance.register("publiclab", _CONFIG_NAME)
class PUBLICLAB_EDITOR_521(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PublicLabEditorImageDefault(self.pr, self._config)

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
        return publiclab_editor_parse_log(test_log)


Instance._registry.setdefault("publiclab/PublicLab.Editor", PUBLICLAB_EDITOR_521)
