import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "debian:bookworm"
NIX_VERSION = "2.31.2"
BASE_TAG = "base-3032_to_3207"

_NIX_CONF = (
    "build-users-group =\\n"
    "sandbox = false\\n"
    "filter-syscalls = false\\n"
    "experimental-features = nix-command flakes\\n"
    "accept-flake-config = true\\n"
    "tarball-ttl = 31536000\\n"
    "substituters = https://cache.nixos.org https://devenv.cachix.org https://cachix.cachix.org\\n"
    "trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= "
    "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw= "
    "cachix.cachix.org-1:eWNHQldwUO7G2VkjpnjDbWwy4KQ/HNxht7H4SSoMckM=\\n"
)

_ENV = """\
export CI=true
export TERM=dumb
export NO_COLOR=1
export USER=root
export HOME=/root
export PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH"
"""

_TARGETS = """\
DIRS=()
for p in $(grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u); do
  if [[ "$p" =~ ^((tests|examples)/[^/]+)/ ]]; then
    d="${BASH_REMATCH[1]}"
    if [ -f "$d/devenv.nix" ] || [ -f "$d/.test.sh" ]; then
      DIRS+=("$d")
    fi
  fi
done
mapfile -t DIRS < <(printf '%s\\n' "${DIRS[@]}" | sed '/^$/d' | sort -u)
test ${#DIRS[@]} -gt 0"""

_RUN = """\
rm -rf /tmp/msb && mkdir -p /tmp/msb
git add -A
nix build -L --out-link /tmp/msb/devenv 2>&1 || RC=$?
if [ -x /tmp/msb/devenv/bin/devenv-run-tests ]; then
  export PATH="/tmp/msb/devenv/bin:$PATH"
  for d in "${DIRS[@]}"; do
    parent="${d%%/*}"
    name="${d#*/}"
    echo "MSB_SUITE|devenv|$d"
    devenv-run-tests run "$parent" --only "$name" 2>&1 | tee /tmp/msb/out.log || RC=$?
    python3 /home/msb_parse.py "$d" "$name" /tmp/msb/out.log
  done
fi"""

_PARSER = """\
import re
import sys

label, name, path = sys.argv[1:4]
text = open(path, encoding="utf-8", errors="replace").read()
text = re.sub(r"\\x1b\\[[0-?]*[ -/]*[@-~]", "", text)
esc = re.escape(name)
if re.search(r"Passed: " + esc + r"(\\s*$|\\s+\\()", text, re.M):
    status = "PASS"
elif re.search(r"Skipping " + esc + r" \\(", text):
    status = "SKIP"
else:
    status = "FAIL"
print(f"MSB_TEST|{status}|{label}", flush=True)
"""

_PREPARE_CMD = """\
nix --version
nix build -L --out-link /opt/msb-devenv
test -x /opt/msb-devenv/bin/devenv
test -x /opt/msb-devenv/bin/devenv-run-tests
/opt/msb-devenv/bin/devenv version
REPO_DIR="$PWD"
rm -rf /tmp/msb-warm && mkdir -p /tmp/msb-warm
printf '%s\\n' '{ pkgs, ... }:' '{' '  packages = [ pkgs.coreutils ];' '}' > /tmp/msb-warm/devenv.nix
(cd /tmp/msb-warm && git init -q && /opt/msb-devenv/bin/devenv --override-input devenv "git+file:$REPO_DIR?dir=src/modules" shell -- true)
rm -rf /tmp/msb-warm
echo DEPS_OK"""

_TEST_CMD = f"""\
RC=0
{_TARGETS}
{_RUN}
exit $RC"""


_CHECK_GIT_CHANGES = """\
#!/bin/bash
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


class Devenv3032To3207ImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
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
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    USER=root \\
    HOME=/root \\
    PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH \\
    TERM=dumb \\
    CI=true

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl xz-utils procps python3 bash \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/nix && printf '{_NIX_CONF}' > /etc/nix/nix.conf

RUN mkdir -m 0755 /nix && \\
    curl -sSfL https://releases.nixos.org/nix/nix-{NIX_VERSION}/install | sh -s -- --no-daemon --no-channel-add --no-modify-profile && \\
    nix --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class Devenv3032To3207ImageDefault(Image):
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
        return Devenv3032To3207ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git config core.ignorecase false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK

PREPARE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_CMD", _TEST_CMD)
                .replace("PREPARE_CMD", _PREPARE_CMD)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_parse.py", _PARSER),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git config core.ignorecase false && git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
    git -c pack.threads=2 -c pack.windowMemory=512m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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


@Instance.register("cachix", "devenv_3032_to_3207")
class DEVENV_3032_TO_3207(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Devenv3032To3207ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }

        re_test = re.compile(r"^MSB_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")

        for line in log.splitlines():
            m = re_test.match(line.strip())
            if m:
                buckets[m.group(1)].add(m.group(2))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
