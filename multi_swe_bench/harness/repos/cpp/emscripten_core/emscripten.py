from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# emscripten-core/emscripten  (lang: cpp — emsdk/clang toolchain, Python-driven
# unittest suite via tests/runner.py).
#
# Conformed to the updated harness (multi_swe_bench/harness/image.py):
#   * Two-tier images.  The heavy toolchain (emsdk + LLVM + node) is built ONCE
#     per era in a shared `:base-<era>` image and reused by every PR image in
#     that era.  Because the PR image's dependency() returns an Image (not a
#     str), build_dataset does NOT inject REPO_URL/BASE_COMMIT and the
#     DockerfileEnhancer is bypassed — so both tiers carry the
#     `# syntax=docker/dockerfile:1.6` directive and emit the standard infra
#     block (proxy / TZ / cert symlinks / MITM mount / OCI labels) themselves,
#     matching the in-tree cpp house style (see google/flatbuffers, envoyproxy).
#   * Anti-reward-hacking: the PR image's prepare.sh reproduces
#     Image._HARDENING_BLOCK on the checked-out HEAD — it detaches at the base
#     commit, strips every other ref/remote/reflog, gc-prunes unreachable
#     objects, and asserts (fail-closed) that no history beyond BASE_COMMIT
#     survives.  This closes the git-history read path to the fix commit that a
#     plain `git checkout <sha>` (as flatbuffers/envoy use) leaves open.
#
# Routing: dataset rows carry no `number_interval`/`tag`, so Instance.create()
# resolves them to the bare key "emscripten-core/emscripten".  We register ONE
# Instance under that key and dispatch to the era image internally by pr.number.
#
# Era boundaries by PR number:
#   [0,     9999 ]  ubuntu:18.04, node14, python2+3, tests/runner.py
#   [10000, 14999]  ubuntu:20.04, node16, python3,   tests/runner.py + llc shim
#   [15000, 22999]  ubuntu:22.04, node18, python3,   test/runner.py (suite auto)
#   [23000, +inf ]  ubuntu:24.04, node20, python3,   test/runner.py + bootstrap
# ---------------------------------------------------------------------------


def _sub(text: str, repo: str, sha: str) -> str:
    return text.replace("@@REPO@@", repo).replace("@@SHA@@", sha)


def _infra_block(org: str, repo: str) -> str:
    """Standard build ARGs / ENV / OCI labels emitted for both image tiers.

    Emitted by the config itself (the syntax directive + Image dependency make
    the DockerfileEnhancer pass the Dockerfile through untouched). Proxy build
    args, CA-certificate symlinks and the MITM secret mount are intentionally
    omitted — this repo does not use proxy or certificate injection.
    """
    repo_url = f"https://github.com/{org}/{repo}.git"
    return (
        "ARG TARGETARCH\n"
        f'ARG REPO_URL="{repo_url}"\n'
        "ARG BASE_COMMIT\n"
        "\n"
        "ENV DEBIAN_FRONTEND=noninteractive \\\n"
        "    LANG=C.UTF-8 \\\n"
        "    TZ=UTC\n"
        "\n"
        f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
        f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
        f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
        f'      org.opencontainers.image.authors="https://www.ethara.ai/"\n'
    )


def _prepare_sh(repo: str, era_setup: str) -> str:
    # Only the era-specific emscripten setup (write .emscripten, llc shim,
    # npm/pip/bootstrap). The repo clone, base-commit checkout, and git-history
    # hardening (the canonical Image._HARDENING_BLOCK) live in the PR Dockerfile,
    # mirroring the cloudwego/eino structure:
    #   clone -> checkout ${BASE_COMMIT} -> COPY -> prepare.sh -> hardening.
    header = "#!/bin/bash\nset -e\ncd /home/@@REPO@@\n"
    return _sub(header + era_setup, repo, "")


