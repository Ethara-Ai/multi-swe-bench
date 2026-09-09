from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "deno"
_TAG_SUFFIX = "2952_to_1281"

_SNAPSHOT = "20260901T000000Z"

_APT_BLOCK = f"""RUN printf 'deb http://snapshot.debian.org/archive/debian/{_SNAPSHOT} bullseye main\\ndeb http://snapshot.debian.org/archive/debian-security/{_SNAPSHOT} bullseye-security main\\n' > /etc/apt/sources.list \\
    && printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99snapshot \\
    && apt-get update && apt-get install -y --no-install-recommends \\
        build-essential ca-certificates curl file git gzip libc6-dev libxml2 lsb-release \\
        lld ninja-build nodejs npm pkg-config python2 python2-dev python3 tar unzip xz-utils zip \\
    && ln -sf /usr/bin/python2 /usr/bin/python \\
    && rm -rf /var/lib/apt/lists/*"""

_TEST_NAME_SCAN = r"""python2 - <<'MSBPY'
import glob
import re

pattern = re.compile(
    r'\btest(?:Perm)?\s*\((?:[^()]|\([^()]*\))*?function\s+([A-Za-z0-9_$]+)\s*\(',
    re.S,
)
files = sorted(glob.glob('js/*_test.ts') + glob.glob('tools/*_test.ts'))
for path in files:
    try:
        source = open(path).read()
    except IOError:
        continue
    for match in pattern.finditer(source):
        print "MSB_TESTFILE %s %s" % (path, match.group(1))
MSBPY"""

_PERM_MATRIX_SCAN = r"""python2 - <<'MSBPERM' > /tmp/msb_perms.txt
import re

source = open('tools/unit_tests.py').read()
pattern = re.compile(
    r'run_unit_test\(\s*deno_exe\s*,\s*"([^"]+)"\s*(?:,\s*\[([^\]]*)\])?',
    re.S,
)
cmd = re.search(r"cmd = \[[^\n]*", source)
pos = "before"
if cmd:
    line = cmd.group(0)
    fi, si = line.find("flags"), line.find("unit_tests.ts")
    if fi >= 0 and si >= 0 and fi > si:
        pos = "after"
print "MSBPOS|%s" % pos

for match in pattern.finditer(source):
    flags = " ".join(re.findall(r'"([^"]+)"', match.group(2) or ""))
    print "%s|%s" % (match.group(1), flags)
MSBPERM"""


