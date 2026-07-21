"""pixijs/pixijs harness (PRs #8536-#11960).

Two-level image layout, aligned with multi_swe_bench.harness.image:

  * ``PixijsImageBase`` -- toolchain only (node:20-bookworm + apt runtime deps
    for headless electron/jest).  It returns a *string* dependency, so
    ``DockerfileEnhancer`` engages and prepends the standard infrastructure
    block (ARG TARGETARCH / REPO_URL / BASE_COMMIT, ENV, OCI LABEL).  It
    deliberately performs **no** ``git clone``: a shared base that clones is
    rewritten by ``DockerfileEnhancer._standardize_repo_fetch`` into
    clone + ``git checkout ${BASE_COMMIT}`` + hardening, which pins the one
    shared ``:base`` tag to whichever PR happened to build it first and strips
    every other commit out of the object store.  Because the base carries no
    git tokens, ``_inject_final_sanitize`` stays inert and the layer is safe to
    share across all PRs.

  * ``PixijsImageDefault`` -- per-PR.  Its dependency is an ``Image``, so the
    enhancer returns the Dockerfile verbatim and ``build_dataset`` passes no
    build args (both only apply to string dependencies).  Everything therefore
    has to be self-embedded here: literal ``ARG REPO_URL``/``ARG BASE_COMMIT``
    defaults, the clone, the checkout, ``Image._HARDENING_BLOCK`` verbatim, and
    the trailing ``CMD``.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "pixijs"

# Node majors pre-cached in the base image.  pixijs spans four toolchains across
# this dataset and the tree itself says which one it wants (.nvmrc), so the base
# carries all of them and use_node.sh selects per checkout -- see _USE_NODE_SH.
# A hardcoded PR-number split cannot work here: the eras overlap (the jest era
# runs 8536-10430 while the .nvmrc=v18 era runs 10277-10557).
NODE_VERSIONS = {
    "16": "16.20.2",
    "18": "18.20.8",
    "20": "20.19.5",
    "24": "24.9.0",
}
DEFAULT_NODE = "20"


def _filter_binary_patches(patch_content: str) -> str:
    """Drop binary file sections from a patch.

    The dataset's patches carry binary diffs (visual-regression PNGs) without
    full index lines, so `git apply` rejects the whole patch with "cannot apply
    binary patch to '...' without full index line".  Stripping those sections
    lets the text hunks -- the ones that actually carry the tests -- apply.
    """
    if not patch_content:
        return patch_content
    lines = patch_content.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1

    out = "\n".join(result)
    # A patch whose last section was binary loses its trailing newline when that
    # section is dropped, and `git apply` rejects the result outright:
    #   error: corrupt patch at line <last>
    # That silently cost pr-11102 its whole test patch -- every stage ran the
    # unpatched tree, so run/test/fix were identical and the report failed
    # Gate 3 ("no test cases transitioned from failed to passed").
    if out and not out.endswith("\n"):
        out += "\n"
    return out


# Selects the Node major for the checked-out tree and puts it on PATH.  Sourced
# (not executed) by every stage script so the toolchain that installed
# node_modules is the same one that runs the tests.
_USE_NODE_SH = """#!/bin/bash
# Pick the Node major this checkout asks for. Sourced, never executed.

_node_major=__DEFAULT_NODE__

if [ -f .nvmrc ]; then
    _v=$(tr -dc '0-9.\\n' < .nvmrc | head -1 | cut -d. -f1)
    [ -n "${_v}" ] && _node_major="${_v}"
elif grep -qE '"(floss|gulp)"' package.json 2>/dev/null; then
    # Pre-jest era (floss/gulp, ~2017-2019): modern Node breaks node-gyp and
    # the old electron toolchain, and these trees carry no .nvmrc.
    _node_major=16
fi

case "${_node_major}" in
    16|18|20|24) ;;
    *) _node_major=__DEFAULT_NODE__ ;;
esac

if [ -d "/opt/node-${_node_major}/bin" ]; then
    export PATH="/opt/node-${_node_major}/bin:${PATH}"
