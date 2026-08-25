"""microsoft/azuredatastudio harness config.

Azure Data Studio (ADS) is a 2021-era fork of VS Code that ships SQL/data
tooling as first-party extensions under extensions/<name>. Extension features
(e.g. Schema Compare) are tested via a real Electron extension-host launch
(`scripts/code.sh --extensionDevelopmentPath=... --extensionTestsPath=...`),
not the `test/unit/node` core-mocha lane — that lane only covers `src/vs` and
`src/sql` core code and never loads extensions/**/src/test at all.

Toolchain constraints, verified against build/npm/preinstall.js and .yarnrc
at the PR's base commit (2021-04-19, e7e4828):
  - Node must be >=10 and <16 (preinstall.js hard-rejects Node 16+).
  - npm is rejected outright; only yarn (>=1.10.1) is accepted.
  - Native modules build against Electron 9.4.3 headers (root .yarnrc).

Only the schema-compare extension is installed/compiled/tested. Real ADS CI
(scripts/test-extensions-unit.sh) builds and tests all ~40 bundled
extensions in one pass; doing that here for a PR that touches exactly one
extension would multiply image-build cost for no signal gained, so this
config narrows install/compile to root + extensions/ (shared devDeps) +
extensions/schema-compare only. test-extensions-unit.sh also launches each
extension standalone against an empty --extensions-dir, which is why
schema-compare's "Microsoft.mssql" extensionDependencies entry (mssql ships
in-tree, not via marketplace download) does not need to be installed here.

One known consequence, measured rather than assumed. Exactly one test --
"SchemaCompareDialog.openDialog > Simulate ok button- with both endpoints set
to dacpac" -- constructs SchemaCompareMainWindow with an undefined service, so
it falls through to getService(), which reads
getExtension('Microsoft.mssql').exports.schemaCompare. With mssql absent that
is undefined and the test throws. It fails identically in all three stages, so
Report.check()'s classifier skips it (`if test.fix != PASS: continue`); it can
neither fabricate an f2p nor invalidate the report. Confirmed harmless by a
real run: f2p=2, p2p=40, valid=True.

Do not try to "fix" it by adding mssql to the build. Both obvious routes were
tried in a container against this exact image and neither works:

  1. Install extensions/mssql's deps + compile-extension:mssql -> exit 0,
     out/main.js present. Test still fails.
  2. Additionally pre-stage SqlToolsService: 51 MB downloaded from the (still
     live) 2021 release into the precise path mssql's own config.json names,
     ./sqltoolsservice/Linux/3.0.0-release.93, with the .NET binary confirmed
     executable in the container. Test still fails, identically.

With the extension compiled and its service present and runnable, `exports`
is still undefined -- so mssql is never *activated*. That is inherent to the
extension-test launch: code.sh starts an isolated host for the extension named
by --extensionDevelopmentPath against an empty --extensions-dir, and nothing
staged on disk makes it activate a second extension. Fixing this would mean
restructuring the launch away from what ADS CI itself does, to recover a test
that Report.check() discards anyway.

Electron, the compiled `out/` core, and product.json's built-in extensions
are all baked into the image by prepare.sh, and the eval-time launch sets
VSCODE_SKIP_PRELAUNCH=1 so scripts/code.sh does not re-run preLaunch.js.
That matters for reliability: preLaunch's getBuiltInExtensions() step is
not existence-guarded, so with preLaunch enabled an unreachable marketplace
would abort the entire test run (code.sh runs under `set -e`) even though
these tests need none of those extensions.
"""

from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# A leading ISO-8601 date, as VS Code / Electron emit on their log lines.
# Used to keep interleaved log output out of parse_log's suite stack.
_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]")

# Lines that are unmistakably program output rather than a mocha suite title.
# The Electron host prints Angular/renderer stack traces onto the same stream,
# indented by an even number of spaces, so indentation alone cannot separate
# them from suite headers. A stack frame starts with 'at ', and any line
# carrying a source location ('file:///...' or 'foo.js:12:34') is code, not a
# suite name. Real suite titles here look like 'utils: Basic tests to verify
# verifyConnectionAndGetOwnerUri' and match none of these.
_STACK_OR_SOURCE = re.compile(r"^at\s|file://|\.js:\d+")