_RING_ARM64_PATCH = r'''import glob
import os
import re
import sys


def patch_build_gn():
    gn = "build_extra/rust/BUILD.gn"
    if not os.path.exists(gn):
        return
    text = open(gn).read()
    if 'current_cpu == "arm64"' in text:
        return
    block = re.search(
        r'( *)if \(is_linux\) \{\n( *)sources \+= \[\n((?:[^\]]*?x86_64[^\]]*?\n)+?)( *)\]\n',
        text,
    )
    if not block:
        sys.stderr.write("ringpatch: no x86_64 ring asm block found\n")
        return
    indent, sind, body, cind = block.groups()
    pm = re.search(r'"([^"]*?)pregenerated/[^"]*x86_64[^"]*\.S"', body)
    if not pm:
        sys.stderr.write("ringpatch: could not derive the ring source prefix\n")
        return
    prefix = pm.group(1)
    pregen = glob.glob("third_party/rust_crates/registry/src/*/ring-*/pregenerated")
    if not pregen:
        sys.stderr.write("ringpatch: ring pregenerated directory not found\n")
        return
    arm = sorted(
        os.path.basename(f)
        for f in glob.glob(os.path.join(pregen[0], "*.S"))
        if f.endswith("-linux64.S") and "ios64" not in f
    )
    if not arm:
        sys.stderr.write("ringpatch: this ring version ships no armv8 assembly\n")
        return
    ringdir = os.path.dirname(pregen[0])
    arm_c = [
        c
        for c in ("crypto/cpu-arm-linux.c", "crypto/cpu-arm.c",
                  "crypto/cpu-aarch64-linux.c", "crypto/fipsmodule/aes/aes.c")
        if os.path.exists(os.path.join(ringdir, c)) and c not in text
    ]
    lines = ['%s"%s%s",\n' % (sind + "  ", prefix, c) for c in arm_c]
    lines += ['%s"%spregenerated/%s",\n' % (sind + "  ", prefix, f) for f in arm]
    has_armcap_c = any("cpu-aarch64" in c or "cpu-arm" in c for c in arm_c)
    new = (
        '%sif (is_linux && current_cpu == "arm64") {\n%ssources += [\n%s%s]\n'
        '%s' '%s} else if (is_linux) {\n%ssources += [\n%s%s]\n'
        % (indent, sind, "".join(lines), cind,
           "" if has_armcap_c else '%sdefines = [ "OPENSSL_STATIC_ARMCAP" ]\n' % sind,
           indent, sind, body, cind)
    )
    open(gn, "w").write(text.replace(block.group(0), new, 1))
    sys.stderr.write(
        "ringpatch: arm64 branch with %d armv8 asm files + OPENSSL_STATIC_ARMCAP\n"
        % len(arm)
    )


CPUID_OLD = """        use std;
        extern "C" {
            fn GFp_cpuid_setup();
        }
        static INIT: std::sync::Once = std::sync::ONCE_INIT;
        INIT.call_once(|| unsafe { GFp_cpuid_setup() });"""

CPUID_NEW = """        #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
        {
            use std;
            extern "C" {
                fn GFp_cpuid_setup();
            }
            static INIT: std::sync::Once = std::sync::ONCE_INIT;
            INIT.call_once(|| unsafe { GFp_cpuid_setup() });
        }"""


def patch_ring_cpu_rs():
    for path in glob.glob(
        "third_party/rust_crates/registry/src/*/ring-*/src/*.rs"
    ):
        text = open(path).read()
        if CPUID_OLD not in text:
            continue
        open(path, "w").write(text.replace(CPUID_OLD, CPUID_NEW, 1))
        sys.stderr.write("ringpatch: arch-gated GFp_cpuid_setup in %s\n" % path)


patch_build_gn()
patch_ring_cpu_rs()
'''


_ARCH_SHIM = r"""import os
import re
import sys

PATTERN = re.compile(
    r'(#\[cfg\(target_arch = "x86_64"\)\]\s*\nstatic BUILD_ARCH: &str = "x64";)'
)
REPLACEMENT = (
    '#[cfg(target_arch = "aarch64")]\n'
    'static BUILD_ARCH: &str = "aarch64";\n'
    r'\1'
)

changed = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "third_party", "target")]
    for name in files:
        if not name.endswith(".rs"):
            continue
        path = os.path.join(root, name)
        try:
            text = open(path).read()
        except (IOError, UnicodeDecodeError):
            continue
        if "BUILD_ARCH" not in text or 'target_arch = "aarch64"' in text:
            continue
        new, n = PATTERN.subn(REPLACEMENT, text, count=1)
        if n:
            open(path, "w").write(new)
            changed.append(path)

if changed:
    sys.stderr.write(
        "arch_shim: added an aarch64 BUILD_ARCH arm to %s\n" % ", ".join(changed)
    )

CCHAR = re.compile(r'\*(const|mut) i8\b')

for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "third_party", "target")]
    for name in files:
        if not name.endswith(".rs"):
            continue
        path = os.path.join(root, name)
        try:
            text = open(path).read()
        except (IOError, UnicodeDecodeError):
            continue
        new, n = CCHAR.subn(r"*\1 std::os::raw::c_char", text)
        if n:
            open(path, "w").write(new)
            sys.stderr.write(
                "arch_shim: %s pointer-to-i8 -> c_char in %s\n" % (n, path)
            )
"""