fi
echo "use_node: node major ${_node_major} -> $(node --version 2>/dev/null || echo MISSING)"
""".replace("__DEFAULT_NODE__", DEFAULT_NODE)


# npm install used by every stage.
#   --engine-strict=false : pixijs ships .npmrc with `engine-strict = true`, so a
#       single transitive dep whose engines predate the running Node aborts the
#       whole install (this emptied node_modules entirely on the v7 era). The CLI
#       flag overrides .npmrc. Stripping root package.json engines does NOT help
#       -- the offending engines live in dependencies -- and rewriting
#       package.json also made every fix-patch hunk touching it conflict.
_NPM_INSTALL = (
    "npm install --legacy-peer-deps --engine-strict=false --ignore-scripts "
    "--no-audit --no-fund 2>&1 || true"
)

# --ignore-scripts (above) is deliberate: it skips native builds like `gl` that
# need a GPU/GL toolchain and are not needed for the test suites. But it also
# skips electron's postinstall, which IS the binary download -- and pixijs runs
# jest inside electron (jest-electron / @pixi/jest-electron) across most eras,
# with floss doing the same on the older ones. Without the binary every stage
# dies with "Electron failed to install correctly" and reports (0, 0, 0).
# So: skip every postinstall, then explicitly run electron's own installer.
# Pre-caching it at image-build time also keeps the download out of the eval.
_ELECTRON_INSTALL = """
find node_modules -path '*/electron/install.js' -print0 2>/dev/null |
  while IFS= read -r -d '' _ei; do
    ( cd "$(dirname "${_ei}")" && node install.js ) >/dev/null 2>&1 || true
  done
"""

# Build the workspace. NOT an `||` chain: `lerna run build` exits 0 with
# "No packages found with the lifecycle script 'build'" on the v5-era layout,
# which short-circuited the fallback and left lib/ empty -- so those tests had
# nothing to import. Prefer the root build script; only use lerna when the root
# has none.
_BUILD = """
if grep -q '"build"[[:space:]]*:' package.json 2>/dev/null; then
    npm run build 2>&1 || true
elif [ -f "lerna.json" ]; then
    npx lerna run build --stream 2>&1 || true
fi
"""

# Test invocation shared by run/test-run/fix-run. Ordered most-specific first and
# driven by what the tree actually declares, rather than guessing a runner:
#   * scripts/test.mts (v8 era) -- run the `unit` selector only; a bare
#     `npm test` there also runs lint/types/index/prune, whose failures are not
#     test signal and would swamp the log.
#   * test:unit (run-s eras) -- the unit half of `run-s test:unit test:scene`.
#   * local jest binary (v7 era).
#   * npm test (floss/gulp eras) -- uses the repo's own runner via xvfb.
#   * unit-test (v4/v5 eras) -- runs floss directly. Preferred over `npm test`
#     because npm fires the `pretest` hook, and on those eras pretest is
#     `npm run lint && npm run build`, whose jsdoc/eslint chain crashes on any
#     modern Node and buries the actual test run.
#
# JEST_FLAGS is the difference between a stage that reports and one that hangs
# forever:
#   --forceExit : electron and the jest-electron http-server leave open handles,
#       so jest sits idle FOREVER after "Ran all test suites". The harness only
#       captures container output on exit, so a completed run looked identical to
#       a hang -- 0% CPU, no output, killed after hours. This is what actually
#       wedged the earlier runs.
#   --runInBand : without it jest forks one worker per core (32 here) and EACH
#       spawns its own Electron. The thrashing is what made the suite look
#       hours-long. Serialised onto a single Electron the full 145-suite run
#       takes 99 SECONDS. Faster and deterministic -- run/test/fix stages then
#       execute the same suites in the same order, which is what report.py's
#       three-way comparison relies on.
# Only applied where the runner IS jest: floss (v4/v5) rejects these flags.
_JEST_FLAGS = "--runInBand --forceExit"

_RUN_TESTS = """
if [ -f scripts/test.mts ] && grep -q '"test":.*test\\.mts' package.json 2>/dev/null; then
    node ./scripts/test.mts unit __JEST_FLAGS__ 2>&1 || true
elif grep -q '"test:unit"[[:space:]]*:' package.json 2>/dev/null; then
    npm run test:unit -- __JEST_FLAGS__ 2>&1 || true
elif [ -x ./node_modules/.bin/jest ]; then
    ./node_modules/.bin/jest --silent __JEST_FLAGS__ 2>&1 || true
elif grep -q '"unit-test"[[:space:]]*:' package.json 2>/dev/null; then
    npm run unit-test 2>&1 || true
else
    npm test 2>&1 || true
fi
""".replace("__JEST_FLAGS__", _JEST_FLAGS)


class PixijsImageBase(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Pre-cache every Node major the dataset needs so no stage has to reach
        # the network for a toolchain. TARGETARCH is supplied by BuildKit (the
        # enhancer declares the ARG), which keeps this correct under the
        # linux/arm64,linux/amd64 multi-arch build.
        node_installs = " \\\n    ".join(
            f'"{major}:{full}"' for major, full in sorted(NODE_VERSIONS.items())
        )

        # NOTE: no repository fetch of any kind belongs in this layer -- see the
        # module docstring.  Keep this Dockerfile free of "git clone",
        # "git fetch" and "git remote add" tokens.
        return """FROM {image_name}

