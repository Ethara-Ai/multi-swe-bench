import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_LO = 51490
_HI = 52516
_ERA = "52516_to_51490"
_INTERVAL_NAME = f"openclaw_{_ERA}"

_DOC_ONLY_PATHS = re.compile(r"^(CHANGELOG\.md$|docs/|\.secrets\.baseline$)")

_JSON_FENCE_RE = re.compile(
    r"-----BEGIN_VITEST_JSON lane=(\S+)-----\n(.*?)\n-----END_VITEST_JSON-----",
    re.DOTALL,
)


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


_CHECK_GIT_CHANGES = """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_APPLY_PATCH = """#!/bin/bash
set -uo pipefail

patch_file="$1"
work="$(mktemp -d)"
status=0

csplit -s -z -f "$work/part." -b "%03d" "$patch_file" '/^diff --git /' '{*}'
if ! ls "$work"/part.* > /dev/null 2>&1; then
    echo "apply-patch: no file sections found in $patch_file"
    rm -rf "$work"
    exit 1
fi

for part in "$work"/part.*; do
    target="$(sed -n 's|^+++ b/||p' "$part" | head -n 1)"
    if [ -z "$target" ]; then
        target="$(sed -n 's|^diff --git a/\\([^ ]*\\).*|\\1|p' "$part" | head -n 1)"
    fi

    if git apply --whitespace=nowarn "$part"; then
        echo "apply-patch: applied $target"
        continue
    fi

    if git apply --reverse --check "$part" > /dev/null 2>&1; then
        echo "apply-patch: already present in the tree, skipping $target"
        continue
    fi

    echo "apply-patch: FAILED $target"
    git apply --whitespace=nowarn --verbose "$part"
    status=1
done

rm -rf "$work"
exit "$status"
"""

_TEST_BODY = r"""
TEST_FILES=$(grep -E '^\+\+\+ b/' /home/test.patch \
    | sed -e 's|^+++ b/||' -e 's|[[:space:]].*$||' \
    | grep -E '\.(test|spec)\.[cm]?[jt]sx?$' \
    | grep -vE '\.(live|e2e)\.test\.[cm]?[jt]sx?$' \
    | sort -u)

