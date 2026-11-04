import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "excalidraw_10947_to_12274"
BASE_TAG = "base-10947_to_12274"
NODE_IMAGE = "node:20-bookworm"

CHECK_GIT_CHANGES = """#!/bin/bash
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

ENV_BLOCK = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
"""

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
"""
)

APPLY_FN = r"""
apply_patches() {
    local excl=() f
    for f in $(awk '/^diff --git /{p=$3; sub(/^a\//,"",p)} /^Binary files .* differ$/{if(p!="")print p}' "$@" | sort -u); do
        excl+=("--exclude=$f")
        echo "apply_patches: excluding $f (binary change with no payload in the patch)"
    done
    git apply --whitespace=nowarn "${excl[@]}" "$@"
}
"""

INSTALL_CMD = "yarn install --frozen-lockfile --network-timeout 600000"

REINSTALL_BLOCK = (
    """
if { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | grep -qE '(^|/)package\\.json$|^yarn\\.lock$'; then
    yarn install --network-timeout 600000
fi
"""
)

TEST_BLOCK = """
yarn test:app --watch=false --reporter=tap-flat

"""

PREPARE_SH = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

node --version
yarn --version
"""
    + INSTALL_CMD
    + """
yarn vitest --version
node -e "for (const m of ['vitest', 'jsdom', 'vitest-canvas-mock', '@testing-library/jest-dom', '@testing-library/react']) require.resolve(m)"
echo "DEPS_OK"

"""
)

RUN_SH = RUN_HEADER + TEST_BLOCK

TEST_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)

FIX_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch /home/fix.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)


class ExcalidrawImageBase_10947_TO_12274(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}

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
    CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (node20 yarn1 vitest3 era)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \\
    done && \\
    apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    bash \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN node --version && yarn --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class ExcalidrawImageDefault_10947_TO_12274(Image):
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
        return ExcalidrawImageBase_10947_TO_12274(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return script.replace("__REPO__", self.pr.repo).replace(
            "__SHA__", self.pr.base.sha
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(PREPARE_SH)),
            File(".", "run.sh", self._render(RUN_SH)),
            File(".", "test-run.sh", self._render(TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("excalidraw", _INTERVAL)
class EXCALIDRAW_10947_TO_12274(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExcalidrawImageDefault_10947_TO_12274(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        tap_re = re.compile(r"^(?P<status>ok|not ok)\s+\d+\s+-\s+(?P<rest>.*)$")
        dir_re = re.compile(r"#\s*(SKIP|TODO)", re.IGNORECASE)
        time_re = re.compile(r"\s*#\s*time=[^\s#]*\s*$")
        seen: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).strip()

            match = tap_re.match(line)
            if not match:
                continue

            name = match.group("rest")
            directive = None

            found = dir_re.search(name)
            if found:
                directive = found.group(1).upper()
                name = name[: found.start()]

            name = time_re.sub("", name).rstrip()
            if not name:
                continue

            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"

            if directive:
                skipped_tests.add(name)
            elif match.group("status") == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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
