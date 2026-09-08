"""payloadcms/payload harness for the Payload 2.x era (PRs #3833 - #4113).

Era evidence (collected from the repo at the earliest and latest ``base.sha``
in the dataset, ``fa550740`` and ``56db87d2``)::

    .nvmrc / .node-version   v18.17.1              (identical at both ends)
    package.json engines     node >=14, pnpm >=8   (identical at both ends)
    lockfile                 pnpm-lock.yaml        (pnpm workspace + turbo)
    jest.config.js           verbose: true,
                             testMatch packages/payload/src/**/*.spec.ts
                                       test/**/*int.spec.ts
    playwright.config.ts     testDir: 'test', testMatch: '*e2e.spec.ts'
    db                       mongodb-memory-server, selected by NODE_ENV=test
                             in packages/db-mongodb/src/connect.ts

The toolchain does not move anywhere inside the range, so this is a single
self-contained era file.

Two structural notes that differ from the older payload era files in this
directory, both deliberate:

1. The repository is cloned in ``ImageDefault`` rather than in ``ImageBase``.
   ``DockerfileEnhancer`` only rewrites base-image Dockerfiles, and its
   standardized fetch ends with ``Image._HARDENING_BLOCK``, which deletes every
   ref and runs ``git gc --prune=now``.  A base image is shared by every PR of
   the era (one ``image_tag()``), so it can only ever be hardened at a single
   ``BASE_COMMIT`` - every other PR's ``base.sha`` would already be pruned by
   the time ``prepare.sh`` tried to check it out.  Cloning per PR image keeps
   each ``base.sha`` reachable and still applies the hardening block.

2. Tests are selected from the patch bodies rather than running the whole
   monorepo suite.  ``select-tests.sh`` derives the selection from
   ``/home/test.patch`` + ``/home/fix.patch``, which are baked into the image,
   so all three stages select exactly the same tests and therefore produce
   directly comparable test names.
"""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "payload_3833_to_4113"

# ---------------------------------------------------------------------------
# git apply exclusions
#
# PR #4039's test.patch adds test/uploads/test-image.jpg as an abbreviated
# "Binary files ... differ" stub with no payload, which makes git apply reject
# the *entire* patch ("cannot apply binary patch ... without full index line").
# Verified against a real clone: with these exclusions all 10 test.patch and
# all 10 fix.patch bodies in the dataset apply cleanly at their base.sha.
# ---------------------------------------------------------------------------
_GIT_APPLY_EXCLUDES = (
    "--exclude=*.jpg --exclude=*.jpeg --exclude=*.png --exclude=*.gif "
    "--exclude=*.webp --exclude=*.ico --exclude=*.bmp --exclude=*.mp4 "
    "--exclude=*.webm --exclude=*.woff --exclude=*.woff2 --exclude=*.ttf "
    "--exclude=*.eot --exclude=*.pdf --exclude=pnpm-lock.yaml"
)

# MONGOMS_*: mongod is never started directly. mongodb-memory-server picks the
# apt-installed binary up through MONGOMS_SYSTEM_BINARY and gives every jest
# worker its own isolated instance, which is what
# packages/db-mongodb/src/connect.ts selects under NODE_ENV=test. Pinning the
# binary also keeps the image offline-deterministic: the default download is
# mongod 5.0.13, which has no build for every arch/distro pair.
_TOOLCHAIN_ENV = (
    "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
    "MONGOMS_SYSTEM_BINARY=/usr/bin/mongod",
    "MONGOMS_SYSTEM_BINARY_VERSION_CHECK=false",
    "MONGOMS_RUNTIME_DOWNLOAD=false",
    "MONGOMS_DISABLE_POSTINSTALL=1",
    "NODE_OPTIONS=--max-old-space-size=4096",
)


# ---------------------------------------------------------------------------
# Shell scripts.  Written as templates with __PLACEHOLDER__ tokens and rendered
# with str.replace() so that no shell ${...} expansion has to be brace-escaped.
# ---------------------------------------------------------------------------