class AzureDataStudioImageBase(Image):
    """Shared OS layer + the cloned repo pinned to this instance's base commit.

    node:14-bullseye, the apt toolchain, then a clone that DockerfileEnhancer
    expands into checkout ${BASE_COMMIT} plus Image._HARDENING_BLOCK. See
    dockerfile() below for why the clone is written the way it is.

    KNOWN LIMITATION, deliberate. image_tag() is the constant "base", but the
    hardening prunes the repo to a single commit's ancestry (ref purge, `git gc
    --prune=now --aggressive`, and an assert that rev-list --all == rev-list
    HEAD). So despite the shared-looking tag, this image is pinned to whichever
    PR built it first and is NOT genuinely reusable across ADS instances: a
    later PR will not find its own commit inside it.

    That is survivable because prepare.sh carries the mitigation --
    `git cat-file -e <sha> || git fetch <url> <sha>` -- so a PR whose commit is
    absent pulls just that object over the network instead of failing. The cost
    is one network fetch per such PR, not a broken build. If ADS ever gets a
    real batch of instances, the fix is a per-era tag (base-<range>) rather than
    a constant one; the harness's own convention is base-pr-<N>.

    dependency() returns a str, so the enhancer does engage here and supplies
    the syntax directive, TARGETARCH/proxy ARGs, CA-cert plumbing and labels --
    none of which are hand-written below.
    """

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
        # Node 14: build/npm/preinstall.js rejects Node <10 or >=16.
        return "node:14-bullseye"

    def image_tag(self) -> str:
        # Scoped per PR, not the bare "base" most configs in this repo use.
        # The hardening below prunes this image to one commit's ancestry and
        # asserts `rev-list --all == rev-list HEAD`, so a PR-agnostic tag would
        # name an image that is physically pinned to whichever PR built it
        # first -- and a second ADS instance reusing it would inherit the wrong
        # commit. Scoping the tag makes the name match what the image actually
        # is. Cost: no cross-PR layer sharing, so each instance pays its own
        # clone + gc. Acceptable here; if ADS ever grows a batch, an era-scoped
        # tag (cf. Stirling-PDF's base-jdk17) shares where sharing is valid.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            # extension native-module headers (node-gyp builds against these)
            "libx11-dev",
            "libxkbfile-dev",
            "libsecret-1-dev",
            "libkrb5-dev",
            "pkg-config",
            # Electron 9 runtime libs + a virtual display to launch it headlessly.
            # xauth and libxtst6 are not optional extras: xvfb-run aborts with
            # "xauth command not found" without the former, and the Electron
            # binary has exactly one unresolved shared library without the
            # latter (libXtst.so.6, confirmed by ldd). Both were found only by
            # launching the app, not by reading the package list.
            "xvfb",
            "xauth",
            "libxtst6",
            "dbus-x11",
            "libgtk-3-0",
            "libgbm1",
            "libnss3",
            "libxss1",
            "libasound2",
            "libatk-bridge2.0-0",
            "libatk1.0-0",
            "libcups2",
            "libdrm2",
            "libxcomposite1",
            "libxdamage1",
            "libxrandr2",
            "libpango-1.0-0",
            "libcairo2",
            "libx11-xcb1",
            "fonts-liberation",
        ]

    def dockerfile(self) -> str:
        # Hand-written rather than inherited, matching every other config in
        # this repo (medium-zoom, Stirling-PDF, uPortal all do the same).
        #
        # Two reasons it cannot just fall through to Image.dockerfile():
        #
        #  1. That template hardcodes "WORKDIR /home/\nENV DEBIAN_FRONTEND=
        #     noninteractive\nENV LANG=C.UTF-8" (image.py:237), and
        #     DockerfileEnhancer._ENV_BLOCK already emits both -- plus TZ --
        #     immediately after FROM. Inheriting duplicates them for no effect.
        #     Only vars the enhancer does not provide belong in this template;
        #     this repo needs none, so none are set.
        #
        #  2. The clone below is written with a literal URL on purpose. The
        #     enhancer's _standardize_repo_fetch matches exactly that shape and
        #     rewrites it into the canonical block -- clone via "${REPO_URL}",
        #     WORKDIR, reset, checkout ${BASE_COMMIT}, _HARDENING_BLOCK, CMD.
        #     Writing the "${REPO_URL}" form here instead would trip its
        #     negative lookahead, skip the rewrite, and silently lose both the
        #     checkout and the hardening.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

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
        apt_command = self._get_apt_update_command(packages_str, image_name)

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

