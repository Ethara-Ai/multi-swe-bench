from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "heygen-com"
REPO = "hyperframes"

_INTERVAL_NAME = "hyperframes_6256_to_3494"
_BUNDLE_NIS = [
    "3494-6256",
    "3494_to_6256",
    "hyperframes_3494_to_6256",
]

_PR_LOW = 3494
_PR_HIGH = 6256

_NODE_IMAGE = "node:22-bookworm"
_TOOLCHAIN_TAG = "toolchain-node22-bun1313-ffmpeg801"
_CLONE_TAG = "base-6256-to-3494"

_BUN_VERSION = "bun-v1.3.13"
_BUN_HOME = "/usr/local/bun"

# The tarball name differs per architecture, so the download is selected from
# TARGETARCH at build time rather than hardcoded to the amd64 slug (an amd64
# binary extracted on arm64 fails the ffprobe smoke test and kills the build).
# Both digests are pinned, so checksum verification is mandatory on either
# arch; a mismatched tarball fails the build rather than reaching the ffprobe
# smoke test. To re-derive one after bumping _FFMPEG_BUILD:
#   curl -fsSL "$_FFMPEG_RELEASE$_FFMPEG_BUILD-linuxarm64-gpl-8.0.tar.xz" | sha256sum
_FFMPEG_RELEASE = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "autobuild-2026-02-28-12-59/"
)
_FFMPEG_BUILD = "ffmpeg-n8.0.1-66-g27b8d1a017"
_FFMPEG_ARCH_SLUG = {"amd64": "linux64", "arm64": "linuxarm64"}
_FFMPEG_SHA256 = {
    "amd64": "26d2ecd0cefba99f9d1ea92252a3a4f743a58df4c1c3cf9f8bbaaa40b1fca5b0",
    "arm64": "d72cc324cb65dd92a9ffe699cc95ccbacfef4def8d3ad9fe2b5164a6a71b9595",
}
_FFMPEG_DIR = "/opt/ffmpeg/bin"
_FFMPEG_STUB = "/opt/hf-ffmpeg"

_APT_PACKAGES = [
    "build-essential",
    "ca-certificates",
    "curl",
    "git",
    "procps",
    "python3",
    "unzip",
    "xz-utils",
]

_TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_MARKER = re.compile(r"^=== MSWEB FILE: (?P<path>\S+) ===$")
_BUN_HEADER = re.compile(r"^(?P<path>[\w./@-]+\.(?:test|spec)\.[cm]?[jt]sx?):$")
_VITEST_LINE = re.compile(
    r"^(?P<mark>[✓×↓·○])\s+"
    r"(?P<name>\S.*?)"
    r"(?:\s+\[(?:skipped|todo)\])?"
    r"(?:\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\))?"
    r"(?:\s+\d+(?:\.\d+)?\s*m?s)?$"
)
_BUN_LINE = re.compile(
    r"^\((?P<kind>pass|fail|skip|todo)\)\s+(?P<name>.+?)(?:\s+\[[\d.]+m?s\])?$"
)
_NODE_LINE = re.compile(
    r"^(?P<mark>[✔✖﹣])\s+(?P<name>.+?)\s+\([\d.]+m?s\)(?P<tail>.*)$"
)
_NODE_SUITE = re.compile(r"^▶\s+(?P<name>.+?)\s*$")

_VITEST_PASS = "✓"
_VITEST_FAIL = "×"
_NODE_PASS = "✔"
_NODE_SKIP = "﹣"


def test_targets(test_patch: str) -> list[str]:
    found: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if path in ("", "/dev/null"):
            continue
        if not _TEST_FILE.search(path.rsplit("/", 1)[-1]):
            continue
        if path not in found:
            found.append(path)
    return sorted(found)