# =========================================================================
#  Shared image renderers
# =========================================================================
class _EmscriptenImageBase(Image):
    """Per-era shared base: toolchain + emsdk + node (multi-arch, no repo clone).

    Subclasses set UBUNTU / NODE_VERSION / APT_EXTRA / BASE_TAG.
    """

    UBUNTU = "ubuntu:22.04"
    NODE_VERSION = ""
    APT_EXTRA: list[str] = []
    BASE_TAG = "base"

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
        return self.UBUNTU

    def image_tag(self) -> str:
        return self.BASE_TAG

    def workdir(self) -> str:
        return self.BASE_TAG

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return self.APT_EXTRA

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, self.UBUNTU)

        emsdk_run = (
            "RUN git clone https://github.com/emscripten-core/emsdk.git /emsdk && \\\n"
            "    cd /emsdk && \\\n"
            "    ./emsdk install latest && \\\n"
            "    ./emsdk activate latest && \\\n"
            "    rm -rf /emsdk/emscripten"
        )
        env_lines = (
            'ENV PATH="/emsdk:/emsdk/upstream/emscripten:/emsdk/upstream/bin:/emsdk/node/current/bin:${PATH}"\n'
            'ENV EMSDK="/emsdk"\n'
            'ENV EM_CONFIG="/emsdk/.emscripten"'
        )

        # Multi-arch node: select the tarball matching the (possibly QEMU-emulated)
        # container architecture, so linux/amd64 and linux/arm64 both build.
        # `dpkg --print-architecture` -> amd64|arm64; node uses x64|arm64.
        # emsdk `install latest` auto-detects the arch, so it needs no mapping.
        node_run = (
            "RUN set -eux; \\\n"
            '    case "$(dpkg --print-architecture)" in \\\n'
            "      amd64) NODE_ARCH=x64 ;; \\\n"
            "      arm64) NODE_ARCH=arm64 ;; \\\n"
            "      *) NODE_ARCH=arm64 ;; \\\n"
            "    esac; \\\n"
            f'    curl -fsSL "https://nodejs.org/dist/{self.NODE_VERSION}/node-{self.NODE_VERSION}-linux-${{NODE_ARCH}}.tar.xz" \\\n'
            "    | tar -xJ -C /usr/local --strip-components=1"
        )

        # NOTE: the base is toolchain-only and does NOT clone the repo — the
        # per-PR image clones it (mirroring cloudwego/eino), so the shared base
        # stays PR-agnostic and reusable across every PR in the era.
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {self.UBUNTU}",
            _infra_block(org, repo).rstrip("\n"),
            "WORKDIR /home/",
            apt_command,
            node_run,
            emsdk_run,
            env_lines,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class _EmscriptenImageDefault(Image):
    """Per-era PR image: FROM shared base, checkout + harden + era setup.

    Subclasses set BASE_CLS and implement _era_files().
    """

    BASE_CLS: type[_EmscriptenImageBase] = _EmscriptenImageBase

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
        return self.BASE_CLS(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _era_files(self) -> list[File]:
        raise NotImplementedError

    def files(self) -> list[File]:
        return self._era_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        org, repo = self.pr.org, self.pr.repo
        sha = self.pr.base.sha

        # Single COPY of all patches/scripts into /home/ (inline template style).
        copy_files = " ".join(f.name for f in self.files())

        # PR image (dependency() is an Image, so the DockerfileEnhancer leaves it
        # verbatim): clone full history, check out ${BASE_COMMIT} inline, COPY the
        # scripts, run the era setup, then the canonical Image._HARDENING_BLOCK
        # strips origin/refs/future history (with post-condition asserts +
        # submodule pass) while keeping base.sha reachable.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


# =========================================================================
#  Era 1: 0 .. 9999   (ubuntu:18.04, node14, python2)
# =========================================================================
class _Base0(_EmscriptenImageBase):
    UBUNTU = "ubuntu:18.04"
    NODE_VERSION = "v14.21.3"
    APT_EXTRA = ["cmake", "python", "python-pip", "xz-utils"]
    BASE_TAG = "base-0_to_9999"


_ERA0_SETUP = """NODE_BIN=$(find /emsdk/node -name node -type f 2>/dev/null | head -1)
# Create llc symlink (old emscripten checks for it)
ln -sf /emsdk/upstream/bin/clang /emsdk/upstream/bin/llc 2>/dev/null || true
cat > /home/@@REPO@@/.emscripten << 'EMCONFIG'
import os
EMSCRIPTEN_ROOT = '/home/@@REPO@@'
LLVM_ROOT = '/emsdk/upstream/bin'
BINARYEN_ROOT = '/emsdk/upstream'
NODE_JS = '/usr/local/bin/node'
COMPILER_ENGINE = NODE_JS
JS_ENGINES = [NODE_JS]
EMCONFIG
npm install || true
"""

_ERA0_RUN = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
python tests/runner.py other
"""

_ERA0_TEST = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
python tests/runner.py other
"""

_ERA0_FIX = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
python tests/runner.py other
"""


class _Default0(_EmscriptenImageDefault):
    BASE_CLS = _Base0

    def _era_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", _prepare_sh(repo, _ERA0_SETUP)),
            File(".", "run.sh", _sub(_ERA0_RUN, repo, "")),
            File(".", "test-run.sh", _sub(_ERA0_TEST, repo, "")),
            File(".", "fix-run.sh", _sub(_ERA0_FIX, repo, "")),
        ]


# =========================================================================
#  Era 2: 10000 .. 14999   (ubuntu:20.04, node16, python3, llc wrapper)
# =========================================================================
class _Base10000(_EmscriptenImageBase):
    UBUNTU = "ubuntu:20.04"
    NODE_VERSION = "v16.20.2"
    APT_EXTRA = ["cmake", "python3-pip", "python3-venv", "xz-utils"]
    BASE_TAG = "base-10000_to_14999"


_ERA10000_SETUP = r"""NODE_BIN=$(find /emsdk/node -name node -type f 2>/dev/null | head -1)
# Create llc wrapper (v1.39 parses 'llc --version' expecting LLVM format)
cat > /emsdk/upstream/bin/llc << 'LLCWRAPPER'
#!/bin/bash
if [ "$1" = "--version" ]; then
  CLANG_VER=$(/emsdk/upstream/bin/clang --version 2>&1 | head -1 | grep -oP '\d+\.\d+\.\d+')
  echo "LLVM (http://llvm.org/):"
  echo "  LLVM version $CLANG_VER"
  echo "  Default target: wasm32-unknown-unknown"
  echo "  Host CPU: generic"
  echo ""
  echo "  Registered Targets:"
  echo "    wasm32     - WebAssembly 32-bit"
  echo "    wasm64     - WebAssembly 64-bit"
  exit 0
fi
exec /emsdk/upstream/bin/clang "$@"
LLCWRAPPER
chmod +x /emsdk/upstream/bin/llc
cat > /home/@@REPO@@/.emscripten << EMCONFIG
import os
EMSCRIPTEN_ROOT = '/home/@@REPO@@'
LLVM_ROOT = '/emsdk/upstream/bin'
BINARYEN_ROOT = '/emsdk/upstream'
NODE_JS = '/usr/local/bin/node'
JS_ENGINES = [NODE_JS]
EMCONFIG
npm install || true
"""

_ERA10000_RUN = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
python3 tests/runner.py other
"""

_ERA10000_TEST = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
python3 tests/runner.py other
"""

_ERA10000_FIX = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
python3 tests/runner.py other
"""


class _Default10000(_EmscriptenImageDefault):
    BASE_CLS = _Base10000

    def _era_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", _prepare_sh(repo, _ERA10000_SETUP)),
            File(".", "run.sh", _sub(_ERA10000_RUN, repo, "")),
            File(".", "test-run.sh", _sub(_ERA10000_TEST, repo, "")),
            File(".", "fix-run.sh", _sub(_ERA10000_FIX, repo, "")),
        ]