{apt_command}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class AzureDataStudioImageDefault(Image):
    """Per-PR layer: stage the scripts and bake the build.

    The repository, the checkout and the hardening all live in the base image;
    this layer only adds the patches and scripts and runs prepare.sh. That is
    the shape every other config in this repo uses.

    dependency() returns an Image, so DockerfileEnhancer.enhance() emits this
    Dockerfile verbatim -- no syntax directive, no ARGs -- and build_dataset
    passes no build args (it sets REPO_URL/BASE_COMMIT only when dependency()
    is a str). Nothing here needs them: prepare.sh interpolates the commit from
    the PR record directly.
    """

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
        return AzureDataStudioImageBase(self.pr, self._config)

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        sections = [f"FROM {base.image_full_name()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        launch_cmd = """\
# Select tests the way scripts/test-extensions-unit.sh does by default:
# an INVERTED @UNSTABLE@ filter, i.e. "run everything not marked unstable".
# This is deliberate. This PR's test patch also appends an @DacFx@ tag to
# four pre-existing suites; selecting on @DacFx@ directly would flip those
# suites from unselected to selected and manufacture a large fake
# fail->pass set. Under an inverted @UNSTABLE@ filter they run identically
# before and after the patch, so only genuinely new/changed tests move.
export CI=true
export ADS_TEST_GREP=@UNSTABLE@
export ADS_TEST_INVERT_GREP=1

# prepare.sh already baked in .build/electron, out/, and the built-in
# extensions, so scripts/code.sh's preLaunch.js has nothing left to do.
# Skipping it keeps the eval-time launch free of yarn and network: its
# getBuiltInExtensions() step is not existence-guarded and would hard-fail
# the whole run (code.sh runs under set -e) if the marketplace were
# unreachable.
export VSCODE_SKIP_PRELAUNCH=1
# --nogpu is an ADS-specific flag (src/main.js:37, a {{SQL CARBON EDIT}}): it
# calls app.disableHardwareAcceleration() and appends the 'headless' and
# 'disable-gpu' Chromium switches. ADS's own scripts/test-extensions-unit.sh
# passes it to every one of its 14 extension-test launches, and ADS CI runs
# those under xvfb -- so xvfb + --nogpu is the pairing ADS itself validated.
# Without it, Electron 9 attempts GPU init against a virtual display with no
# GPU, which hangs rather than failing.
# --disable-dev-shm-usage covers Docker's 64 MB default /dev/shm, the usual
# cause of a Chromium renderer dying before any test line is printed.
UDIR=$(mktemp -d)
EDIR=$(mktemp -d)
xvfb-run -a --server-args="-screen 0 1280x1024x24" \\
    ./scripts/code.sh --no-sandbox \\
    --extensionDevelopmentPath="$PWD/extensions/schema-compare" \\
    --extensionTestsPath="$PWD/extensions/schema-compare/out/test" \\
    --user-data-dir="$UDIR" \\
    --extensions-dir="$EDIR" \\
    --disable-telemetry --disable-crash-reporter --disable-updates \\
    --disable-dev-shm-usage --nogpu 2>&1
rm -rf "$UDIR" "$EDIR"
"""

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# A clone only carries commits reachable from a branch, and the base image's
# hardening block has already removed the origin remote -- hence the full URL
# on the fallback. Fetch the base commit by sha if it is not already present.
# With the per-PR base tag this should never fire (the base was built from this
# very commit); it stays as a guard for a reused or hand-rebuilt base.
DID_FETCH=0
if ! git cat-file -e {base_sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {base_sha}
    DID_FETCH=1
fi
git checkout {base_sha}

# If a fetch DID fire it wrote new objects and a FETCH_HEAD into a repository
# the base image had already scrubbed, which silently breaks the base's
# `rev-list --all == rev-list HEAD` invariant. Re-establish and re-assert it
# rather than shipping an image whose history quietly grew. Cheap and a no-op
# on the normal path, so it is gated on the fetch actually having happened.
if [ "$DID_FETCH" = "1" ]; then
    echo "base commit was fetched at PR-build time; restoring scrub invariants"
    git checkout --detach {base_sha}
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    rm -f .git/FETCH_HEAD
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    test "$(git rev-parse HEAD)" = "$(git rev-parse {base_sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# Assert the tree is exactly BASE_COMMIT before anything below touches it.
# This is the last point at which that is true: the gulp-atom-electron pin
# below is a deliberate, documented edit to a tracked file, so the guard
# belongs here rather than at the end of this script.
bash /home/check_git_changes.sh

# UPSTREAM BIT-ROT FIX 1 of 2: dead git protocol. package.json pins
#   "html-query-plan": "git://github.com/kburtram/html-query-plan.git#2.6"
# and yarn.lock records the same scheme in its `resolved` URL. GitHub
# permanently disabled the unencrypted git:// protocol (port 9418) in March
# 2022, so that fetch cannot connect -- it hangs until the TCP connection
# times out and yarn exits 128. Rewriting the protocol in git's global config
# fixes both the manifest spec and the lockfile URL at once, without editing
# either file, because yarn shells out to git and git applies the rewrite.
# The ssh forms are covered too, defensively; --add keeps all three, since a
# plain `git config` on the same key would replace rather than append.
git config --global url."https://github.com/".insteadOf "git://github.com/"
git config --global --add url."https://github.com/".insteadOf "git@github.com:"
git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/"

# UPSTREAM BIT-ROT FIX 2 of 2: gulp-atom-electron@1.22.0, which this commit pins,
# depends on github-releases-ms@^0.5.0 -- and that package has since been
# UNPUBLISHED from npm outright (the registry returns no versions at all, with
# an "unpublished" marker), so its tarball 404s and the root install cannot
# complete as written. Exactly seven releases, 1.19.0 through 1.22.0, carry
# that dependency; 1.23.0 onward fetch releases through @electron/get +
# @octokit/rest instead, both alive.
#
# The package cannot simply be dropped: gulpfile.js glob-loads every
# build/gulpfile.*.js, and build/gulpfile.vscode.js:15 does
# `require('gulp-atom-electron')` at module load, so its absence breaks every
# gulp task including compile-client. The bump is safe because that file only
# uses the module inside packaging tasks we never invoke, and
# build/lib/electron.js merely builds an options object at import time -- we
# need require() to resolve, not the packaging API to behave. The Electron
# version itself is unaffected: it comes from .yarnrc (9.4.3), not from here.
#
# This is the one place this config knowingly departs from the base commit.
sed -i 's|"gulp-atom-electron": "\\^1\\.22\\.0"|"gulp-atom-electron": "^1.23.0"|' package.json
if ! grep -q '"gulp-atom-electron": "\\^1\\.23\\.0"' package.json; then
    echo "FATAL: gulp-atom-electron pin was not rewritten; the dead"
    echo "       github-releases-ms dependency would 404 during install."
    exit 1
fi

# --network-timeout 600000 (10 min, vs yarn's 30 s default) is required for
# multi-arch builds: buildx runs the amd64 and arm64 stages concurrently over
# one link while arm64 additionally runs under QEMU, and a single slow tarball
# then aborts the whole install with ESOCKETTIMEDOUT. Observed exactly that on
# rxjs-6.6.0.tgz during a linux/arm64 stage while the amd64 stage was already
# compiling native modules.
# --ignore-scripts skips native-module (keytar/node-pty/etc.) lifecycle
# builds against Electron 9 headers for packages this extension never
# touches; --ignore-engines works around the yarn engines gate for a
# 2021-era lockfile on a newer yarn/node patch level. It also means the
# root postinstall (build/npm/postinstall.js) never runs, so every
# directory it would have installed and that we actually need must be
# installed explicitly below.
# No --frozen-lockfile: yarn must be free to re-resolve the bumped subtree,
# since yarn.lock still pins the 1.22.0 entry.
yarn --ignore-scripts --ignore-engines --network-timeout 600000

# extensions/ holds the TypeScript shared by all extensions
# (extensions/shared.tsconfig.json consumers).
(cd extensions && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

# The extension under test: should / typemoq / sinon / mocha / vscodetestcover.
(cd extensions/schema-compare && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

# build/ carries gulp-bom and gulp-sourcemaps, which build/lib/compilation.js
# and build/gulpfile.extensions.js require but which are NOT in the root
# package.json - without this, every gulp compile task dies on
# MODULE_NOT_FOUND. build/lib/*.js is committed pre-compiled, so skipping
# build's own postinstall (tsc) is fine.
(cd build && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

# build/lib/compilation.js and build/gulpfile.extensions.js both eagerly
# `require('./watch')`, and on Linux build/lib/watch/index.js resolves to
# vscode-gulp-watch, declared only in build/lib/watch/package.json.
(cd build/lib/watch && yarn --ignore-scripts --ignore-engines --network-timeout 600000)

# --ignore-scripts above skipped every node-gyp/prebuild-install step, so the
# ~10 native modules in the root dependencies (vscode-sqlite3, native-keymap,
# native-watchdog, node-pty, keytar, vscode-nsfw, spdlog, ...) have no
# build/Release/*.node binary. That is survivable for a headless `yarn mocha`
# run, which is why the sibling vscode config gets away with it -- but this
# config boots the real Electron workbench via scripts/code.sh, and the
# electron-main process does an unguarded `await import('native-keymap')`
# (keyboardLayoutMainService.ts) and loads vscode-sqlite3 for its storage
# service. A missing .node there kills the main process before a single test
# runs, and parse_log would then see zero test lines -> all_count == 0 ->
# Report.check() rule 1 rejection, after a full image build.
#
# vscode-sqlite3's gyp action_before_build shells out to bare `python` to
# unpack the sqlite amalgamation, and Debian 11 ships only python3 with no
# `python` alias -- so make dies with "python: not found" (exit 127) before a
# single source file is compiled. node-gyp itself is fine; it finds python3 on
# its own. Only the action script needs the alias. Its extract.py uses just
# sys/os/tarfile, so python3 runs it unchanged.
ln -sf /usr/bin/python3 /usr/local/bin/python
python --version

# xauth and libxtst6 come from the base image's apt list. Assert rather than
# install: this layer must not need a second apt transaction, and a missing
# xauth otherwise surfaces only at eval time as "xvfb-run: xauth command not
# found" in all three graded stages.
command -v xauth

# Rebuild against the Electron 9.4.3 headers the root .yarnrc pins. The apt
# headers this image installs (libx11-dev, libxkbfile-dev, libsecret-1-dev,
# libkrb5-dev) exist precisely to let these compile. Not `|| true`: a broken
# native surface must fail the build, not surface later as an empty run.
# NB: package.json's own "electron-rebuild" script is stale (--arch=arm64
# --version=11.0.2) and must not be used; invoke the binary directly.
./node_modules/.bin/electron-rebuild --version 9.4.3 --force --module-dir .

# Heap is 4096, matching upstream's own "compile" script (4095) rather than
# the 8192 on package.json's generic "gulp" alias. 8192 also exceeds the RAM
# of a default WSL2 VM, where V8 grows past the cgroup limit and the kernel
# OOM-kills the compile with exit 137 after the whole install has completed.
# Core (src -> out) only. Plain `gulp compile` also builds all ~40 bundled
# extensions via compile-extensions plus a monaco typecheck, none of which
# this PR touches. The fix/test patches only touch extensions/schema-compare,
# so this core out/ stays valid for the run/test/fix passes alike.
node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-client

# Electron 9.4.3 (.build/electron + version file). NB: `electron` is NOT a
# gulp task in this repo - package.json maps `yarn electron` to this script,
# which has a require.main guard. This one IS load-bearing: without
# .build/electron there is nothing to launch.
#
# Fallback exists because gulp-atom-electron was bumped above: if the newer
# packaging API does not drive this 2021 script correctly, fetch the same
# Electron build straight from GitHub instead. scripts/code.sh launches
# ".build/electron/$(node -p "require('./product.json').applicationName")",
# so the binary is renamed to match, and build/lib/electron.js treats a
# matching version file as "already up to date".
# NB: the trigger is the version file, NOT the exit code. Node 14 reports an
# unhandled promise rejection as a warning and still exits 0, so when
# @electron/get times out fetching the release asset this script "succeeds"
# while staging nothing. Keying the fallback off the artefact it is supposed
# to produce is the only reliable signal.
node build/lib/electron || true

if [ ! -f .build/electron/version ]; then
    echo "gulp-atom-electron path staged nothing; downloading Electron 9.4.3 directly"
    APPNAME=$(node -p "require('./product.json').applicationName")
    rm -rf .build/electron && mkdir -p .build/electron
    # Derive the Electron arch from the build host rather than hardcoding it:
    # under buildx this script runs inside a container of the TARGET arch, so
    # uname -m is the right source. Electron 9.4.3 publishes linux-x64,
    # linux-arm64 and linux-armv7l (all verified live). Hardcoding x64 here
    # would silently stage an x86 binary into an arm64 image, which then fails
    # at launch with a confusing exec-format error rather than at build time.
    case "$(uname -m)" in
        x86_64)  EARCH=linux-x64 ;;
        aarch64|arm64) EARCH=linux-arm64 ;;
        armv7l)  EARCH=linux-armv7l ;;
        *) echo "FATAL: no Electron 9.4.3 build for arch $(uname -m)"; exit 1 ;;
    esac
    echo "staging Electron 9.4.3 for $EARCH"
    # --retry covers the transient ETIMEDOUT seen against GitHub's asset CDN
    # (185.199.x); github.com itself stays reachable throughout.
    curl -fsSL --retry 5 --retry-delay 5 --retry-connrefused \\
        --connect-timeout 30 --max-time 900 -o /tmp/electron.zip \\
        "https://github.com/electron/electron/releases/download/v9.4.3/electron-v9.4.3-$EARCH.zip"
    # python3 (already in the base image) rather than unzip, so this fallback
    # needs no extra apt package. It does not restore the executable bit, hence
    # the explicit chmod; chrome-sandbox is irrelevant since we pass --no-sandbox.
    python3 -m zipfile -e /tmp/electron.zip .build/electron
    (cd .build/electron && mv electron "$APPNAME" && chmod +x "$APPNAME")
    printf '9.4.3' > .build/electron/version
    rm -f /tmp/electron.zip
fi
if [ ! -f .build/electron/version ]; then
    echo "FATAL: no Electron staged in .build/electron"
    exit 1
fi

# Assert the staged binary actually matches this container's architecture.
# Both staging routes can get this wrong under buildx -- gulp-atom-electron
# picks by node's process.arch, the fallback by uname -- and a mismatch is
# invisible until launch, where it appears as an opaque exec-format error in
# every eval stage. Compare the ELF e_machine field (offset 18, 2 bytes LE):
# 3e00 = x86-64, b700 = AArch64, 2800 = ARM.
ELECTRON_BIN=".build/electron/$(node -p "require('./product.json').applicationName")"
ELF_MACHINE=$(od -An -tx1 -j18 -N2 "$ELECTRON_BIN" | tr -d ' \\n')
case "$(uname -m)" in
    x86_64)        WANT=3e00 ;;
    aarch64|arm64) WANT=b700 ;;
    armv7l)        WANT=2800 ;;
    *)             WANT="$ELF_MACHINE" ;;
esac
if [ "$ELF_MACHINE" != "$WANT" ]; then
    echo "FATAL: staged Electron is the wrong architecture."
    echo "       host $(uname -m) expects ELF machine $WANT, binary has $ELF_MACHINE"
    exit 1
fi
echo "Electron binary arch OK for $(uname -m) (ELF machine $ELF_MACHINE)"

# product.json's marketplace built-in extensions (sqlservernotebook et al).
# Non-fatal on purpose: the eval path sets VSCODE_SKIP_PRELAUNCH=1 and
# launches schema-compare against an empty --extensions-dir, so none of these
# are ever loaded. Letting an unreachable/proxied marketplace kill the image
# build would fail it on a dependency the tests do not use.
node build/lib/builtInExtensions.js || echo "WARN: built-in extension sync failed; not required for these tests"

# The one extension under test. Not `|| true`: a broken compile here must
# fail the image build, not silently produce a zero-test run later.
node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Start the baseline from the same tree the other two stages start from.
# prepare.sh intentionally leaves package.json modified (the gulp-atom-electron
# pin), and test-run.sh / fix-run.sh both reset before they patch -- without
# this, the baseline would be the only stage grading a modified tree.
# Safe: the pin matters solely to `yarn install`, which ran at image-build
# time, and `git reset --hard` does not touch untracked paths, so
# node_modules/, out/ and .build/electron/ all survive.
git reset --hard
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
if ! git apply --whitespace=nowarn --3way /home/test.patch; then
    # A failed --3way can leave conflict markers in the tree; --reject has to
    # start clean or it stacks on top of a half-applied patch.
    git reset --hard
    git apply --whitespace=nowarn --reject /home/test.patch
fi

# The test patch alone does not typecheck against the pre-fix dialog source:
# it reaches still-private members, passes a third constructor argument, and
# imports a type that is not yet exported -- 14 errors in total. That is the
# whole point of this stage; the tests must be present and failing.
#
# But the gulp task cannot deliver them. schema-compare is in
# gulpfile.extensions.js's sqlLocalizedExtensions list, so its compileTask
# runs createPipeline(build=true, emitError=true); the reporter then raises
# "Found 14 errors" on stream end, the task aborts, and because the task is
# task.series(clean, compile) the preceding rimraf has already emptied out/.
# Net effect: no JS at all, not merely stale JS. (Measured -- an earlier
# revision of this config assumed tsc's emit-on-error behaviour would survive
# the gulp wrapper. It does not.)
node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare || true

# So fall back to tsc directly, which honours the tsconfig's outDir and emits
# JS despite type errors (no tsconfig here sets noEmitOnError). The emitted
# code is what the test stage needs: `private` and arity are erased at
# runtime, the unexported type import is elided entirely, and the calls to
# the not-yet-existing connectionButtonClick/promise/promise2 become genuine
# runtime TypeErrors -- a real failing test rather than an absent one, which
# is what makes this a fail->pass instance instead of a none->pass one.
if [ ! -f extensions/schema-compare/out/test/testSchemaCompareDialog.js ]; then
    echo "gulp pipeline aborted on the expected type errors; emitting with tsc"
    ./node_modules/.bin/tsc -p extensions/schema-compare/tsconfig.json || true
fi

# Whatever route got us here, the test patch's own new file must exist, or the
# stage would silently report zero new tests and be misclassified downstream.
if [ ! -f extensions/schema-compare/out/test/testSchemaCompareDialog.js ]; then
    echo "FATAL: test.patch applied but its new test file was never emitted to"
    echo "       out/test, by gulp or by tsc. Refusing to report a misleading"
    echo "       zero-new-test run."
    exit 1
fi
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
if git apply --whitespace=nowarn --check /home/test.patch /home/fix.patch 2>/dev/null; then
    git apply --whitespace=nowarn /home/test.patch /home/fix.patch
else
    if ! git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch; then
        git reset --hard
        git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch
    fi
fi

node --max-old-space-size=4096 ./node_modules/gulp/bin/gulp.js compile-extension:schema-compare
""".format(repo=self.pr.repo)
                + launch_cmd,
            ),
        ]