def vitest_groups(targets: list[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for path in targets:
        parts = path.split("/")
        if parts[0] == "packages" and len(parts) > 2:
            pkg = "/".join(parts[:2])
            rel = "/".join(parts[2:])
        else:
            pkg = "."
            rel = path
        groups.setdefault(pkg, []).append(rel)
    return [(pkg, sorted(rels)) for pkg, rels in sorted(groups.items())]


_RUN_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export DO_NOT_TRACK=1
export HYPERFRAMES_NO_TELEMETRY=1
export HYPERFRAMES_MEDIA_HOME_REQUIRED=1
export NODE_OPTIONS=--max-old-space-size=4096
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_update_notifier=false"""

_BUILD_WORKSPACE = """msweb_build() {
    local label="$1"
    shift
    if ! "$@" >> /tmp/msweb_build.log 2>&1; then
        echo "MSWEB BUILD| step '$label' failed; last 40 lines follow"
        tail -n 40 /tmp/msweb_build.log | sed 's/^/MSWEB BUILD| /'
    fi
}

: > /tmp/msweb_build.log
msweb_build parsers-lint-studio bun run --filter '@hyperframes/{parsers,lint,studio-server}' build
msweb_build core bun run --cwd packages/core build
msweb_build core-runtime bun run --cwd packages/core build:hyperframes-runtime
msweb_build engine bun run --filter @hyperframes/engine build"""

_RUNNERS = r"""MSWEB_OUT=/tmp/msweb_tests.log
MSWEB_INVOKED=0
: > "$MSWEB_OUT"

is_bun_test() {
    grep -q 'from "bun:test"' "$1"
}

is_node_test() {
    case "$1" in
        *.mjs|*.cjs) return 0 ;;
    esac
    grep -q 'node:test' "$1"
}

# Each runner streams to stdout (so parse_log sees it) and to $MSWEB_OUT (so the
# end-of-script gate can inspect it), and records the runner's real exit status
# via PIPESTATUS instead of discarding it with `|| true`.  A non-zero status is
# expected whenever tests fail and so must not abort the script -- but it is now
# reported, and a runner that never started at all is caught by the gate.
msweb_note_exit() {
    [ "$2" -eq 0 ] || printf 'MSWEB RUNNER| %s exited %s\n' "$1" "$2"
}

run_vitest() {
    local pkg="$1"
    shift
    local sel=""
    local rel
    for rel in "$@"; do
        [ -f "$pkg/$rel" ] || continue
        if is_bun_test "$pkg/$rel"; then
            continue
        fi
        if is_node_test "$pkg/$rel"; then
            continue
        fi
        sel="$sel $rel"
    done
    [ -n "$sel" ] || return 0
    MSWEB_INVOKED=1
    local st
    set +e
    (
        cd "$pkg" || exit 1
        npx vitest run --reporter=verbose --coverage.enabled=false \
            --testTimeout=30000 --hookTimeout=30000 --passWithNoTests $sel
    ) 2>&1 | tee -a "$MSWEB_OUT"
    st=${PIPESTATUS[0]}
    set -e
    msweb_note_exit "vitest($pkg)" "$st"
    return 0
}

run_bun() {
    local f
    local st
    for f in "$@"; do
        [ -f "$f" ] || continue
        is_bun_test "$f" || continue
        MSWEB_INVOKED=1
        set +e
        bun test --timeout 30000 "$f" 2>&1 | tee -a "$MSWEB_OUT"
        st=${PIPESTATUS[0]}
        set -e
        msweb_note_exit "bun($f)" "$st"
    done
    return 0
}

run_node() {
    local imports="--import tsx"
    if [ -f packages/cli/scripts/test-home.mjs ]; then
        imports="$imports --import ./packages/cli/scripts/test-home.mjs"
    fi
    local f
    local st
    for f in "$@"; do
        [ -f "$f" ] || continue
        if is_bun_test "$f"; then
            continue
        fi
        is_node_test "$f" || continue
        MSWEB_INVOKED=1
        printf '=== MSWEB FILE: %s ===\n' "$f" | tee -a "$MSWEB_OUT"
        set +e
        node $imports --test --test-reporter=spec "$f" 2>&1 | tee -a "$MSWEB_OUT"
        st=${PIPESTATUS[0]}
        set -e
        msweb_note_exit "node($f)" "$st"
    done
    return 0
}"""

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1
test -z "$(git status --porcelain)"
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse [[BASE_SHA]])"

[[RUN_ENV]]

ok=0
for attempt in 1 2 3 4 5; do
    if timeout -k 60 600 bun install --frozen-lockfile; then
        ok=1
        break
    fi
    pkill -9 bun 2>/dev/null || true
    rm -rf "$HOME/.bun/install/cache" 2>/dev/null || true
    rm -rf [[BUN_HOME]]/install/cache 2>/dev/null || true
    rm -rf node_modules 2>/dev/null || true
    sleep 15
done

if [ "$ok" -ne 1 ]; then
    pkill -9 bun 2>/dev/null || true
    rm -rf "$HOME/.bun/install/cache" node_modules 2>/dev/null || true
    timeout -k 60 900 bun install
fi

test -d node_modules
test -x node_modules/.bin/vitest
test -x node_modules/.bin/tsx
ffprobe -version > /dev/null
"""

# The result marks parse_log recognises, as one source of truth shared with
# _VITEST_LINE / _BUN_LINE / _NODE_LINE above.  Written as escapes so this
# module stays pure ASCII.
_RESULT_MARKS = "\u2713\u00d7\u2193\u00b7\u25cb\u2714\u2716\ufe63"

_RESULT_GREP = (
    "grep -qE '("
    + "|".join(_RESULT_MARKS)
    + r")[[:space:]]|\((pass|fail|skip|todo)\)[[:space:]]' "
    + '"$MSWEB_OUT"'
)

# run.sh: at baseline the test files added by test.patch do not exist yet, so an
# empty result set is legitimate here and must NOT fail.  Report.check() requires
# results only from the FIX stage (report.py:204); run.sh exists to give the
# step-6 classifier (report.py:269-313) temporal proof that a test pre-existed.
_GATE_SOFT = f"""if [ "$MSWEB_INVOKED" -eq 0 ]; then
    echo "MSWEB NOTE| baseline ran no test file (all targets are new in test.patch)"
elif ! {_RESULT_GREP}; then
    echo "MSWEB NOTE| baseline runners produced no parseable results"
fi
exit 0"""

# test-run.sh / fix-run.sh: this gate is what redeems tolerating a non-zero
# runner status above.  A runner that failed to START (missing binary, bad
# config) is otherwise indistinguishable from a suite that merely failed, and
# both reach parse_log as 0/0/0 -- which Report.check() rule 1 (report.py:204)
# rejects only after the image is built and all three stages have run.
_GATE_HARD = f"""if [ "$MSWEB_INVOKED" -eq 0 ]; then
    echo "MSWEB FATAL| no runner matched any test file after applying the patch(es)" >&2
    exit 1
fi
if ! {_RESULT_GREP}; then
    echo "MSWEB FATAL| runners started but produced no parseable test results" >&2
    exit 1
fi
exit 0"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]] || exit 1

[[RUN_ENV]]

[[BUILD]]

[[RUNNERS]]

[[INVOCATIONS]]

[[GATE]]
"""

_PATCH_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]] || exit 1

