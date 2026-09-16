import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_DIR = "openclaw"

_NODE_BASE = "node:22-bookworm"

_PNPM_INSTALL = (
    "corepack pnpm install --frozen-lockfile --ignore-scripts=false"
    " --config.engine-strict=false --config.enable-pre-post-scripts=true"
)

_EXCLUDES = (
    "src/cron/isolated-agent/run.message-tool-policy.test.ts",
    "src/gateway/server.chat.gateway-server-chat.test.ts",
    "src/gateway/credential-precedence.parity.test.ts",
    "src/gateway/probe.auth.integration.test.ts",
    "src/gateway/server-methods/send.test.ts",
    "src/gateway/server-runtime-config.test.ts",
    "ui/src/ui/focus-mode.browser.test.ts",
    "src/telegram/bot.test.ts",
    "extensions/signal/src/monitor.tool-result.sends-tool-summaries-responseprefix.test.ts",
)

_PINNED_HOSTS = (
    ("example.com", "172.66.147.243"),
    ("api.voyageai.com", "136.110.181.169"),
    ("chat.googleapis.com", "172.217.114.4"),
    ("generativelanguage.googleapis.com", "172.217.112.4"),
    ("auth.openai.com", "104.18.41.241"),
    ("api.openai.com", "162.159.140.245"),
    ("api.search.brave.com", "15.197.138.111"),
    ("api.perplexity.ai", "104.18.27.48"),
    ("graph.microsoft.com", "20.190.175.152"),
    ("openrouter.ai", "104.18.2.115"),
    ("api.moonshot.ai", "104.18.29.136"),
    ("api.mistral.ai", "172.66.2.203"),
    ("api.x.ai", "104.18.18.80"),
    ("openclaw.ai", "216.150.1.1"),
)

_MAIN_LIMIT = "240m"

_UI_LIMIT = "45m"


class OpenclawImageBase(Image):

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
        return _NODE_BASE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_BASE}

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

ENV DO_NOT_TRACK=1
ENV OPENCLAW_TELEMETRY_DISABLED=1
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git python3 make g++ ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable

RUN git config --global http.version HTTP/1.1 && \\
    git config --global http.lowSpeedLimit 1000 && \\
    git config --global http.lowSpeedTime 300 && \\
    git config --global http.postBuffer 524288000

RUN for attempt in 1 2 3 4 5 6; do \\
        getent hosts github.com > /dev/null 2>&1 || \\
            echo "20.207.73.82 github.com" >> /etc/hosts; \\
        rm -rf /home/{_REPO_DIR}; \\
        if git clone "${{REPO_URL}}" /home/{_REPO_DIR}; then \\
            break; \\
        fi; \\
        echo "clone attempt $attempt failed; retrying in 60s" >&2; \\
        sleep 60; \\
    done; \\
    test -d /home/{_REPO_DIR}/.git

CMD ["/bin/bash"]
"""


class OpenclawImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return OpenclawImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = _REPO_DIR
        sha = self.pr.base.sha

        e2e_files = sorted(
            set(
                re.findall(
                    r"^\+\+\+ b/(\S+\.e2e\.test\.[cm]?[jt]sx?)$",
                    self.pr.test_patch or "",
                    re.M,
                )
            )
        )
        if e2e_files:
            e2e_args = " ".join(f"'{f}'" for f in e2e_files)
            e2e_lane = (
                "\n"
                "if [ -f vitest.e2e.config.ts ]; then\n"
                f"    run_lane e2e vitest_run --config vitest.e2e.config.ts {e2e_args}\n"
                "fi\n"
            )
        else:
            e2e_lane = ""

        exclude_paths = " ".join(f"'{p}'" for p in _EXCLUDES)
        host_pins = "\n".join(f"pin_host {h} {ip}" for h, ip in _PINNED_HOSTS)

        suite = """
export CI=true
cd /home/{repo}

pin_host() {{
    ip=$(timeout 5 getent ahostsv4 "$1" | awk 'NR==1 {{print $1}}') || ip=""
    echo "${{ip:-$2}} $1" >> /etc/hosts
}}
{host_pins}

if ! git diff --quiet -- pnpm-lock.yaml ':(glob)**/package.json'; then
    echo "===== openclaw: patches changed a manifest; re-installing ====="
    reinstall_ok=0
    for attempt in 1 2 3; do
        if {install}; then
            reinstall_ok=1
            break
        fi
        echo "reinstall attempt $attempt failed; retrying in 30s" >&2
        sleep 30
    done
    test "$reinstall_ok" = "1"
fi

rm -f {excludes}

main_extra=""
if grep -q loadChannelConfigSurfaceModuleSync src/plugins/bundled-plugin-metadata.ts 2>/dev/null; then
    main_extra="--no-isolate"
    if [ -f extensions/tlon/src/config-schema.ts ] && [ -f src/plugin-sdk/channel-config-schema.ts ]; then
        sed -i 's#"openclaw/plugin-sdk/core"#"openclaw/plugin-sdk/channel-config-schema"#' \\
            extensions/tlon/src/config-schema.ts
    fi
fi

vitest_run() {{
    timeout -k 60 {main_limit} corepack pnpm exec vitest run --reporter=verbose --silent=passed-only "$@"
}}

run_lane() {{
    label="$1"
    shift
    echo "===== openclaw-lane: ${{label}} ====="
    "$@" || echo "===== openclaw-lane-failed: ${{label}} (exit $?) ====="
}}