# The test selection is derived from the PR patches, which are baked into the
# image, so it is computed once at build time (appended to prepare.sh) and the
# three run scripts merely read the result. That makes it impossible for the
# stages to disagree about which tests to run, so their test names line up.
# SUITES: a patch that only edits test/fields/collections/Array/index.ts still
# needs test/fields/*int.spec.ts to run, so the whole suite is selected.
# UNIT: unit specs live next to the source under packages/ and are matched by
# jest.config.js testMatch, so they are taken verbatim rather than by suite.
# e2e specs are also taken verbatim: booting the admin panel per suite is
# expensive, so only the specs the patches actually touch are run.
# Outputs /home/jest_pattern.txt (a --testPathPattern regex) and
# /home/e2e_specs.txt (one Playwright spec per line); either may be empty.
_SELECT_TESTS = r"""
CHANGED=$(cat /home/test.patch /home/fix.patch 2>/dev/null \
  | sed -n 's|^diff --git a/\(.*\) b/.*$|\1|p' | sort -u)

SUITES=$(printf '%s\n' "$CHANGED" | grep -E '^test/[^/]+/' | cut -d/ -f2 | sort -u || true)

UNIT=$(printf '%s\n' "$CHANGED" | grep -E '^packages/.+\.spec\.tsx?$' | sort -u || true)

PATTERN=""
for suite in $SUITES; do
  PATTERN="${PATTERN}${PATTERN:+|}/test/${suite}/.*int\.spec\.ts$"
done
for unit in $UNIT; do
  escaped=$(printf '%s' "$unit" | sed 's/[.[\*^$]/\\&/g')
  PATTERN="${PATTERN}${PATTERN:+|}/${escaped}$"
done
printf '%s' "$PATTERN" > /home/jest_pattern.txt

printf '%s\n' "$CHANGED" | grep -E '^test/.*e2e\.spec\.ts$' | sort -u \
  > /home/e2e_specs.txt || true

echo "=== Selected jest pattern: $(cat /home/jest_pattern.txt) ==="
echo "=== Selected e2e specs: $(tr '\n' ' ' < /home/e2e_specs.txt) ==="
"""


# _EXEC_TESTS is concatenated verbatim into run.sh, test-run.sh and fix-run.sh,
# so all three stages run byte-identical test commands with the same reporters.
# --reporter=list is mandatory: with CI=true Playwright defaults to the dot
# reporter, which prints no names for passing tests (measured: 0 passed/0 failed).
# </dev/null stops the runner from consuming the spec list on the loop's stdin.
# A spec absent from the working tree is one the test patch has not created yet
# in the baseline stage, so it is skipped rather than run.
_SCRIPT_HEADER = r"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export DISABLE_LOGGING=true
export PAYLOAD_DROP_DATABASE=true
export NODE_NO_WARNINGS=1

cd /home/__REPO__
"""


_EXEC_TESTS = r"""
echo "=== Selected jest pattern: $(cat /home/jest_pattern.txt) ==="
echo "=== Selected e2e specs: $(tr '\n' ' ' < /home/e2e_specs.txt) ==="

STATUS=0

if [ -s /home/jest_pattern.txt ]; then
  echo "=== Running Jest ==="
  pnpm exec jest \
    --forceExit \
    --detectOpenHandles \
    --verbose \
    --maxWorkers=2 \
    --testPathPattern="$(cat /home/jest_pattern.txt)" || STATUS=$?
else
  echo "=== No Jest suites selected ==="
fi

if [ -s /home/e2e_specs.txt ]; then
  while IFS= read -r spec; do
    if [ ! -f "$spec" ]; then
      echo "=== E2E spec absent at this revision: $spec ==="
      continue
    fi
    echo "=== Running Playwright spec: $spec ==="
    pnpm exec playwright test "$spec" \
      -c playwright.config.ts \
      --reporter=list \
      --workers=1 < /dev/null || STATUS=$?
  done < /home/e2e_specs.txt