{global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
ENV CI=true
ENV JEST_ELECTRON_NO_SANDBOX=1
ENV DISPLAY=:99

# Electron reads extra Chromium switches from this variable. jest-electron owns
# the launch call, so this is the only way to reach those flags.
#   --disable-gpu / --disable-software-rasterizer: the gpu-process came up under
#       ANGLE+swiftshader on headless arm64, burned 23s of CPU and then stalled
#       with jest parked in ep_poll -- a hang, not slow progress.
#   --disable-dev-shm-usage: the harness runs `docker run` without --shm-size, so
#       /dev/shm is Docker's 64MB default; this routes Chromium off it.
#   --no-sandbox: no user namespaces in the build container.
ENV ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --no-sandbox"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    python3 \\
    xz-utils \\
    xvfb \\
    libgtk-3-0 \\
    libgtk2.0-0 \\
    libgconf-2-4 \\
    libxi6 \\
    libnotify4 \\
    libnss3 \\
    libxss1 \\
    libxtst6 \\
    xauth \\
    libgbm1 \\
    libasound2 \\
    libatk-bridge2.0-0 \\
    libdrm2 \\
    libxkbcommon0 \\
    libxcomposite1 \\
    libxdamage1 \\
    libxfixes3 \\
    libxrandr2 \\
    libpango-1.0-0 \\
    libcairo2 \\
    libcups2 \\
    libdbus-1-3 \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "${{TARGETARCH:-arm64}}" in \\
        arm64) NARCH=arm64 ;; \\
        amd64) NARCH=x64 ;; \\
        *)     NARCH=x64 ;; \\
    esac; \\
    for spec in {node_installs}; do \\
        major="${{spec%%:*}}"; full="${{spec#*:}}"; \\
        curl -fsSL "https://nodejs.org/dist/v${{full}}/node-v${{full}}-linux-${{NARCH}}.tar.xz" \\
            -o /tmp/node.tar.xz; \\
        mkdir -p "/opt/node-${{major}}"; \\
        tar -xJf /tmp/node.tar.xz -C "/opt/node-${{major}}" --strip-components=1; \\
        rm -f /tmp/node.tar.xz; \\
        "/opt/node-${{major}}/bin/node" --version; \\
    done

RUN npm install -g http-server

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            global_env=self.global_env,
            clear_env=self.clear_env,
            node_installs=node_installs,
        )


