import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# PalisadoesFoundation/talawa-api -- interval 1785_to_1256 (vitest suite)
#
# Two-stage layout:
#   * TALAWA_API_1785_TO_1256_ImageBase
#       -> mswebench/palisadoesfoundation_m_talawa-api:base-1785_to_1256
#     ubuntu:22.04 + official Node tarball (picked by TARGETARCH, so amd64 and
#     arm64 both work) + MongoDB 6.0 server/mongosh + redis-server, full clone,
#     NO checkout/hardening (shared by every PR in this interval).
#   * TALAWA_API_1785_TO_1256_ImageDefault
#       -> mswebench/palisadoesfoundation_m_talawa-api:pr-<N>
#     checks out base.sha (detached), installs deps from package-lock.json
#     (npm ci), then applies the anti-reward-hack hardening.
#
# The vitest globalSetup connects to MongoDB (MONGO_DB_URL) and newer bases
# also need Redis, so test.sh starts both local services before `npm test`.
# Some bases list "@esbuild/win32-x64" as a direct dependency, so npm ci runs
# with --force (skips the EBADPLATFORM check) and --ignore-scripts (husky).
# =============================================================================

NODE_VERSION = "20.10.0"
MONGODB_VERSION = "6.0"
NPM_CI_FLAGS = (
    "--force --ignore-scripts --no-audit --no-fund"
    " --fetch-retries=6 --fetch-retry-factor=2"
    " --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=180000"
    " --fetch-timeout=600000 --maxsockets=8"
)


class TALAWA_API_1785_TO_1256_ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-1785_to_1256"

    def workdir(self) -> str:
        return "base-1785_to_1256"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
WORKDIR /home/
ENV LC_ALL=C.UTF-8 \\
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    curl \\
    git \\
    gnupg \\
    procps \\
    python3 \\
    redis-server \\
    xz-utils \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    curl -fsSL "https://pgp.mongodb.com/server-{MONGODB_VERSION}.asc" \\
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-{MONGODB_VERSION}.gpg; \\
    echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-{MONGODB_VERSION}.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/{MONGODB_VERSION} multiverse" \\
        > /etc/apt/sources.list.d/mongodb-org-{MONGODB_VERSION}.list; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends mongodb-org-server mongodb-mongosh; \\
    apt-get clean; rm -rf /var/lib/apt/lists/*; \\
    mkdir -p /data/db; \\
    mongod --version; mongosh --version; \\
    command -v redis-server; command -v redis-cli; dpkg -s redis-server > /dev/null

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" \\
        | tar -xJ -C /usr/local --strip-components=1 --no-same-owner; \\
    node --version; npm --version

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class TALAWA_API_1785_TO_1256_ImageDefault(Image):
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
        return TALAWA_API_1785_TO_1256_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = (
            "cd /home/{repo}\n"
            "export CI=true\n"
            "# test-only values (no real credentials); dotenv never overrides these\n"
            "export MONGO_DB_URL=mongodb://localhost:27017/talawa-test-db\n"
            "export REDIS_HOST=localhost\n"
            "export REDIS_PORT=6379\n"
            "export ACCESS_TOKEN_SECRET=talawa-test-access-token-secret\n"
            "export REFRESH_TOKEN_SECRET=talawa-test-refresh-token-secret\n"
            "export LAST_RESORT_SUPERADMIN_EMAIL=abc@gmail.com\n"
            "export COLORIZE_LOGS=false\n"
            "export LOG_LEVEL=info\n"
            "\n"
            "# fix patches may bump locked deps; reinstall only when the lockfile changed\n"
            "if ! git diff --quiet HEAD -- package-lock.json; then\n"
            "  for attempt in 1 2 3 4 5; do\n"
            "    if npm ci {npm_ci_flags}; then break; fi\n"
            "    if [ \"$attempt\" = 5 ]; then exit 1; fi\n"
            "    rm -rf node_modules\n"
            "    sleep $((attempt * 15))\n"
            "  done\n"
            "fi\n"
            "\n"
            "# local services used by the vitest globalSetup / redis cache\n"
            "if ! pgrep -x mongod > /dev/null; then\n"
            "  mkdir -p /data/db\n"
            "  mongod --fork --bind_ip 127.0.0.1 --dbpath /data/db --logpath /var/log/mongod.log\n"
            "fi\n"
            "if ! pgrep -x redis-server > /dev/null; then\n"
            "  redis-server --daemonize yes --bind 127.0.0.1 --port 6379\n"
            "fi\n"
            "for _ in $(seq 1 60); do\n"
            "  if mongosh --quiet --eval 'db.adminCommand({{ping: 1}}).ok' > /dev/null 2>&1; then break; fi\n"
            "  sleep 1\n"
            "done\n"
            "mongosh --quiet --eval 'db.adminCommand({{ping: 1}}).ok'\n"
            "redis-cli -h 127.0.0.1 -p 6379 ping\n"
            "\n"
            "npm test -- --reporter=verbose\n"
        ).format(repo=repo, npm_ci_flags=NPM_CI_FLAGS)
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
                f"""#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

for attempt in 1 2 3 4 5; do
  if npm ci {NPM_CI_FLAGS}; then
    break
  fi
  if [ "$attempt" = 5 ]; then
    echo "npm ci failed after $attempt attempts"
    exit 1
  fi
  echo "npm ci attempt $attempt failed, retrying"
  rm -rf node_modules
  sleep $((attempt * 15))
done

# hard gate: vitest must resolve from the installed deps
node -e "require.resolve('vitest/package.json')"
./node_modules/.bin/vitest --version
""",
            ),
            File(
                ".",
                "test.sh",
                f"""#!/bin/bash
set -eo pipefail

{test_cmd}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
bash /home/test.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/test.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/test.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT={self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("PalisadoesFoundation", "talawa_api_1785_to_1256")
class TALAWA_API_1785_TO_1256(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TALAWA_API_1785_TO_1256_ImageDefault(self.pr, self._config)

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
        # vitest --reporter=verbose (non-TTY) prints one line per finished test:
        #    ✓ tests/a.spec.ts > describe > title 1234ms 80 MB heap used
        #    × tests/a.spec.ts > describe > title
        #      → error message
        #    ↓ tests/a.spec.ts > describe > skipped title
        # and the failure summary repeats failed ids / unloadable files as
        #    FAIL  tests/a.spec.ts > describe > title
        #    FAIL  tests/b.spec.ts [ tests/b.spec.ts ]
        # Test id = "<file> > <describe chain> > <title>" with timing/heap
        # suffixes stripped, so ids are identical across run / test / fix.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_re = re.compile(r"^\s*(\u2713|\u2714|\u00d7|\u2715|\u2716|\u2193)\s+(\S+\.(?:ts|js|mts|tsx) > .+?)\s*$")
        fail_re = re.compile(r"^\s*FAIL\s+(\S+\.(?:ts|js|mts|tsx)(?: > .+?)?)\s*(?:\[ .+ \])?\s*$")
        suffix_re = re.compile(r"(?:\s+\d+(?:\.\d+)?\s*ms)?(?:\s+\d+\s+MB heap used)?$")

        for raw in log.splitlines():
            line = raw.rstrip()
            m = test_re.match(line)
            if m:
                mark = m.group(1)
                name = suffix_re.sub("", m.group(2)).strip()
                if mark in ("\u2713", "\u2714"):
                    passed_tests.add(name)
                elif mark == "\u2193":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue
            m = fail_re.match(line)
            if m:
                failed_tests.add(suffix_re.sub("", m.group(1)).strip())

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