if ! git apply --3way --whitespace=nowarn [[PATCHES]]; then
    exit 1
fi

[[RUN_ENV]]

[[BUILD]]

[[RUNNERS]]

[[INVOCATIONS]]

[[GATE]]
"""


class HyperframesToolchainImage(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _TOOLCHAIN_TAG

    def workdir(self) -> str:
        return _TOOLCHAIN_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        amd64_slug = _FFMPEG_ARCH_SLUG["amd64"]
        arm64_slug = _FFMPEG_ARCH_SLUG["arm64"]
        amd64_sha = _FFMPEG_SHA256["amd64"]
        arm64_sha = _FFMPEG_SHA256["arm64"]
        packages_str = " \\\n    ".join(_APT_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {base_img}
{global_env}
WORKDIR /home/

{apt_command}

RUN curl -fsSL https://bun.sh/install \\
    | BUN_INSTALL="{_BUN_HOME}" bash -s "{_BUN_VERSION}"

RUN set -eux; \\
    case "${{TARGETARCH:-amd64}}" in \\
        amd64) ff_slug="{amd64_slug}"; ff_sha="{amd64_sha}" ;; \\
        arm64) ff_slug="{arm64_slug}"; ff_sha="{arm64_sha}" ;; \\
        *) echo "unsupported TARGETARCH: ${{TARGETARCH}}" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL --retry 5 --retry-all-errors -o /tmp/ffmpeg.tar.xz \\
        "{_FFMPEG_RELEASE}{_FFMPEG_BUILD}-${{ff_slug}}-gpl-8.0.tar.xz"; \\
    if [ -n "${{ff_sha}}" ]; then \\
        test "$(sha256sum /tmp/ffmpeg.tar.xz | cut -d' ' -f1)" = "${{ff_sha}}"; \\
    else \\
        echo "WARNING: no pinned sha256 for ${{TARGETARCH}}, relying on exec check" >&2; \\
    fi; \\
    mkdir -p {_FFMPEG_DIR}; \\
    tar -xJf /tmp/ffmpeg.tar.xz -C {_FFMPEG_DIR} --strip-components=2 \\
        --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \\
    rm -f /tmp/ffmpeg.tar.xz; \\
    cp {_FFMPEG_DIR}/ffmpeg {_FFMPEG_STUB}; \\
    chmod +x {_FFMPEG_STUB} {_FFMPEG_DIR}/ffmpeg {_FFMPEG_DIR}/ffprobe; \\
    {_FFMPEG_DIR}/ffprobe -version > /dev/null; \\
    {_FFMPEG_DIR}/ffmpeg -version > /dev/null

ENV BUN_INSTALL="{_BUN_HOME}"
ENV PATH="{_BUN_HOME}/bin:{_FFMPEG_DIR}:${{PATH}}"
ENV FFMPEG_BIN="{_FFMPEG_STUB}"
{clear_env}
CMD ["/bin/bash"]
"""