_SUBMODULE_SYNC = r'''import re
import subprocess
import sys

pattern = re.compile(
    r"^diff --git a/(\S+) b/\S+\n"
    r"index [0-9a-f]+\.\.[0-9a-f]+ 160000\n"
    r"(?:.*\n)*?"
    r"\+Subproject commit ([0-9a-f]{40})",
    re.M,
)
for path in sys.argv[1:]:
    try:
        text = open(path).read()
    except IOError:
        continue
    for sub, sha in pattern.findall(text):
        sys.stderr.write("submodule_sync: %s -> %s\n" % (sub, sha))
        for cmd in (
            ["git", "-C", sub, "fetch", "--depth", "1", "origin", sha],
            ["git", "-C", sub, "fetch", "origin"],
        ):
            if subprocess.call(cmd) == 0:
                break
        if subprocess.call(["git", "-C", sub, "checkout", "-q", sha]) != 0:
            sys.stderr.write("submodule_sync: checkout of %s failed\n" % sha)
'''


_DEPOT_SHIMS = """cat > third_party/depot_tools/ninja <<'MSBNINJA'
#!/bin/sh
exec /usr/bin/ninja -j 4 "$@"
MSBNINJA
chmod 0755 third_party/depot_tools/ninja
if [ "$(dpkg --print-architecture)" != "amd64" ]; then
cat > third_party/depot_tools/gn <<'MSBGN'
#!/bin/sh
exec /usr/local/bin/gn "$@"
MSBGN
    chmod 0755 third_party/depot_tools/gn
fi"""


def _heredoc(tag: str, script: str, args: str = "") -> str:
    prefix = " " + args if args else ""
    return "python3 -%s <<'%s'\n%s%s" % (prefix, tag, script, tag)


_BUILD_BLOCK = """BUILD_RC=0
if [ -f tools/build.py ]; then
    python2 ./tools/build.py -C target/release deno || BUILD_RC=$?
else
    cargo build --release --locked || BUILD_RC=$?
fi"""

_TOOLCHAIN_BLOCK = """RUST_VERSION=$(grep -oE '1\\.[0-9]+\\.[0-9]+' .travis.yml | head -1)
test -n "$RUST_VERSION" || { echo "prepare.sh: could not read the rust version from .travis.yml"; exit 1; }
echo "prepare.sh: repo pins rust $RUST_VERSION"
rustup toolchain install "$RUST_VERSION" --profile minimal --no-self-update
rustup default "$RUST_VERSION"
rustc --version
cargo --version"""


class _ImageBase(Image):
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
        return "debian:bullseye-slim"

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{REPO_DIR}'
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

{_APT_BLOCK}

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:$PATH

RUN curl -sSf https://sh.rustup.rs -o /tmp/rustup.sh \\
    && sh /tmp/rustup.sh -y --no-modify-path --default-toolchain none \\
    && rm /tmp/rustup.sh \\
    && rustup --version && python --version

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


def _test_commands(pr: PullRequest) -> str:
    return f"""if ./target/release/deno --help 2>&1 | grep -qE "^[[:space:]]+run[[:space:]]"; then
    RUN_SUBCOMMAND="run"
else
    RUN_SUBCOMMAND=""
fi

if [ -f js/unit_test_runner.ts ]; then
    echo "MSB_RUNNER unit_test_runner"
    ./target/release/deno $RUN_SUBCOMMAND --reload --allow-run --allow-env js/unit_test_runner.ts 2>&1 || true
else
    echo "MSB_RUNNER perm_matrix"
    {_PERM_MATRIX_SCAN}
    test -s /tmp/msb_perms.txt || {{ echo "MSB_NO_PERM_MATRIX"; exit 1; }}
    FLAGPOS=$(grep '^MSBPOS|' /tmp/msb_perms.txt | head -1 | cut -d'|' -f2)
    echo "MSB_FLAGPOS $FLAGPOS"
    grep -v '^MSBPOS|' /tmp/msb_perms.txt > /tmp/msb_combos.txt
    while IFS='|' read -r perm flags; do
        [ -n "$perm" ] || continue
        echo "MSB_PERM_COMBO $perm"
        case " $flags " in *" --reload "*) RELOAD="" ;; *) RELOAD="--reload" ;; esac
        if [ "$FLAGPOS" = "after" ]; then
            ./target/release/deno $RUN_SUBCOMMAND $RELOAD js/unit_tests.ts "$perm" $flags 2>&1 || true
        else
            ./target/release/deno $RUN_SUBCOMMAND $RELOAD $flags js/unit_tests.ts "$perm" 2>&1 || true
        fi
    done < /tmp/msb_combos.txt
fi"""