ROOT_TARGETS=""
EXTRA_TARGETS=""
for f in $TEST_FILES; do
    if [ ! -f "$f" ]; then
        continue
    fi
    case "$f" in
        src/*|extensions/*|test/*) ROOT_TARGETS="$ROOT_TARGETS $f" ;;
        *)                         EXTRA_TARGETS="$EXTRA_TARGETS $f" ;;
    esac
done

if [ -z "$ROOT_TARGETS" ] && [ -z "$EXTRA_TARGETS" ]; then
    echo "no test file from the test patch is present in this tree"
    exit 0
fi

VITEST_ARGS="run --reporter=verbose --reporter=json --no-file-parallelism --testTimeout=120000 --hookTimeout=180000"

if [ -n "$ROOT_TARGETS" ]; then
    ROOT_OUT=/home/vitest-root.json
    rm -f "$ROOT_OUT"
    echo "running vitest (lane=root) on:$ROOT_TARGETS"
    set +e
    pnpm exec vitest $VITEST_ARGS --config vitest.config.ts --outputFile="$ROOT_OUT" $ROOT_TARGETS
    set -e
    echo "-----BEGIN_VITEST_JSON lane=root-----"
    if [ -f "$ROOT_OUT" ]; then
        cat "$ROOT_OUT"
    fi
    echo
    echo "-----END_VITEST_JSON-----"
fi

if [ -n "$EXTRA_TARGETS" ]; then
    EXTRA_OUT=/home/vitest-extra.json
    rm -f "$EXTRA_OUT"
    INCLUDE_LIST=""
    for f in $EXTRA_TARGETS; do
        INCLUDE_LIST="$INCLUDE_LIST\"$f\","
    done
    cat > vitest.msb.config.mts <<EOF
import baseConfig from "./vitest.config.ts";
const base = baseConfig as unknown as Record<string, unknown>;
const baseTest = (baseConfig as { test?: Record<string, unknown> }).test ?? {};
export default {
  ...base,
  test: { ...baseTest, include: [$INCLUDE_LIST] },
};
EOF
    echo "running vitest (lane=extra) on:$EXTRA_TARGETS"
    set +e
    pnpm exec vitest $VITEST_ARGS --config vitest.msb.config.mts --outputFile="$EXTRA_OUT" $EXTRA_TARGETS
    set -e
    echo "-----BEGIN_VITEST_JSON lane=extra-----"
    if [ -f "$EXTRA_OUT" ]; then
        cat "$EXTRA_OUT"
    fi
    echo
    echo "-----END_VITEST_JSON-----"
fi
"""


class Openclaw52516To51490ImageBase(Image):
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
        return f"base-{_ERA}"

    def workdir(self) -> str:
        return f"base-{_ERA}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    TZ=UTC \\
    CI=true \\
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
    NODE_OPTIONS=--max-old-space-size=4096 \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    DO_NOT_TRACK=1 \\
    OPENCLAW_TELEMETRY_DISABLED=1 \\
    ADBLOCK=1

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

RUN git config --global http.version HTTP/1.1 \\
 && git config --global http.postBuffer 524288000 \\
 && git config --global advice.detachedHead false

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Openclaw52516To51490ImageDefault(Image):
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
        return Openclaw52516To51490ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = r"""
set -eo pipefail

REPO_DIR=/home/[[REPO]]
BASE_COMMIT=[[SHA]]

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"

if [ ! -d .git ]; then
    git init -q .
else
    git reset --hard
    git clean -fd
fi

git config --local advice.detachedHead false
git config --local gc.auto 0

if git rev-parse --verify --quiet "${BASE_COMMIT}^{commit}" > /dev/null; then
    echo "prepare: base commit is already in the object store"
else
    if ! git remote get-url origin > /dev/null 2>&1; then
        git remote add origin https://github.com/[[ORG]]/[[REPO]].git
    fi
    FETCHED=0
    for attempt in 1 2 3 4 5; do
        if git fetch --no-tags --depth=1 origin "$BASE_COMMIT"; then
            FETCHED=1
            break
        fi
        echo "prepare: fetch attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done
    test "$FETCHED" = "1"
fi

git checkout --detach "$BASE_COMMIT"
test "$(git rev-parse HEAD)" = "$BASE_COMMIT"
git reset --hard
bash /home/check_git_changes.sh
git clean -fd
bash /home/check_git_changes.sh

PM=$(node -p "String(require('./package.json').packageManager || 'pnpm@10.32.1')")
echo "prepare: packageManager from the checkout is $PM"
corepack enable
corepack prepare "$PM" --activate

export CI=true

pnpm install --frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true \
    || pnpm install --frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true \
    || pnpm install --no-frozen-lockfile --ignore-scripts=false \
        --config.engine-strict=false --config.enable-pre-post-scripts=true

test -x node_modules/.bin/vitest
pnpm exec vitest list --config vitest.config.ts --filesOnly > /home/vitest-files.txt
test -s /home/vitest-files.txt
echo "DEPS_OK node $(node --version) pnpm $(pnpm --version) vitest $(pnpm exec vitest --version)"
"""

        run_header = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
"""

        test_header = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
if ! bash /home/apply-patch.sh /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
"""

        fix_header = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
if ! bash /home/apply-patch.sh /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
if ! bash /home/apply-patch.sh /home/fix.patch; then
    echo "=== PATCH FAILURE: fix.patch did not apply ==="
    exit 1
fi
"""

        return [
            File(".", "fix.patch", _strip_doc_paths(self.pr.fix_patch)),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "apply-patch.sh", _APPLY_PATCH),
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
                (run_header + _TEST_BODY).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "test-run.sh",
                (test_header + _TEST_BODY).replace("[[REPO]]", repo),
            ),
            File(
                ".",
                "fix-run.sh",
                (fix_header + _TEST_BODY).replace("[[REPO]]", repo),
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
WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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

RUN bash /home/prepare.sh
"""


def _parse_vitest_log(test_log: str, repo: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]|\x00", "", test_log)
    repo_prefix = f"/home/{repo}/"

    for match in _JSON_FENCE_RE.finditer(clean_log):
        lane = match.group(1)
        try:
            report = json.loads(match.group(2))
        except ValueError:
            continue

        for suite in report.get("testResults") or []:
            path = suite.get("name") or ""
            if repo_prefix in path:
                path = path.split(repo_prefix, 1)[1]

            assertions = suite.get("assertionResults") or []
            if not assertions:
                ident = f"vitest::{lane}::{path}::<suite failed to load>"
                if suite.get("status") == "passed":
                    passed_tests.add(ident)
                else:
                    failed_tests.add(ident)
                continue

            seen: dict[str, int] = {}
            for assertion in assertions:
                name = assertion.get("fullName") or assertion.get("title") or ""
                name = " ".join(name.split())
                seen[name] = seen.get(name, 0) + 1
                suffix = "" if seen[name] == 1 else f"#{seen[name]}"
                ident = f"vitest::{lane}::{path}::{name}{suffix}"

                status = assertion.get("status")
                if status == "passed":
                    passed_tests.add(ident)
                elif status == "failed":
                    failed_tests.add(ident)
                else:
                    skipped_tests.add(ident)

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


@Instance.register("openclaw", _INTERVAL_NAME)
class OPENCLAW_52516_TO_51490(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Openclaw52516To51490ImageDefault(self.pr, self._config)

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
        return _parse_vitest_log(test_log, self.pr.repo)


_INCUMBENT = Instance._registry.get("openclaw/openclaw")


@Instance.register("openclaw", "openclaw")
class OPENCLAW_52516_TO_51490_DISPATCH(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return OPENCLAW_52516_TO_51490(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"openclaw/openclaw#{pr.number} is outside {_LO}-{_HI} and no other "
            f"openclaw adapter is registered under the bare name"
        )


for _number in range(_LO, _HI + 1):
    Instance._registry.setdefault(f"openclaw/{_number}", OPENCLAW_52516_TO_51490)
del _number