if [ -f test/vitest/vitest.config.ts ]; then
    for cfg in $(grep -oE '"test/vitest/vitest\\.[a-z0-9-]+\\.config\\.ts"' test/vitest/vitest.config.ts | tr -d '"'); do
        run_lane "main:$(basename "$cfg" .config.ts)" vitest_run --config "$cfg"
    done
else
    run_lane main vitest_run --config vitest.config.ts $main_extra
fi

if [ -d ui ] && [ -f ui/vitest.config.ts ]; then
    run_lane ui timeout -k 60 {ui_limit} corepack pnpm --dir ui exec vitest run \\
        --config vitest.config.ts --reporter=verbose --silent=passed-only
fi

if [ -d ui ] && [ -f ui/vitest.node.config.ts ]; then
    run_lane ui-node timeout -k 60 {ui_limit} corepack pnpm --dir ui exec vitest run \\
        --config vitest.node.config.ts --reporter=verbose --silent=passed-only
fi
{e2e_lane}
exit 0
""".format(
            repo=repo,
            install=_PNPM_INSTALL,
            excludes=exclude_paths,
            host_pins=host_pins,
            main_limit=_MAIN_LIMIT,
            ui_limit=_UI_LIMIT,
            e2e_lane=e2e_lane,
        )

        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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
set -eo pipefail
export CI=true

cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
echo "prepare: HEAD pinned at {sha}"

corepack enable
node --version
corepack pnpm --version

corepack pnpm config set fetch-retries 6
corepack pnpm config set fetch-retry-mintimeout 20000
corepack pnpm config set fetch-retry-maxtimeout 240000
corepack pnpm config set network-concurrency 8

cd /home/{repo}
install_ok=0
for attempt in 1 2 3; do
    if {install}; then
        install_ok=1
        break
    fi
    echo "prepare: pnpm install attempt $attempt failed; retrying in 30s" >&2
    sleep 30
done
test "$install_ok" = "1"

if grep -qE '^\\+\\+\\+ b/(pnpm-lock\\.yaml|(.*/)?package\\.json)$' /home/test.patch /home/fix.patch; then
    rm -rf /tmp/prefetch
    git worktree add --detach /tmp/prefetch HEAD
    cd /tmp/prefetch
    git apply --whitespace=nowarn /home/test.patch
    git apply --whitespace=nowarn /home/fix.patch
    prefetch_ok=0
    for attempt in 1 2 3; do
        if {install}; then
            prefetch_ok=1
            break
        fi
        echo "prepare: prefetch attempt $attempt failed; retrying in 30s" >&2
        sleep 30
    done
    cd /home/{repo}
    git worktree remove --force /tmp/prefetch
    git worktree prune
    echo "prepare: patched-manifest prefetch ok=$prefetch_ok"
fi

cd /home/{repo}
corepack pnpm canvas:a2ui:bundle || \\
    echo "prepare: a2ui bundle failed; the suites stub it themselves" >&2

if [ -f /home/{repo}/ui/vitest.config.ts ]; then
    cd /home/{repo}
    pw_ok=0
    for attempt in 1 2 3; do
        if corepack pnpm --dir ui exec playwright install --with-deps chromium; then
            pw_ok=1
            break
        fi
        echo "prepare: playwright install attempt $attempt failed; retrying in 30s" >&2
        sleep 30
    done
    test "$pw_ok" = "1"
fi

cd /home/{repo}
test -d node_modules
corepack pnpm exec vitest --version
if [ -f ui/vitest.config.ts ]; then
    test -n "$(ls -d /root/.cache/ms-playwright/chromium* 2>/dev/null)"
    echo "prepare: playwright chromium present"
fi
echo "prepare: DEPS_OK"
""".format(repo=repo, sha=sha, install=_PNPM_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
""".format(repo=repo)
                + suite,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
""".format(repo=repo)
                + suite,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply of fix.patch failed" >&2
    exit 1
fi
""".format(repo=repo)
                + suite,
            ),
        ]

    def dockerfile(self) -> str:
        repo = _REPO_DIR
        sha = self.pr.base.sha

        return f"""FROM mswebench/{self.pr.org}_m_{self.pr.repo}:base

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

COPY check_git_changes.sh /home/
RUN bash /home/check_git_changes.sh

WORKDIR /home/

COPY fix.patch /home/
COPY test.patch /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
"""


@Instance.register("openclaw", "openclaw_41536_to_40543")
@Instance.register("openclaw", "openclaw")
class OPENCLAW_41536_TO_40543(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_lane = re.compile(r"^=+\s*openclaw-lane:\s*(\S+)\s*=+$")

        timing = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s|m))?"
        re_pass = re.compile(rf"^\s*[✓✔]\s+(.+?){timing}\s*$")
        re_fail = re.compile(rf"^\s*[×✕✗]\s+(.+?){timing}\s*$")
        re_skip = re.compile(rf"^\s*[↓○◌]\s+(.+?){timing}\s*$")

        re_test_name = re.compile(r"\.test\.[cm]?[jt]sx?\s+>\s+\S")

        lane = ""

        def qualify(name: str) -> str:
            name = re.sub(r"\s+", " ", name).strip()
            return f"{lane} > {name}" if lane in ("ui", "ui-node") else name

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()

            banner = re_lane.match(line.strip())
            if banner:
                lane = banner.group(1)
                continue

            m = re_fail.match(line)
            if m and re_test_name.search(m.group(1)):
                failed_tests.add(qualify(m.group(1)))
                continue

            m = re_pass.match(line)
            if m and re_test_name.search(m.group(1)):
                passed_tests.add(qualify(m.group(1)))
                continue

            m = re_skip.match(line)
            if m and re_test_name.search(m.group(1)):
                skipped_tests.add(qualify(m.group(1)))

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