def _touches_submodule(pr: PullRequest) -> bool:
    pattern = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+ 160000", re.M)
    return bool(pattern.search(pr.fix_patch or "")) or bool(
        pattern.search(pr.test_patch or "")
    )


def _stage_script(pr: PullRequest, patches: str) -> str:
    apply_line = (
        f"git apply --whitespace=nowarn {patches}\n" if patches else ""
    )
    if patches and _touches_submodule(pr):
        apply_line += (
            "@@SUBMODULE_SYNC@@\n"
            "@@DEPOT_SHIMS@@\n"
            "@@RING_PATCH@@\n@@ARCH_SHIM@@\n"
        )
    return f"""#!/bin/bash
set -eo pipefail

cd /home/{REPO_DIR}

export CI=true
export NO_COLOR=1
export PYTHONPATH=/home/{REPO_DIR}/third_party/python_packages
export PATH=/home/{REPO_DIR}/third_party/depot_tools:$PATH

if grep -rq "use_prebuilt_v8" libdeno/deno.gni core/libdeno/deno.gni 2>/dev/null; then
    export DENO_BUILD_ARGS="use_prebuilt_v8=false"
fi

if [ "$(dpkg --print-architecture)" != "amd64" ]; then
    export DENO_GN_PATH=/usr/local/bin/gn
    export DENO_BUILD_ARGS="$DENO_BUILD_ARGS is_clang=false use_lld=true use_sysroot=false use_custom_libcxx=false is_official_build=false treat_warnings_as_errors=false symbol_level=0 rust_treat_warnings_as_errors=false"
    export RUSTFLAGS="-C link-arg=-lstdc++ -C link-arg=-fuse-ld=lld"
fi

{apply_line}find target -name '*.d' -delete 2>/dev/null || true
rm -f target/release/deno

{_BUILD_BLOCK}

if [ "$BUILD_RC" != "0" ]; then
    echo "MSB_BUILD_FAILED"
    exit 1
fi

if [ ! -x target/release/deno ]; then
    echo "MSB_NO_BINARY"
    exit 1
fi

{_TEST_NAME_SCAN}

python2 ./tools/http_server.py > /tmp/http_server.log 2>&1 &
HTTP_SERVER_PID=$!
trap 'kill $HTTP_SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:4545/ ; then
        break
    fi
    sleep 1
done

{_test_commands(pr)}
""".replace("@@SUBMODULE_SYNC@@", _heredoc("MSBSUB", _SUBMODULE_SYNC, patches)).replace("@@DEPOT_SHIMS@@", _DEPOT_SHIMS).replace("@@RING_PATCH@@", _heredoc("MSBRING", _RING_ARM64_PATCH)).replace("@@ARCH_SHIM@@", _heredoc("MSBARCH", _ARCH_SHIM))


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
    amd64|arm64) echo "prepare.sh: building for $ARCH" ;;
    *) echo "prepare.sh: unsupported architecture $ARCH"; exit 1 ;;
esac