class PixijsImageDefault(Image):
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
        return PixijsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        def sh(body: str) -> str:
            """Placeholder substitution -- .format() would mangle every ${...}."""
            return (
                body.replace("__REPO__", REPO_DIR)
                .replace("__NPM_INSTALL__", _NPM_INSTALL)
                .replace("__ELECTRON_INSTALL__", _ELECTRON_INSTALL.strip("\n"))
                .replace("__BUILD__", _BUILD.strip("\n"))
                .replace("__RUN_TESTS__", _RUN_TESTS.strip("\n"))
            )

        return [
            File(
                ".",
                "fix.patch",
                _filter_binary_patches(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _filter_binary_patches(self.pr.test_patch),
            ),
            File(".", "use_node.sh", _USE_NODE_SH),
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
                sh("""#!/bin/bash
set -e

cd /home/__REPO__

# The checkout to BASE_COMMIT and the history strip happen at build time in the
# Dockerfile, so the tree must already be clean and pinned here.
bash /home/check_git_changes.sh

source /home/use_node.sh

__NPM_INSTALL__

__ELECTRON_INSTALL__

# Fail loudly. A silent dependency-install failure previously produced an image
# that built "successfully" and only surfaced as an all-zero report much later.
if [ ! -d node_modules ]; then
    echo "prepare: FATAL - npm install produced no node_modules"
    exit 1
fi

# Build packages if this is a workspace project.
__BUILD__

# The tree must still match BASE_COMMIT so the patches apply cleanly.
git checkout -- . 2>/dev/null || true
"""),
            ),
            File(
                ".",
                "run.sh",
                sh("""#!/bin/bash
set -uo pipefail

cd /home/__REPO__

source /home/use_node.sh

Xvfb :99 -screen 0 1024x768x24 &
sleep 1
__RUN_TESTS__
"""),
            ),
            File(
                ".",
                "test-run.sh",
                sh("""#!/bin/bash
set -uo pipefail

cd /home/__REPO__

source /home/use_node.sh

git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --3way /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

__NPM_INSTALL__

__ELECTRON_INSTALL__

__BUILD__

Xvfb :99 -screen 0 1024x768x24 &
sleep 1
__RUN_TESTS__
"""),
            ),
            File(
                ".",
                "fix-run.sh",
                sh("""#!/bin/bash
set -uo pipefail

cd /home/__REPO__

source /home/use_node.sh

git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --3way /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --3way /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

__NPM_INSTALL__

__ELECTRON_INSTALL__

__BUILD__

Xvfb :99 -screen 0 1024x768x24 &
sleep 1
__RUN_TESTS__
"""),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Validated before interpolation into RUN/WORKDIR paths and the clone
        # URL, mirroring image.py, so a name carrying shell metacharacters
        # cannot inject commands into the generated build.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        repo_dir = _safe_path_component(REPO_DIR, "repo_dir")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR /home
RUN git clone "${{REPO_URL}}" /home/{repo_dir}

WORKDIR /home/{repo_dir}
RUN git fetch --no-tags origin "${{BASE_COMMIT}}" || true
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


# The dataset now carries number_interval as the dash-joined prs_in_bundle
# (e.g. "10309-10312-10313-..."), so Instance.create looks up one distinct key
# per row -- 105 of them.  Those are dispatched by the range shim at the bottom
# of this module rather than by 105 register() calls.  The two literal keys below
# are kept for back-compat: "pixijs_11960_to_8536" was the previous era-style
# value, and the bare "pixijs" is the fallback Instance.create computes whenever
# number_interval is empty.
@Instance.register("pixijs", "pixijs")
@Instance.register("pixijs", "pixijs_11960_to_8536")
class Pixijs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PixijsImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Jest individual test patterns
        # ✓ test name (Nms)
        re_jest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        # ✕ test name (Nms)
        re_jest_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        # ○ skipped test name
        re_jest_skip = re.compile(r"^\s*○\s+(.+)$")

        # Jest file-level PASS/FAIL lines
        re_jest_file_pass = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:s|ms)\))?$")
        re_jest_file_fail = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:s|ms)\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_jest_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_jest_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

        # Fail wins over pass
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval routing (dash-joined prs_in_bundle) -- registry-scoped logic.
#
# The dataset ships number_interval as the dash-joined PR bundle
# ("10309-10312-10313-...", i.e. instance_id minus the "pixijs__pixijs-" prefix),
# which is unique per row -- 105 distinct values.  Instance.create looks that up
# verbatim as "<org>/<number_interval>", so without this shim every row raises
# "Instance 'pixijs/10309-10312-...' is not registered".
#
# Routing is expressed as LOGIC rather than a hardcoded list of 105 bundle
# strings, so the registry needs no edit when the bundling is regenerated.  This
# module is the only pixijs registry, so every bundle resolves to Pixijs; the
# anchor (lowest PR in the bundle) is computed anyway so that splitting a second
# era out later only means adding entries to _pixijs_eras.
#
# The shim is idempotent and scoped to pixijs/pixijs -- it delegates to the
# previous Instance.create first and only handles the ValueError for this repo,
# so the other registries that wrap Instance.create are unaffected.
# ---------------------------------------------------------------------------

# Upper PR bound -> Instance class, ascending. One era today: everything.
Instance._pixijs_eras = getattr(Instance, "_pixijs_eras", {})
Instance._pixijs_eras[11960] = Pixijs


def _pixijs_pick_era(pr):
    eras = getattr(Instance, "_pixijs_eras", {})
    if not eras:
        return None
    ni = getattr(pr, "number_interval", "") or ""
    anchors = [int(tok) for tok in ni.split("-") if tok.isdigit()]
    anchor = min(anchors) if anchors else getattr(pr, "number", None)
    if anchor is None:
        return None
    for hi in sorted(eras):
        if anchor <= hi:
            return eras[hi]
    return eras[max(eras)]


if not getattr(Instance, "_pixijs_route_shim", False):
    _pixijs_orig_create = Instance.create.__func__

    def _pixijs_create(cls, pr, config, *args, **kwargs):
        try:
            return _pixijs_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == "pixijs" and getattr(pr, "repo", "") == "pixijs":
                era = _pixijs_pick_era(pr)
                if era is not None:
                    return era(pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_pixijs_create)
    Instance._pixijs_route_shim = True