@Instance.register("microsoft", "azuredatastudio")
class AzureDataStudio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AzureDataStudioImageDefault(self.pr, self._config)

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
        """Parse the mocha spec reporter output of one extension-host run.

        Test names are qualified with their enclosing suites
        ("Suite > Nested > test title") rather than the bare it() title.
        That is required for correctness here, not cosmetic: at the base
        commit this extension already has the same leaf title under two
        different suites --

            SchemaCompareMainWindow.start @DacFx@ > Should be correct when created.
            SchemaCompareDialog.openDialog @DacFx@ > Should be correct when created.

        -- so leaf-only naming collapses two distinct tests into one entry and
        can fabricate an f2p (the failing one wins the test stage, the passing
        one wins the fix stage).

        Suite segments have @Tag@ markers stripped. This PR's test patch
        appends " @DacFx@" to four previously untagged suites, so raw suite
        text would differ between the run stage (captured before test.patch)
        and the test/fix stages: one test would appear under two names, the
        untagged one seen only at baseline and the tagged one only afterwards.

        That is a data-quality loss rather than a hard rejection -- worth
        being precise about, because the failure is silent. The untagged
        entries end up test=NONE *and* fix=NONE, so Report.check() rule 4
        (test NONE/SKIP + fix FAIL) never fires; instead the classifier's
        leading `if test.fix != PASS: continue` drops them outright. The cost
        is inflated distinct-name counts and, worse, the loss of the baseline
        evidence rule 6 relies on: test.run is what distinguishes a genuine
        n2p from a test that merely regressed under test.patch. Stripping
        tags keeps one stable identity per test across all three stages.

        Assumes a single mocha run per log, which is what vscodetestcover
        produces: everything after the "N passing" summary is the failure
        detail block (and coverage output), where `N)` lines carry the SUITE
        name and diff lines can look like pending markers, so parsing stops
        there.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip all ANSI CSI sequences, including private-parameter forms such
        # as the cursor-hide CSI ?25l that the Electron host emits.
        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

        summary_re = re.compile(r"^\d+\s+(passing|failing|pending)\b")
        tag_re = re.compile(r"\s*@[\w.-]+@")
        duration = r"(?:\s+\([\d.]+\s*\w+\))?"

        re_pass = re.compile(r"^[✔✓]\s+(.*?)" + duration + r"$")
        # Hook failures name the test they ran for; check before the generic form.
        re_hook_fail = re.compile(r'^\d+\)\s+".*?"\s+hook(?:\s+for\s+"(.*?)")?\s*:?$')
        re_fail = re.compile(r"^\d+\)\s+(.*?)" + duration + r"$")
        # Require a non-space after "- " so assertion diff lines ("-foo") and
        # mocha's "- actual" diff rows are not harvested as pending tests.
        re_skip = re.compile(r"^-\s+(\S.*?)" + duration + r"$")

        summary_seen = False
        suite_stack: list[tuple[int, str]] = []

        def qualify(leaf: str) -> str:
            return " > ".join([name for _, name in suite_stack] + [leaf])

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if summary_re.match(stripped):
                summary_seen = True
                continue
            if summary_seen:
                continue

            indent = len(line) - len(line.lstrip())

            # Mocha indents every test at least one level beneath its suite, so
            # an unindented match is build/Electron log noise, not a test.
            m_pass = re_pass.match(stripped) if indent >= 2 else None
            m_hook = re_hook_fail.match(stripped) if indent >= 2 else None
            m_fail = re_fail.match(stripped) if (indent >= 2 and not m_hook) else None
            m_skip = re_skip.match(stripped) if indent >= 2 else None

            if m_pass or m_hook or m_fail or m_skip:
                # A test at indent I belongs to the nearest suite shallower than
                # I. Drop any sibling suite at or deeper than I, which mocha has
                # necessarily finished printing by now — otherwise a test that
                # follows a nested suite is misattributed to that nested suite.
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()

            if m_pass:
                name = qualify(m_pass.group(1).strip())
                if name not in failed_tests and name not in skipped_tests:
                    passed_tests.add(name)
                continue

            if m_hook:
                inner = (m_hook.group(1) or "").strip()
                name = qualify(inner) if inner else stripped
                failed_tests.add(name)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                continue

            if m_fail:
                name = qualify(m_fail.group(1).strip())
                failed_tests.add(name)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                continue

            if m_skip:
                name = qualify(m_skip.group(1).strip())
                if name not in failed_tests:
                    skipped_tests.add(name)
                    passed_tests.discard(name)
                continue

            # Anything else indented *and shaped like a suite title* is a suite
            # header. The indent >= 2 test alone is not enough: the Electron
            # host interleaves its own logging on this stream, and an indented
            # or wrapped log line would be adopted as the enclosing suite. That
            # is worse than cosmetic -- such lines usually carry a timestamp, so
            # the qualified name would differ between the run, test and fix
            # stages, splitting one test into three single-stage entries. Each
            # is then dropped by the classifier's `if test.fix != PASS`, so the
            # instance silently loses the test rather than erroring out.
            #
            # Suite titles in this extension include colons ("utils: Basic tests
            # to verify ...") so a colon must NOT be treated as a log marker;
            # the discriminators are a leading '[' (VS Code's "[main ...]" /
            # "[renderer1] [warning]" prefixes) and a leading ISO timestamp.
            if (
                indent >= 2
                and indent % 2 == 0
                and not stripped.startswith("[")
                and not _LOG_TIMESTAMP.match(stripped)
                and not _STACK_OR_SOURCE.search(stripped)
            ):
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()
                suite_stack.append((indent, tag_re.sub("", stripped).strip()))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