cd /home/{repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export GIT_TERMINAL_PROMPT=0
git submodule update --init --depth 1 \
    || git submodule update --init

{toolchain_block}

test -d third_party/v8 || {{ echo "prepare.sh: third_party/v8 missing after submodule init"; exit 1; }}
test -d core/libdeno/build -o -d build || {{ echo "prepare.sh: chromium build submodule missing"; exit 1; }}
test -x third_party/depot_tools/cipd || {{ echo "prepare.sh: depot_tools/cipd missing after submodule init"; exit 1; }}

find js -path '*testing*' -name '*.ts' \
    -exec sed -i 's/exitOnFail = true/exitOnFail = false/g' {{}} + 2>/dev/null || true

export PYTHONPATH=/home/{repo_dir}/third_party/python_packages
export PATH=/home/{repo_dir}/third_party/depot_tools:$PATH

sed -i 's/third_party[.]download_sccache()/""/g; s/prebuilt[.]load_sccache()/""/g' tools/setup.py
sed -i 's/prebuilt[.]load()/""/g; s/prebuilt[.]download_v8_prebuilt()/""/g' tools/setup.py

test -x /usr/bin/ninja || {{ echo "prepare.sh: /usr/bin/ninja is missing from the base image"; exit 1; }}
printf '#!/bin/sh\\nexec /usr/bin/ninja -j 4 "$@"\\n' > third_party/depot_tools/ninja
chmod 0755 third_party/depot_tools/ninja

if grep -rq "use_prebuilt_v8" libdeno/deno.gni core/libdeno/deno.gni 2>/dev/null; then
    echo "prepare.sh: this era defaults to a prebuilt libv8 whose bucket no longer serves; building v8 from source instead"
    export DENO_BUILD_ARGS="use_prebuilt_v8=false"
fi

if [ "$ARCH" = "amd64" ]; then
    python2 ./tools/setup.py
else
    git clone -q https://gn.googlesource.com/gn /tmp/gn
    GN_REV=$(grep -oE 'git_revision:[0-9a-f]{{40}}' tools/third_party.py | head -1 | cut -d: -f2)
    if [ -z "$GN_REV" ]; then
        BASE_DATE=$(git log -1 --format=%cI HEAD)
        test -n "$BASE_DATE" || {{ echo "prepare.sh: could not read the base commit date"; exit 1; }}
        GN_REV=$(git -C /tmp/gn rev-list -1 --before="$BASE_DATE" HEAD)
        echo "prepare.sh: tools/third_party.py pins no gn revision; using gn as of $BASE_DATE"
    fi
    test -n "$GN_REV" || {{ echo "prepare.sh: could not determine a gn revision to build"; exit 1; }}
    echo "prepare.sh: building gn $GN_REV from source for $ARCH"
    git -C /tmp/gn checkout -q "$GN_REV"
    ( cd /tmp/gn && export CC=gcc CXX=g++ AR=ar CFLAGS="-pthread" CXXFLAGS="-pthread" LDFLAGS="-pthread" \
        && (python2 build/gen.py || python3 build/gen.py) \
        && sed -i 's/ -Wl,--icf=all//g' out/build.ninja \
        && /usr/bin/ninja -C out gn ) \
        || {{ echo "prepare.sh: gn source build failed for $ARCH"; exit 1; }}
    install -m 0755 /tmp/gn/out/gn /usr/local/bin/gn
    rm -rf /tmp/gn
    /usr/local/bin/gn --version

    @@RING_PATCH@@

    @@ARCH_SHIM@@

    printf '#!/bin/sh\nexec /usr/local/bin/gn "$@"\n' > third_party/depot_tools/gn
    chmod 0755 third_party/depot_tools/gn
    export DENO_GN_PATH=/usr/local/bin/gn
    export DENO_BUILD_ARGS="$DENO_BUILD_ARGS is_clang=false use_lld=true use_sysroot=false use_custom_libcxx=false is_official_build=false treat_warnings_as_errors=false symbol_level=0 rust_treat_warnings_as_errors=false"
    export RUSTFLAGS="-C link-arg=-lstdc++ -C link-arg=-fuse-ld=lld"
    python2 ./tools/setup.py --no-binary-download
fi

git checkout -- tools/setup.py

{build_block}
test "$BUILD_RC" = "0" || {{ echo "prepare.sh: deno build failed with rc=$BUILD_RC"; exit 1; }}

test -x target/release/deno || {{ echo "prepare.sh: deno binary was not produced"; exit 1; }}
./target/release/deno --version 2>/dev/null || ./target/release/deno version 2>/dev/null || echo "prepare.sh: deno built but version query unsupported at this era"

""".format(
                    pr=self.pr,
                    repo_dir=REPO_DIR,
                    build_block=_BUILD_BLOCK,
                    toolchain_block=_TOOLCHAIN_BLOCK,
                )
                .replace("@@RING_PATCH@@", _heredoc("MSBRING", _RING_ARM64_PATCH))
                .replace("@@ARCH_SHIM@@", _heredoc("MSBARCH", _ARCH_SHIM)),
            ),
            File(
                ".",
                "run.sh",
                _stage_script(self.pr, ""),
            ),
            File(
                ".",
                "test-run.sh",
                _stage_script(self.pr, "/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                _stage_script(self.pr, "/home/test.patch /home/fix.patch"),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{REPO_DIR}

{hardening}

RUN bash /home/prepare.sh

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MARKER_RE = re.compile(r"^MSB_TESTFILE (\S+) (\S+)$")
_PERM_SUFFIX_RE = re.compile(r"_perm[A-Z0-9]+$")
_MODERN_PASS_RE = re.compile(r"^OK\s+(\S+)\s+\(\d+(?:\.\d+)?ms\)$")
_MODERN_FAIL_RE = re.compile(r"^FAILED\s+(\S+)$")
_INLINE_RE = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|unresolved)\b")
_PENDING_NAME_RE = re.compile(r"^test\s+(\S+)$")
_PENDING_RESULT_RE = re.compile(r"^\.\.\.\s+(ok|FAILED)\b")
_RUNNING_RE = re.compile(r"^RUNNING\s+\S+")


def _parse_deno_unit_test_log(test_log: str) -> TestResult:
    name_to_file: dict[str, str] = {}
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    lines = []
    for raw in test_log.splitlines():
        clean = _ANSI_RE.sub("", raw).replace("\r", "").strip()
        if not clean:
            continue
        lines.append(clean)
        marker = _MARKER_RE.match(clean)
        if marker:
            name_to_file.setdefault(marker.group(2), marker.group(1))

    def test_id(raw_name: str) -> str:
        name = _PERM_SUFFIX_RE.sub("", raw_name)
        path = name_to_file.get(name)
        return f"{path}::{name}" if path else name

    def record(raw_name: str, outcome: str) -> None:
        if outcome == "ok":
            passed_tests.add(test_id(raw_name))
        else:
            failed_tests.add(test_id(raw_name))

    pending: Optional[str] = None
    for clean in lines:
        if _RUNNING_RE.match(clean) or clean.startswith("test result:"):
            pending = None
            continue

        match = _MODERN_PASS_RE.match(clean)
        if match:
            record(match.group(1), "ok")
            pending = None
            continue

        match = _MODERN_FAIL_RE.match(clean)
        if match:
            record(match.group(1), "FAILED")
            pending = None
            continue

        match = _INLINE_RE.match(clean)
        if match:
            record(match.group(1), match.group(2))
            pending = None
            continue

        match = _PENDING_NAME_RE.match(clean)
        if match:
            pending = match.group(1)
            continue

        match = _PENDING_RESULT_RE.match(clean)
        if match and pending is not None:
            record(pending, match.group(1))
            pending = None

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


class _DenoInstanceBase(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return _ImageDefault(self.pr, self._config)

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
        return _parse_deno_unit_test_log(test_log)


@Instance.register("denoland", "deno_2952_to_1281")
class Deno2952To1281(_DenoInstanceBase):
    pass


@Instance.register("denoland", "deno")
class DenoDefault(_DenoInstanceBase):
    pass