# =========================================================================
#  Era 3: 15000 .. 22999   (ubuntu:22.04, node18, python3, suite auto-detect)
# =========================================================================
class _Base15000(_EmscriptenImageBase):
    UBUNTU = "ubuntu:22.04"
    NODE_VERSION = "v18.20.8"
    APT_EXTRA = ["cmake", "ninja-build", "python3-pip", "python3-venv", "xz-utils"]
    BASE_TAG = "base-15000_to_22999"


_ERA15000_SETUP = """NODE_BIN=$(find /emsdk/node -name node -type f 2>/dev/null | head -1)
cat > /home/@@REPO@@/.emscripten << EMCONFIG
NODE_JS = '/usr/local/bin/node'
LLVM_ROOT = '/emsdk/upstream/bin'
BINARYEN_ROOT = '/emsdk/upstream'
EMSCRIPTEN_ROOT = '/home/@@REPO@@'
EMCONFIG
pip3 install --break-system-packages psutil || pip3 install psutil || true
npm install || true
"""

_ERA15000_RUN = r"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
if [ -f test/runner.py ]; then
  SUITES=""
  if grep -q "test_core\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES core2"; fi
  if grep -q "test_other\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES other"; fi
  if grep -q "test_browser\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES browser"; fi
  if [ -z "$SUITES" ]; then SUITES="other"; fi
  python3 test/runner.py $SUITES
