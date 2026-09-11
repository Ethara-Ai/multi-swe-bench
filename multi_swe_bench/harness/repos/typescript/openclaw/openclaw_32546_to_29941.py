import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base-29941_to_32546"
_LO, _HI = 29941, 32546


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

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
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    DO_NOT_TRACK=1 \
    OPENCLAW_TELEMETRY_DISABLED=1 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NODE_OPTIONS=--max-old-space-size=4096

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
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
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
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
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "__BASE_SHA__"
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

installed=0
for attempt in 1 2 3; do
    if corepack pnpm install --frozen-lockfile --config.engine-strict=false; then
        installed=1
        break
    fi
    echo "prepare: pnpm install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1

node -e "require('./package.json'); console.log('DEPS_OK: package.json')"
test -x node_modules/.bin/vitest
corepack pnpm exec vitest --version > /dev/null
echo "DEPS_OK"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

echo "===== openclaw-lane: all ====="
corepack pnpm exec vitest run \
    --config vitest.config.ts \
    --reporter=verbose \
    --silent=passed-only \
    --retry=2 \
    || echo "===== openclaw-lane-failed: all (exit $?) ====="

exit 0
"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

echo "===== openclaw-lane: all ====="
corepack pnpm exec vitest run \
    --config vitest.config.ts \
    --reporter=verbose \
    --silent=passed-only \
    --retry=2 \
    || echo "===== openclaw-lane-failed: all (exit $?) ====="

exit 0
"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "fix-run: fix.patch conflicts; retrying without CHANGELOG.md" >&2
    if ! git apply --whitespace=nowarn --exclude=CHANGELOG.md /home/fix.patch; then
        echo "Error: git apply fix.patch failed" >&2
        exit 1
    fi
fi

echo "===== openclaw-lane: all ====="
corepack pnpm exec vitest run \
    --config vitest.config.ts \
    --reporter=verbose \
    --silent=passed-only \
    --retry=2 \
    || echo "===== openclaw-lane-failed: all (exit $?) ====="

exit 0
"""


class OpenclawBundleImageBase(Image):

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class OpenclawBundleImageDefault(Image):

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
        return OpenclawBundleImageBase(self.pr, self._config)

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
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_LANE_RE = re.compile(r"^=+\s*openclaw-lane:\s*(\S+)\s*=+$")
_TIMING = r"(?:\s+\d+(?:\.\d+)?(?:ms|s|m))?(?:\s*\(retry x\d+\))?"
_PASS_RE = re.compile(rf"^\s*[✓✔]\s+(.+?){_TIMING}\s*$")
_FAIL_RE = re.compile(rf"^\s*[×✕✗]\s+(.+?){_TIMING}\s*$")
_SKIP_RE = re.compile(rf"^\s*[↓○◌]\s+(.+?){_TIMING}\s*$")
_TEST_NAME_RE = re.compile(r"\.test\.[cm]?[jt]sx?\s+>\s+\S")


_NETWORK_TEST_FILES = frozenset({
    "extensions/msteams/src/attachments.test.ts",
    "src/agents/tools/web-fetch.cf-markdown.test.ts",
    "src/agents/tools/web-fetch.ssrf.test.ts",
    "src/agents/tools/web-tools.enabled-defaults.test.ts",
    "src/agents/tools/web-tools.fetch.test.ts",
    "src/browser/cdp.test.ts",
    "src/browser/pw-tools-core.snapshot.navigate-guard.test.ts",
    "src/memory/batch-voyage.test.ts",
    "src/memory/embeddings-voyage.test.ts",
    "src/memory/embeddings.test.ts",
    "src/memory/manager.batch.test.ts",
    "src/slack/monitor/media.test.ts",
    "src/telegram/bot.create-telegram-bot.test.ts",
    "src/telegram/bot.media.downloads-media-file-path-no-file-download.test.ts",
    "src/telegram/bot.media.stickers-and-fragments.test.ts",
    "src/web/auto-reply.web-auto-reply.compresses-common-formats-jpeg-cap.test.ts",
    "src/web/media.test.ts",
})

_NETWORK_TESTS = frozenset({
    "src/telegram/bot.test.ts::createTelegramBot::includes replied image media in inbound context for text replies",
})


def _is_network_conditional(name: str) -> bool:
    return name.split("::", 1)[0] in _NETWORK_TEST_FILES or name in _NETWORK_TESTS


def openclaw_bundle_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log)

    def normalise(name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip()
        return name.replace(" > ", "::")

    for raw_line in clean_log.splitlines():
        line = raw_line.rstrip()

        if _LANE_RE.match(line.strip()):
            continue

        match = _FAIL_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            failed_tests.add(normalise(match.group(1)))
            continue

        match = _PASS_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            passed_tests.add(normalise(match.group(1)))
            continue

        match = _SKIP_RE.match(line)
        if match and _TEST_NAME_RE.search(match.group(1)):
            skipped_tests.add(normalise(match.group(1)))

    failed_tests -= passed_tests
    skipped_tests -= passed_tests | failed_tests

    passed_tests = {t for t in passed_tests if not _is_network_conditional(t)}
    failed_tests = {t for t in failed_tests if not _is_network_conditional(t)}
    skipped_tests = {t for t in skipped_tests if not _is_network_conditional(t)}

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("openclaw", "openclaw_32546_to_29941")
class OPENCLAW_32546_TO_29941(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawBundleImageDefault(self.pr, self._config)

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
        return openclaw_bundle_parse_log(test_log)