class HyperframesCloneImage(Image):
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
        return HyperframesToolchainImage(self.pr, self._config)

    def image_tag(self) -> str:
        return _CLONE_TAG

    def workdir(self) -> str:
        return _CLONE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("HyperframesCloneImage dependency must be an Image")

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        return f"""FROM {image.image_full_name()}
{global_env}
WORKDIR /home/

RUN git clone "https://github.com/{ORG}/{REPO}.git" /home/{REPO}
{clear_env}
CMD ["/bin/bash"]
"""


class HyperframesInstanceImage(Image):
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
        return HyperframesCloneImage(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _invocations(self) -> str:
        targets = test_targets(self.pr.test_patch)
        if not targets:
            # Emitting `true` here would make all three stages report 0/0/0,
            # which Report.check() rule 1 (report.py:204) rejects only after
            # the image has been built and every stage has run.  Fail at the
            # first stage instead, naming the cause.
            return (
                'echo "MSWEB FATAL| test.patch touches no runnable test file; '
                'this instance cannot produce a fail-to-pass transition" >&2\n'
                "exit 1"
            )
        lines = []
        for pkg, rels in vitest_groups(targets):
            lines.append("run_vitest {} {}".format(pkg, " ".join(rels)))
        joined = " ".join(targets)
        lines.append(f"run_bun {joined}")
        lines.append(f"run_node {joined}")
        return "\n".join(lines)

    def _render(self, template: str, **subs: str) -> str:
        # Block insertions first, scalar substitutions second: a scalar token
        # such as [[REPO]] must still resolve when it appears *inside* one of
        # the inserted blocks.
        out = template.replace("[[RUN_ENV]]", _RUN_ENV)
        out = out.replace("[[BUILD]]", _BUILD_WORKSPACE)
        out = out.replace("[[RUNNERS]]", _RUNNERS)
        out = out.replace("[[INVOCATIONS]]", self._invocations())
        for key, value in subs.items():
            out = out.replace(f"[[{key}]]", value)
        out = out.replace("[[REPO]]", REPO)
        out = out.replace("[[BUN_HOME]]", _BUN_HOME)
        # Narrow to the token shape so shell constructs such as [[:space:]]
        # inside an inserted block are not mistaken for a template token.
        leftover = re.search(r"\[\[[A-Z_]+\]\]", out)
        if leftover:
            raise ValueError(f"unresolved template token: {leftover.group(0)}")
        return out

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                self._render(_PREPARE_SH, BASE_SHA=self.pr.base.sha),
            ),
            File(".", "run.sh", self._render(_RUN_SH, GATE=_GATE_SOFT)),
            File(
                ".",
                "test-run.sh",
                self._render(
                    _PATCH_RUN_SH, PATCHES="/home/test.patch", GATE=_GATE_HARD
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(
                    _PATCH_RUN_SH,
                    PATCHES="/home/test.patch /home/fix.patch",
                    GATE=_GATE_HARD,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("HyperframesInstanceImage dependency must be an Image")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Stage order follows the project's canonical PR Dockerfile layout:
        # reset/checkout, COPY, prepare.sh, then the hardening block LAST so
        # that its four assertions describe the final image state rather than
        # an intermediate one that `prepare.sh` could still have perturbed.
        # Running prepare.sh before the scrub also leaves tags and remotes in
        # place for it, which is strictly more git context than it had before.
        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"
{global_env}
WORKDIR /home/{REPO}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK.rstrip()}
{clear_env}"""


class HYPERFRAMES_6256_TO_3494(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if not (_PR_LOW <= pr.number <= _PR_HIGH):
            raise ValueError(
                f"{ORG}/{REPO} PR {pr.number} is tagged {_INTERVAL_NAME} but "
                f"falls outside {_PR_LOW}-{_PR_HIGH}"
            )
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HyperframesInstanceImage(self.pr, self._config)

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
        targets = test_targets(self.pr.test_patch)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        suites: set[str] = set()
        current = ""

        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            if not line:
                continue

            marker = _FILE_MARKER.match(line)
            if marker:
                current = marker.group("path")
                continue

            header = _BUN_HEADER.match(line)
            if header:
                current = header.group("path")
                continue

            suite = _NODE_SUITE.match(line)
            if suite:
                suites.add(suite.group("name").strip())
                continue

            match = _VITEST_LINE.match(line)
            if match:
                name = match.group("name").strip()
                if " > " in name:
                    head, rest = name.split(" > ", 1)
                    for target in targets:
                        if target == head or target.endswith("/" + head):
                            name = f"{target} > {rest}"
                            break
                    mark = match.group("mark")
                    if mark == _VITEST_PASS:
                        passed.add(name)
                    elif mark == _VITEST_FAIL:
                        failed.add(name)
                    else:
                        skipped.add(name)
                    continue

            match = _BUN_LINE.match(line)
            if match:
                name = match.group("name").strip()
                full = f"{current} > {name}" if current else name
                kind = match.group("kind")
                if kind == "pass":
                    passed.add(full)
                elif kind == "fail":
                    failed.add(full)
                else:
                    skipped.add(full)
                continue

            match = _NODE_LINE.match(line)
            if match:
                name = match.group("name").strip()
                if name in suites or name == current:
                    continue
                full = f"{current} > {name}" if current else name
                tail = match.group("tail") or ""
                mark = match.group("mark")
                if mark == _NODE_SKIP or "# SKIP" in tail or "# TODO" in tail:
                    skipped.add(full)
                elif mark == _NODE_PASS:
                    passed.add(full)
                else:
                    failed.add(full)

        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register(ORG, _INTERVAL_NAME)(HYPERFRAMES_6256_TO_3494)
Instance.register(ORG, REPO)(HYPERFRAMES_6256_TO_3494)

for _ni in _BUNDLE_NIS:
    Instance.register(ORG, _ni)(HYPERFRAMES_6256_TO_3494)
