import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "engine_9439_to_9439"
BASE_TAG = f"base-{_INTERVAL}"
NODE_IMAGE = "node:24-bookworm"

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
export NODE_OPTIONS=--max-old-space-size=3072
export npm_config_fund=false
export npm_config_audit=false
"""

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """export NODE_ENV=test
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
    for f in "${excl[@]#--exclude=}"; do
        if [ -f "/home/binaries/${f}" ]; then
            mkdir -p "$(dirname "${f}")"
            cp "/home/binaries/${f}" "${f}"
            echo "apply_patches: restored ${f} from /home/binaries"
        fi
    done
}
"""

REINSTALL_BLOCK = """
changed="$(git diff --name-only HEAD; git ls-files --others --exclude-standard)"
if printf '%s\\n' "${changed}" | grep -E '(^|/)package\\.json$|(^|/)package-lock\\.json$' >/dev/null; then
    npm install --no-audit --no-fund
fi
"""

TEST_BLOCK = """
npm test -- --reporter tap
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

npm ci --no-audit --no-fund
node -e "require('canvas')" 2>/dev/null || npm rebuild canvas
./node_modules/.bin/mocha --version
node --input-type=module -e "await import('canvas'); await import('chai'); await import('jsdom'); await import('sinon'); console.log('DEPS_OK')"

"""
)

RUN_SH = RUN_HEADER + TEST_BLOCK

TEST_RUN_SH = RUN_HEADER + APPLY_FN + "apply_patches /home/test.patch\n" + REINSTALL_BLOCK + TEST_BLOCK

FIX_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch /home/fix.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)


def _payloadless_binaries(*patches: str) -> list[tuple[str, str]]:
    found = []
    for patch in patches:
        for chunk in re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE):
            if not re.search(r"^Binary files .* differ$", chunk, re.MULTILINE):
                continue
            path = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
            index = re.search(r"^index [0-9a-f]+\.\.([0-9a-f]+)", chunk, re.MULTILINE)
            if not path or not index or re.search(r"^Binary files .* and /dev/null differ$", chunk, re.MULTILINE):
                continue
            item = (path.group(1), index.group(1))
            if item not in found:
                found.append(item)
    return found


class EngineImageBase_9439_TO_9439(Image):
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

RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \\
    done && \\
    apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    pkg-config \\
    fonts-liberation \\
    libcairo2-dev \\
    libpango1.0-dev \\
    libjpeg-dev \\
    libgif-dev \\
    librsvg2-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class EngineImageDefault_9439_TO_9439(Image):
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
        return EngineImageBase_9439_TO_9439(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return script.replace("__REPO__", self.pr.repo).replace("__SHA__", self.pr.base.sha)

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

        binaries = _payloadless_binaries(self.pr.test_patch, self.pr.fix_patch)
        stage_binaries = ""
        if binaries:
            steps = "".join(
                f'    mkdir -p "/home/binaries/$(dirname "{path}")"; \\\n'
                f'    blob="$(git rev-parse "FETCH_HEAD:{path}")"; \\\n'
                f'    case "${{blob}}" in {index}*) ;; *) echo "blob mismatch for {path}"; exit 1 ;; esac; \\\n'
                f'    git cat-file blob "${{blob}}" > "/home/binaries/{path}"; \\\n'
                for path, index in binaries
            )
            stage_binaries = (
                "RUN set -eux; \\\n"
                f"    cd /home/{self.pr.repo}; \\\n"
                f'    git fetch --no-tags origin "pull/{self.pr.number}/head"; \\\n'
                + steps
                + "    rm -f .git/FETCH_HEAD; \\\n"
                + '    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"\n'
            )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{stage_binaries}

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


@Instance.register("playcanvas", _INTERVAL)
class ENGINE_9439_TO_9439(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EngineImageDefault_9439_TO_9439(self.pr, self._config)

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
        tap_re = re.compile(r"^(?P<status>ok|not ok) \d+ (?P<rest>.*)$")
        dir_re = re.compile(r"\s#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            match = tap_re.match(line)
            if not match:
                continue
            name = match.group("rest")
            found = dir_re.search(name)
            if found:
                name = name[: found.start()].strip()
                if name:
                    skipped_tests.add(name)
                continue
            name = name.strip()
            if not name:
                continue
            if match.group("status") == "ok":
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