else
  python3 tests/runner.py other
fi
"""

_ERA15000_TEST = r"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
if [ -f test/runner.py ]; then
  SUITES=""
  if grep -q "test_core\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES core2"; fi
  if grep -q "test_other\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES other"; fi
  if grep -q "test_browser\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES browser"; fi
  if [ -z "$SUITES" ]; then SUITES="other"; fi
  python3 test/runner.py $SUITES
else
  python3 tests/runner.py other
fi
"""

_ERA15000_FIX = r"""#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
if [ -f test/runner.py ]; then
  SUITES=""
  if grep -q "test_core\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES core2"; fi
  if grep -q "test_other\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES other"; fi
  if grep -q "test_browser\.py" /home/test.patch 2>/dev/null; then SUITES="$SUITES browser"; fi
  if [ -z "$SUITES" ]; then SUITES="other"; fi
  python3 test/runner.py $SUITES
else
  python3 tests/runner.py other
fi
"""


class _Default15000(_EmscriptenImageDefault):
    BASE_CLS = _Base15000

    def _era_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", _prepare_sh(repo, _ERA15000_SETUP)),
            File(".", "run.sh", _sub(_ERA15000_RUN, repo, "")),
            File(".", "test-run.sh", _sub(_ERA15000_TEST, repo, "")),
            File(".", "fix-run.sh", _sub(_ERA15000_FIX, repo, "")),
        ]


# =========================================================================
#  Era 4: 23000 .. +inf   (ubuntu:24.04, node20, python3, bootstrap)
# =========================================================================
class _Base23000(_EmscriptenImageBase):
    UBUNTU = "ubuntu:24.04"
    NODE_VERSION = "v20.19.2"
    APT_EXTRA = ["cmake", "ninja-build", "python3-pip", "python3-venv", "xz-utils"]
    BASE_TAG = "base-23000_to_99999"


_ERA23000_SETUP = """NODE_BIN=$(find /emsdk/node -name node -type f 2>/dev/null | head -1)
cat > /home/@@REPO@@/.emscripten << EMCONFIG
NODE_JS = '/usr/local/bin/node'
LLVM_ROOT = '/emsdk/upstream/bin'
BINARYEN_ROOT = '/emsdk/upstream'
EMSCRIPTEN_ROOT = '/home/@@REPO@@'
EMCONFIG
pip3 install --break-system-packages psutil || pip3 install psutil || true
./bootstrap 2>/dev/null || python3 bootstrap.py 2>/dev/null || true
"""

_ERA23000_RUN = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
python3 test/runner.py other
"""

_ERA23000_TEST = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
./bootstrap 2>/dev/null || true
python3 test/runner.py other
"""

_ERA23000_FIX = """#!/bin/bash
set -eo pipefail
export EM_CONFIG=/home/@@REPO@@/.emscripten
cd /home/@@REPO@@
git apply --whitespace=nowarn --reject /home/test.patch || true
./bootstrap 2>/dev/null || true
git apply --whitespace=nowarn --reject /home/fix.patch || true
./bootstrap 2>/dev/null || true
python3 test/runner.py other
"""


class _Default23000(_EmscriptenImageDefault):
    BASE_CLS = _Base23000

    def _era_files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", _prepare_sh(repo, _ERA23000_SETUP)),
            File(".", "run.sh", _sub(_ERA23000_RUN, repo, "")),
            File(".", "test-run.sh", _sub(_ERA23000_TEST, repo, "")),
            File(".", "fix-run.sh", _sub(_ERA23000_FIX, repo, "")),
        ]


def _default_for(pr: PullRequest, config: Config) -> Image:
    """Pick the era-appropriate PR image by PR number."""
    n = pr.number
    if n <= 9999:
        return _Default0(pr, config)
    if n <= 14999:
        return _Default10000(pr, config)
    if n <= 22999:
        return _Default15000(pr, config)
    return _Default23000(pr, config)