else
  echo "=== No E2E specs selected ==="
fi

echo "=== Test run complete ==="
exit $STATUS
"""


# _REBUILD rebuilds packages/payload after a patch touches packages/. It is
# concatenated into test-run.sh and fix-run.sh only, since it must run after the
# patches are applied and run.sh applies none.
# test/**/int.spec.ts imports packages/payload/src directly, but the sibling
# workspace packages (richtext-lexical, db-postgres, ...) import the `payload`
# package, which resolves through its exports map into packages/payload/dist.
# Without this rebuild a fix inside packages/payload/src would be invisible to
# them (e.g. PR #4103 patches flattenTopLevelFields.ts and is consumed by
# richtext-lexical through `payload/utilities`).
_REBUILD = r"""
if git diff --name-only HEAD | grep -qE '^packages/'; then
  echo "=== Rebuilding packages/payload ==="
  pnpm run build
else
  echo "=== No packages/ changes, skipping rebuild ==="
fi
"""


# prepare.sh owns all provisioning, so the base image is only the git clone.
# apt/npm/curl all run at PR-image build time via `RUN bash /home/prepare.sh`,
# which has network access. The apt lists are cleared once at the end rather than
# per install, since it is a single layer now.
# mongod is never started directly: mongodb-memory-server picks the apt-installed
# binary up through MONGOMS_SYSTEM_BINARY (set in the base ENV, pointing at a path
# this script creates) and gives every jest worker its own isolated instance.
# The libvips pre-seed writes into sharp's own tarball cache, which sharp checks
# before reaching for the GitHub release CDN. Arch is read from uname rather than
# TARGETARCH because the harness builds with the classic builder, where the
# TARGETARCH build arg is empty.
# The node -e line is registry rot, not a repo bug:
# packages/db-postgres/package.json pins the exact snapshot build
# drizzle-kit@0.19.13-e99bac1 at every commit in this range, and that build has
# since been unpublished from npm (tarball -> HTTP 404), which aborts the whole
# pnpm install. drizzle-kit is a postgres-migration-only devDependency and these
# tests run on the mongoose adapter, so overriding it to the published 0.19.13
# restores the install without touching the test path.
# sharp fetches a prebuilt libvips from the GitHub release CDN during its install
# lifecycle, which intermittently answers ECONNREFUSED; pnpm then aborts before
# it finishes linking bins, leaving node_modules/.bin without jest or playwright.
# The libvips pre-seed above makes this normally offline, and the retry loop
# covers the rest.
# --ignore-scripts is deliberately NOT used as a fallback: sharp is a hard import
# in packages/payload/src/uploads/generateFileData.ts, which every suite reaches
# through collections/operations/create.ts, so a sharp-less tree makes every
# suite fail to load and yields a silent 0/0/0 report in all three stages.
# The three assertions are what actually seal the image; the require("sharp")
# smoke test is the one that catches a tree that looks complete but cannot run.
# playwright install is per PR image because the browser build must match the
# @playwright/test version pinned by that commit.
_PREPARE_SH = (
    r"""#!/bin/bash
set -e

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg git build-essential make python3 sudo lsb-release

wget -qO - https://pgp.mongodb.com/server-6.0.asc \
    | gpg --dearmor -o /usr/share/keyrings/mongodb-server-6.0.gpg
echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-6.0.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-6.0.list
apt-get update
apt-get install -y --no-install-recommends mongodb-org-server

apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libx11-xcb1 \
    libxshmfence1 fonts-liberation fonts-noto-color-emoji
