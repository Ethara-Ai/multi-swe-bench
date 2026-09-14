import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PKGROOT_MARKER = "##PKGROOT "

_DOC_ONLY_PATHS = re.compile(r"^(CHANGELOG\.md$|docs/|\.secrets\.baseline$)")


def _strip_doc_paths(patch: str) -> str:
    out: list[str] = []
    keep = True
    for line in patch.splitlines(True):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            keep = not (m and _DOC_ONLY_PATHS.match(m.group(2)))
        if keep:
            out.append(line)
    return "".join(out)

_TEST_BODY = r"""
set +e
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '\.(test|spec)\.[cm]?[jt]sx?$' \
    | grep -vE '\.(live|e2e)\.test\.[cm]?[jt]sx?$' \
    | sort -u)
set -e

ROOT_TARGETS=""
PKG_TARGETS=""
for f in $TEST_FILES; do
    [ -f "$f" ] || continue
    case "$f" in
        src/*|extensions/*|test/*) ROOT_TARGETS="$ROOT_TARGETS $f" ;;
        *)                         PKG_TARGETS="$PKG_TARGETS $f" ;;
    esac
done

if [ -z "$ROOT_TARGETS" ] && [ -z "$PKG_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

VITEST_ARGS="run --reporter=verbose --no-file-parallelism --testTimeout=120000 --hookTimeout=180000"

set +e

if [ -n "$ROOT_TARGETS" ]; then
    echo "${PKGROOT_MARKER}."
    echo "running vitest on:$ROOT_TARGETS"
    pnpm exec vitest $VITEST_ARGS $ROOT_TARGETS 2>&1
fi

for f in $PKG_TARGETS; do
    pkg="$(dirname "$f")"
    while [ "$pkg" != "." ] && [ ! -f "$pkg/package.json" ]; do pkg="$(dirname "$pkg")"; done
    if [ "$pkg" = "." ]; then
        echo "skipping $f: no owning package.json"
        continue
    fi
    rel="${f#$pkg/}"
    cat > "$pkg/vitest.msb.config.mts" <<EOF
import { defineConfig } from "vitest/config";
export default defineConfig({
  test: { include: ["$rel"], setupFiles: [], pool: "forks" },
});
EOF
    echo "${PKGROOT_MARKER}$pkg"
    echo "running vitest in $pkg on: $rel"
    ( cd "$pkg" && pnpm exec vitest $VITEST_ARGS --config vitest.msb.config.mts 2>&1 )
done
"""


class OpenclawEraImageBase(Image):

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
        return "node:22"

    def image_tag(self) -> str:
        return "base-40409_to_39906"

    def workdir(self) -> str:
        return "base-40409_to_39906"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
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
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    COREPACK_ENABLE_STRICT=0 \\
    PNPM_HOME=/usr/local/share/pnpm \\
    PATH=/usr/local/share/pnpm:$PATH \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    ADBLOCK=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    curl \\
    python3 \\
    make \\
    g++ \\
    pkg-config \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p ${{PNPM_HOME}} && corepack enable

WORKDIR /home/

{code}

CMD ["/bin/bash"]
"""


class OpenclawEraImageDefault(Image):
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
        return OpenclawEraImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        prepare = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git remote add origin https://github.com/[[ORG]]/[[REPO]].git 2>/dev/null || true
git fetch --no-tags --depth=1 origin [[SHA]] 2>/dev/null || git fetch --no-tags origin 2>/dev/null || true
git checkout --detach [[SHA]]
test "$(git rev-parse HEAD)" = "$(git rev-parse [[SHA]])"
bash /home/check_git_changes.sh

PM=$(node -p "String(require('./package.json').packageManager || 'pnpm@10.23.0')")
echo "packageManager from checkout: $PM"
corepack enable
corepack prepare "$PM" --activate

CI=true pnpm install --frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true \
    || CI=true pnpm install --frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true \
    || CI=true pnpm install --no-frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true

test -x node_modules/.bin/vitest
echo "DEPS_OK node $(node --version) pnpm $(pnpm --version) vitest $(pnpm exec vitest --version)"
"""

        return [
            File(".", "fix.patch", _strip_doc_paths(self.pr.fix_patch)),
            File(".", "test.patch", self.pr.test_patch),
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
                prepare.replace("[[REPO]]", repo)
                .replace("[[ORG]]", org)
                .replace("[[SHA]]", sha),
            ),
            File(
                ".",
                "run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true
PKGROOT_MARKER="[[MARKER]]"

cd /home/[[REPO]]
"""
                 + _TEST_BODY)
                .replace("[[REPO]]", repo)
                .replace("[[MARKER]]", _PKGROOT_MARKER),
            ),
            File(
                ".",
                "test-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true
PKGROOT_MARKER="[[MARKER]]"

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
"""
                 + _TEST_BODY)
                .replace("[[REPO]]", repo)
                .replace("[[MARKER]]", _PKGROOT_MARKER),
            ),
            File(
                ".",
                "fix-run.sh",
                ("""#!/bin/bash
set -uo pipefail
export CI=true
PKGROOT_MARKER="[[MARKER]]"

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "=== PATCH FAILURE: fix.patch did not apply ==="
    exit 1
fi
"""
                 + _TEST_BODY)
                .replace("[[REPO]]", repo)
                .replace("[[MARKER]]", _PKGROOT_MARKER),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("openclaw", "openclaw_40409_to_39906")
class Openclaw40409To39906(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawEraImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]|\x00", "", test_log)

        spec_re = r"\S+\.(?:test|spec)\.[cm]?[jt]sx?"
        trailing_re = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?(?:\s*\(retry\s+x\d+\))?"
        case_re = re.compile(
            rf"^\s*(?P<marker>[✓✔√×✕✖✗✘↓○])\s+"
            rf"(?P<name>{spec_re}\s+>\s+.*?)"
            rf"{trailing_re}\s*$"
        )
        fail_case_re = re.compile(rf"^\s*FAIL\s+(?P<name>{spec_re}\s+>\s+.+?)\s*$")

        pass_markers = {"✓", "✔", "√"}
        fail_markers = {"×", "✕", "✖", "✗", "✘"}
        skip_markers = {"↓", "○"}

        prefix = ""

        for line in clean_log.split("\n"):
            stripped = line.strip()
            if stripped.startswith(_PKGROOT_MARKER):
                root = stripped[len(_PKGROOT_MARKER):].strip()
                prefix = "" if root in ("", ".") else root.rstrip("/")
                continue

            m = case_re.match(line)
            if m:
                name = m.group("name").strip()
                if prefix:
                    name = f"{prefix}/{name}"
                marker = m.group("marker")
                if marker in pass_markers:
                    passed_tests.add(name)
                elif marker in fail_markers:
                    failed_tests.add(name)
                elif marker in skip_markers:
                    skipped_tests.add(name)
                continue

            m = fail_case_re.match(line)
            if m:
                name = m.group("name").strip()
                if prefix:
                    name = f"{prefix}/{name}"
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