@Instance.register("emscripten-core", "emscripten")
class EMSCRIPTEN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return _default_for(self.pr, self._config)

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
        re_unittest = re.compile(
            r"^(\S+.*?)[ \t]+\.\.\.[ \t]+(ok|FAIL|ERROR|skipped\b)", re.MULTILINE
        )
        # Resolve same-name collisions by precedence FAIL > PASS > SKIP.
        priority = {"FAIL": 3, "PASS": 2, "SKIP": 1}
        seen: dict[str, str] = {}
        for match in re_unittest.finditer(test_log):
            test_name = match.group(1).strip()
            raw = match.group(2)
            if raw == "ok":
                outcome = "PASS"
            elif raw in ("FAIL", "ERROR"):
                outcome = "FAIL"
            else:
                outcome = "SKIP"
            prev = seen.get(test_name)
            if prev is None or priority[outcome] > priority[prev]:
                seen[test_name] = outcome
        passed_tests = {n for n, s in seen.items() if s == "PASS"}
        failed_tests = {n for n, s in seen.items() if s == "FAIL"}
        skipped_tests = {n for n, s in seen.items() if s == "SKIP"}
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval derivation from `prs_in_bundle`
# ---------------------------------------------------------------------------
# The raw bundle records carry `prs_in_bundle` (e.g. [10659, 10811, ...]) but
# leave `number_interval` unset. The required output form is the EXPLICIT
# dash-joined member list ("10659-10811-...-11051") — NOT a range like
# "10659-11051", which would wrongly imply every PR in between is in the bundle.
#
# `PullRequest.from_json` drops unknown fields, so `prs_in_bundle` never reaches
# the harness and the emitted dataset's `number_interval` comes out empty. Fix
# it in the registry (no edits to the shared harness core) with two
# narrowly-scoped, idempotent monkeypatches, mirroring
# bytecodealliance/wasmtime:
#
#   1. Wrap PullRequest.from_json to STASH the raw `prs_in_bundle` list onto the
#      parsed PR as `_prs_in_bundle`. number_interval is deliberately NOT set
#      here: Instance.create() routes on f"{org}/{number_interval}" and would
#      raise on the (unregistered) dash-joined key, breaking the bare-key +
#      pr.number era dispatch this config relies on. Scoped to emscripten rows.
#
#   2. Wrap Dataset.build (the single output-serialization point, invoked in
#      gen_report AFTER all routing is finished) to fill
#      number_interval = "-".join(prs_in_bundle) from that stash whenever the
#      record's number_interval is still empty. Routing never sees it; only the
#      emitted resolved *_dataset.jsonl record does.
#
# Sentinels are checked against each class's OWN __dict__ (not getattr, which
# sees inherited attrs — Dataset subclasses PullRequest and would otherwise
# inherit the from_json sentinel and skip its own patch).
import json as _json  # noqa: E402

from multi_swe_bench.harness.dataset import Dataset as _Dataset  # noqa: E402


def _emsc_number_interval_from_bundle(bundle) -> str:
    # Explicit member list in original bundle order; dash-joined, de-duplicated
    # while preserving order. Range collapsing is intentionally avoided.
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


if "_emsc_from_json_patch" not in PullRequest.__dict__:
    _emsc_orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _emsc_pr_from_json_with_bundle(cls, json_str):
        pr = _emsc_orig_pr_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "emscripten-core"
                and raw.get("repo") == "emscripten"
                and raw.get("prs_in_bundle")
            ):
                # stash only; number_interval stays "" so routing is unaffected
                pr._prs_in_bundle = list(raw["prs_in_bundle"])
        except Exception:
            pass
        return pr

    PullRequest.from_json = _emsc_pr_from_json_with_bundle
    PullRequest._emsc_from_json_patch = True


if "_emsc_build_patch" not in _Dataset.__dict__:
    _emsc_orig_dataset_build = _Dataset.build.__func__

    @classmethod
    def _emsc_dataset_build_with_interval(cls, pr, report):
        ds = _emsc_orig_dataset_build(cls, pr, report)
        if not getattr(ds, "number_interval", ""):
            bundle = getattr(pr, "_prs_in_bundle", None)
            if bundle:
                ds.number_interval = _emsc_number_interval_from_bundle(bundle)
        return ds

    _Dataset.build = _emsc_dataset_build_with_interval
    _Dataset._emsc_build_patch = True