rm -rf /var/lib/apt/lists/*

npm install -g pnpm@8

mkdir -p /root/.npm/_libvips
case "$(uname -m)" in
    aarch64) SHARP_ARCH=linux-arm64v8 ;;
    x86_64)  SHARP_ARCH=linux-x64 ;;
    *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
for LIBVIPS_VERSION in 8.13.3 8.14.5; do
  curl -fsSL -o "/root/.npm/_libvips/libvips-${LIBVIPS_VERSION}-${SHARP_ARCH}.tar.br" \
    "https://github.com/lovell/sharp-libvips/releases/download/v${LIBVIPS_VERSION}/libvips-${LIBVIPS_VERSION}-${SHARP_ARCH}.tar.br"
done

cd /home/__REPO__

git reset --hard
git checkout __BASE_SHA__

node -e 'const f="package.json";const fs=require("fs");const p=JSON.parse(fs.readFileSync(f,"utf8"));p.pnpm=p.pnpm||{};p.pnpm.overrides=Object.assign({},p.pnpm.overrides,{"drizzle-kit":"0.19.13"});fs.writeFileSync(f,JSON.stringify(p,null,2));'

installed=0
for attempt in 1 2 3 4 5; do
  if pnpm install --no-frozen-lockfile; then
    installed=1
    break
  fi
  echo "=== pnpm install attempt ${attempt} failed, retrying ==="
  sleep 20
done
test "$installed" -eq 1

test -x node_modules/.bin/jest
test -x node_modules/.bin/playwright
(cd packages/payload && node -e 'require("sharp")')

pnpm exec playwright install --with-deps chromium || pnpm exec playwright install chromium || true

pnpm run build
test -d packages/payload/dist
"""
    + _SELECT_TESTS
)


_APPLY_TEST_PATCH = r"""
git reset --hard
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch
"""


_APPLY_BOTH_PATCHES = r"""
git reset --hard
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch
"""


_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _APPLY_TEST_PATCH + _REBUILD + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _REBUILD + _EXEC_TESTS


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

__CLEAR_ENV__

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

ARG BASE_COMMIT="__BASE_SHA__"

WORKDIR /home/__REPO__

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

__HARDENING__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


# ---------------------------------------------------------------------------
# parse_log
#
# Two reporters end up in the same log:
#
#   Jest verbose (jest.config.js sets verbose: true)
#       PASS test/b.int.spec.ts
#         Uploads
#           ✓ nested duplicate name (2 ms)
#           ✎ todo a todo test
#       FAIL test/a.int.spec.ts
#         Auth
#           ✓ top level test (61 ms)
#           GraphQL - admin user
#             ✕ should fail login (2 ms)
#             ○ skipped should be skipped
#
#     Leaf names collide - "nested duplicate name" occurred three times across
#     two files in a real jest 29.7 run - so the describe() nesting is
#     reconstructed from the two-space indentation and the name is emitted as
#     "<file> › <describe> › ... › <test>".
#
#   Playwright list reporter
#         ✓  1 fields/e2e.spec.ts:5:9 › fields › text › should display field (12ms)
#         ✘  2 fields/e2e.spec.ts:6:9 › fields › text › should fail here (6ms)
#         -  3 fields/e2e.spec.ts:7:10 › fields › text › should be skipped
#
#     The file:line:col is dropped: applying test.patch/fix.patch shifts line
#     numbers, so keeping them would give the same test a different name in
#     each stage and Report.__post_init__ would score it as two tests.
#
# Both formats have optional, run-dependent duration suffixes, which are
# stripped for the same reason.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DURATION_RE = re.compile(r"\s*\((?:\d+(?:[.,]\d+)?\s*(?:ms|s|m)\s*)+\)\s*$")
_RETRY_RE = re.compile(r"\s*\(retry\s*#\d+\)\s*$")

_PASS_SYMBOLS = "\u2713\u2714\u221a"  # ✓ ✔ √
_FAIL_SYMBOLS = "\u2715\u2717\u00d7\u2718"  # ✕ ✗ × ✘
_SKIP_SYMBOLS = "\u25cb\u25ef\u270e\u2193"  # ○ ◯ ✎ ↓

_JEST_SUITE_RE = re.compile(r"^(?:PASS|FAIL)\s+(\S+\.(?:spec|test)\.[cm]?[jt]sx?)")

_JEST_TEST_RE = re.compile(
    r"^(\s+)([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + _SKIP_SYMBOLS + r"])\s+(\S.*)$"
)

_PW_TEST_RE = re.compile(
    r"^([" + _PASS_SYMBOLS + _FAIL_SYMBOLS + r"\-\u2013])\s+\d+\s+"
    r"(?:\[[^\]]+\]\s*\u203a\s*)?"
    r"(\S+?):\d+:\d+\s*\u203a\s*(\S.*)$"
)

_JEST_BLOCK_TERMINATORS = (
    "Test Suites:",
    "Tests:",
    "Snapshots:",
    "Time:",
    "Ran all test suites",
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "console.debug",
    "at ",
)

_JEST_SKIP_PREFIXES = ("skipped ", "todo ")


def _clean_title(title: str) -> str:
    title = _DURATION_RE.sub("", title.strip())
    title = _RETRY_RE.sub("", title).strip()
    return title


def payload_v2_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(symbol: str, name: str) -> None:
        if not name:
            return
        if symbol in _PASS_SYMBOLS:
            passed_tests.add(name)
        elif symbol in _FAIL_SYMBOLS:
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    current_file: str | None = None
    describe_stack: list[str] = []
    in_jest_block = False

    for raw_line in test_log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()

        pw_match = _PW_TEST_RE.match(stripped)
        if pw_match:
            in_jest_block = False
            spec = pw_match.group(2)
            title = _clean_title(pw_match.group(3))
            record(pw_match.group(1), f"{spec} \u203a {title}")
            continue

        suite_match = _JEST_SUITE_RE.match(stripped)
        if suite_match:
            current_file = suite_match.group(1)
            describe_stack = []
            in_jest_block = True
            continue

        if not stripped:
            continue

        if not in_jest_block:
            continue

        if stripped.startswith("\u25cf") or stripped.startswith(
            _JEST_BLOCK_TERMINATORS
        ):
            in_jest_block = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jest_block = False
            continue

        test_match = _JEST_TEST_RE.match(line)
        if test_match:
            symbol = test_match.group(2)
            title = _clean_title(test_match.group(3))
            if symbol in _SKIP_SYMBOLS:
                for prefix in _JEST_SKIP_PREFIXES:
                    if title.startswith(prefix):
                        title = title[len(prefix) :].strip()
                        break
            context = describe_stack[: max(indent // 2 - 1, 0)]
            parts = ([current_file] if current_file else []) + context + [title]
            record(symbol, " \u203a ".join(part for part in parts if part))
        else:
            level = max(indent // 2, 1)
            describe_stack = describe_stack[: level - 1]
            describe_stack.append(stripped)

    # TestResult.__post_init__ requires the three sets to be pairwise disjoint.
    # A real execution outranks a skip, and a failure outranks everything.
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


class PayloadV2EraImageBase(Image):
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
        # .nvmrc and .node-version both pin v18.17.1 across the whole range.
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

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
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
            .replace("__CLEAR_ENV__", self.clear_env)
        )

    # The enhancer's ENV block, any --global_env vars and the toolchain vars are
    # emitted as one ENV instruction so the base image has a single ENV section.
    # clear_env stays separate because its job is to blank those vars at the end.
    def _merged_env_block(self) -> str:
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(_TOOLCHAIN_ENV)
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )


class PayloadV2EraImageDefault(Image):
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
        return PayloadV2EraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _GIT_APPLY_EXCLUDES)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__HARDENING__", Image._HARDENING_BLOCK)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__CLEAR_ENV__", self.clear_env)
        )


@Instance.register("payloadcms", _INTERVAL_NAME)
class PAYLOAD_3833_TO_4113(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PayloadV2EraImageDefault(self.pr, self._config)

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
        return payload_v2_parse_log(test_log)
