import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.23-bookworm"
NODE_VERSION = "18.20.4"
YARN_VERSION = "1.22.22"
BASE_TAG = "base-sphinx-tribes_1992_to_962"
FRONTEND_DIR = "frontend/app"

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM {base_image}

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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

ENV GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false \\
    CGO_ENABLED=1 \\
    HUSKY=0 \\
    CI=true

WORKDIR /home/

RUN set -eux; \\
    for i in 1 2 3; do \\
        apt-get update && \\
        apt-get install -y --no-install-recommends --fix-missing \\
            ca-certificates curl build-essential git gnupg make python3 sudo wget xz-utils \\
            postgresql postgresql-contrib \\
        && break || {{ echo "apt attempt $i failed, retrying"; sleep 10; }}; \\
    done; \\
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "$(dpkg --print-architecture)" in \\
        amd64) NODE_ARCH=x64 ;; \\
        arm64) NODE_ARCH=arm64 ;; \\
        *) echo "unsupported arch: $(dpkg --print-architecture)" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 \\
        -o /tmp/node.tar.xz "https://nodejs.org/dist/v{node_version}/node-v{node_version}-linux-${{NODE_ARCH}}.tar.xz"; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \\
    rm -f /tmp/node.tar.xz; \\
    npm install -g yarn@{yarn_version}; \\
    node --version; \\
    yarn --version

RUN set -eux; \\
    PGBIN=$(ls -d /usr/lib/postgresql/*/bin | head -1); \\
    mkdir -p /var/lib/postgresql/testdb /var/run/postgresql; \\
    chown -R postgres:postgres /var/lib/postgresql/testdb /var/run/postgresql; \\
    su postgres -c "$PGBIN/initdb -A trust -D /var/lib/postgresql/testdb"; \\
    su postgres -c "$PGBIN/pg_ctl -D /var/lib/postgresql/testdb -o '-p 5532' -w start"; \\
    su postgres -c "$PGBIN/createuser -p 5532 -s test_user"; \\
    su postgres -c "$PGBIN/createdb -p 5532 -O test_user test_db"; \\
    su postgres -c "$PGBIN/pg_ctl -D /var/lib/postgresql/testdb -w stop"

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=120 \\
            clone "${{REPO_URL}}" /home/{repo} && break; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    git -C /home/{repo} rev-parse HEAD

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""

GIT_HARDENING = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

PREPARE_SH = """#!/bin/bash
set -e
export CI=true
export HUSKY=0

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  git fetch --no-tags origin [[SHA]]
fi
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

go version
node --version
yarn --version

go mod download
go test -count=1 -run '^$' ./... > /dev/null 2>&1 || true

if [ -f [[FRONTEND]]/package.json ]; then
  yarn --cwd [[FRONTEND]] install --frozen-lockfile --network-timeout 600000
fi

git reset --hard

go build ./...
if [ -f [[FRONTEND]]/package.json ]; then
  (cd [[FRONTEND]] && node -e "require.resolve('jest'); require.resolve('ts-jest')" && yarn --silent jest --version)
fi
echo DEPS_OK
"""

TEST_CMD = """PGBIN="$(ls -d /usr/lib/postgresql/*/bin | head -1)"
PGDATA_DIR=/var/lib/postgresql/testdb
if ! su postgres -c "$PGBIN/pg_ctl -D $PGDATA_DIR status" > /dev/null 2>&1; then
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA_DIR -o '-p 5532' -w -t 60 start" > /dev/null
fi
su postgres -c "$PGBIN/pg_isready -p 5532"

go_rc=0
RELAY_AUTH_KEY=TEST V2_BOT_URL=http://localhost:3005 V2_BOT_TOKEN=xyzxyzxyz \\
  go test -v -count=1 ./... 2>&1 || go_rc=$?

jest_rc=0
if [ -f [[FRONTEND]]/package.json ]; then
  cd /home/[[REPO]]/[[FRONTEND]]
  REACT_APP_IS_TEST=true NODE_ENV=test NODE_OPTIONS="--max_old_space_size=8192" \\
    yarn jest --ci --verbose --silent --no-cache --coverage=false --maxWorkers=2 \\
    --transformIgnorePatterns "./frontend/app/svg/" 2>&1 || jest_rc=$?
fi

echo "go exit code: $go_rc, jest exit code: $jest_rc"
if [ "$go_rc" -ne 0 ] || [ "$jest_rc" -ne 0 ]; then
  exit 1
fi
"""

RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export HUSKY=0

cd /home/[[REPO]]
git reset --hard
git clean -fd

[[TEST_CMD]]
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export HUSKY=0

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

[[TEST_CMD]]
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export HUSKY=0

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

[[TEST_CMD]]
"""


def render_script(template: str, repo: str, sha: str = "") -> str:
    return (
        template.replace("[[TEST_CMD]]", TEST_CMD)
        .replace("[[FRONTEND]]", FRONTEND_DIR)
        .replace("[[REPO]]", repo)
        .replace("[[SHA]]", sha)
    )


class SphinxTribesImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE.format(
            base_image=GO_IMAGE,
            org=self.pr.org,
            repo=self.pr.repo,
            node_version=NODE_VERSION,
            yarn_version=YARN_VERSION,
        )


class SphinxTribesImageDefault(Image):
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
        return SphinxTribesImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", render_script(PREPARE_SH, repo, sha)),
            File(".", "run.sh", render_script(RUN_SH, repo)),
            File(".", "test-run.sh", render_script(TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", render_script(FIX_RUN_SH, repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        sections = ["FROM {name}:{tag}".format(name=name, tag=tag)]

        if self.global_env:
            sections.append(self.global_env)

        sections.append('ARG BASE_COMMIT="{sha}"'.format(sha=self.pr.base.sha))
        sections.append(copy_commands.strip())
        sections.append("RUN bash /home/prepare.sh")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(GIT_HARDENING.strip())

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("stakwork", "sphinx-tribes")
class SphinxTribes(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SphinxTribesImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log).replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        go_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        suite_re = re.compile(r"^(PASS|FAIL)\s+(src/\S+\.(?:js|jsx|ts|tsx))(?:\s|$)")
        jest_re = re.compile(
            r"^(\s*)([✓✔√✕✗×○])\s+(?:(?:skipped|todo)\s+)?(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        describe_re = re.compile(r"^(\s+)(\S.*)$")

        suite = ""
        describes: list[tuple[int, str]] = []

        for line in clean_log.splitlines():
            go_match = go_re.match(line)
            if go_match:
                status, name = go_match.groups()
                if status == "PASS":
                    passed_tests.add(name)
                elif status == "FAIL":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            suite_match = suite_re.match(line)
            if suite_match:
                suite = FRONTEND_DIR + "/" + suite_match.group(2)
                describes = []
                continue

            if not suite:
                continue

            if not line.strip():
                continue

            if not line.startswith(" "):
                suite = ""
                describes = []
                continue

            jest_match = jest_re.match(line)
            if jest_match:
                indent = len(jest_match.group(1))
                marker = jest_match.group(2)
                title = jest_match.group(3).strip()
                parents = [text for level, text in describes if level < indent]
                name = " > ".join([suite] + parents + [title])
                if marker in "✓✔√":
                    passed_tests.add(name)
                elif marker in "✕✗×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            describe_match = describe_re.match(line)
            if describe_match:
                indent = len(describe_match.group(1))
                text = describe_match.group(2).strip()
                if text.startswith("●"):
                    suite = ""
                    describes = []
                    continue
                describes = [(level, t) for level, t in describes if level < indent]
                describes.append((indent, text))

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
