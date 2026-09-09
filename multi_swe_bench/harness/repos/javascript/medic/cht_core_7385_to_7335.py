"""medic/cht-core -- era 1 of 3, PR interval 7385 -> 7335 (node 8/12, grunt,
Angular 10-12, node-sass, karma 5/6 + mocha 7).

Era boundary. At every base commit in this range package.json still declares

    "engines": { "node": ">=8.11.0", "npm": ">=6.4.1" }

and the whole test surface is driven by the Gruntfile (`grunt unit` ->
`mochaTest:unit`, `karma:admin`, `exec:unit-webapp`); there are no npm
workspaces and no scripts/build/cli. At PR 8717's base (314e79061acf) that
declaration moves to `"node": ">=16.12.0", "npm": ">=8.3.1"`, grunt is gone and
the root scripts become `npm run unit` + `node scripts/build/cli npmCiModules`
with `"workspaces": ["./shared-libs/*"]`. node-sass 4.14.1 / 6.0.1 in this era
is the hard ceiling: it publishes no prebuilt binary for node 16+, so the two
groups cannot share a base image.

    cht_core_7385_to_7335.py   this file   4 PRs   node:12.16.1
    cht_core_8825_to_8717.py   4 PRs               node:16.20.2-bullseye
    cht_core_9422_to_9312.py   2 PRs               node:20.11.1-bookworm

Registration. This era answers to `medic/cht_core_7385_to_7335`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="cht_core_7385_to_7335". The plain key `medic/cht-core` is NOT
aliased here: it is already owned by cht_core.py (the 6353-7301 era) and, with
three toolchain eras in play, aliasing it would silently route an old PR at a
new base image (R26 / section 17.4 option 2 is only correct when a single era
serves every row).

Image layout follows the reference pair (apache/druid base-3284-to-2285 /
pr-2285). The base is the plain toolchain image plus a clone -- no apt layer,
no ENV beyond the proxy/cert block, no CMD but the inherited one. `node:12.16.1`
is buildpack-deps based and already ships git, curl, wget, gnupg and
build-essential, so the only system package this era adds is chromium, and it
is added in prepare.sh, in the PR layer, only for the PRs that launch a browser.
The PR image declares `ARG BASE_COMMIT`, runs prepare.sh first, then resets and
checks out that commit before the hardening block.

Graded suites, one per PR, chosen from where each PR's test patch lives:

    7335  api/tests/mocha/                -> api mocha        (TAP)
    7364  webapp/tests/mocha/unit/        -> webapp mocha     (TAP)
    7366  webapp/tests/karma/ts/          -> webapp karma     (mocha reporter)
    7385  webapp/tests/karma/ts/          -> webapp karma     (mocha reporter)

Transition shape. Every PR in this era should grade FAIL -> PASS. 7385 is the
only one that ADDS a spec rather than modifying one
(webapp/tests/karma/ts/components/snackbar.component.spec.ts), but the
component it imports, webapp/src/ts/components/snackbar/snackbar.component.ts,
already exists at 7385's base (386a45efe2f3 serves it) and the fix patch only
MODIFIES it. So the Angular spec bundle still compiles at the test stage, the
new spec runs and fails against the unfixed component, and the transition is an
ordinary f2p -- not the none-to-pass shape that an import of a fix-created file
would force (see cht_core_8825_to_8717.py, where 8785 genuinely does that).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# package.json declares engines node >=8.11.0 at all four base commits
# (2021-09 through 2021-11), so one pin covers the whole set. node 12 is also
# the newest runtime node-sass 4.14.1 (7335, 7364, 7366) publishes a binary
# for, which is what fixes this era's ceiling.
NODE_IMAGE = "node:12.16.1"

# UNIT_TEST_ENV=1 is the repo's own switch (set by the Gruntfile for these
# tests): api/src/environment and api/src/db export stubs when it is set, so
# no CouchDB is needed. --exit is required because these suites leave open
# handles and mocha 4+ otherwise hangs after printing its summary.
#
# settings.spec.js is excluded outright because it requires a JSON file from
# build/, produced only by the full grunt webapp build - absent in all three
# stages, so excluding it keeps test selection identical across stages.
#
# One mocha process for the whole tree. This is the default because the api
# suite is written to run that way: several specs depend on state established
# by files loaded before them, and in isolation they fail. The per-spec
# fallback runner that cht_core.py needs for 6360/7081 is deliberately NOT
# reproduced here: no api spec in this era's test patches references a module
# that only the fix patch creates, so nothing can wedge mocha's loader.
API_TEST_CMD = (
    "UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 "
    '--exclude "api/tests/mocha/services/settings.spec.js" "api/tests/mocha/**/*.js"'
)

# 7364's two specs are the webapp half of the Gruntfile's mochaTest:unit target
# (Gruntfile.js:772-779, which globs webapp/tests/mocha/unit plus api). Only
# the webapp half is graded: the fix touches webapp/src/js/bootstrapper, and
# running api as well would add ~1200 unrelated ids to every stage. Both
# patterns are kept because the Gruntfile lists both.
WEBAPP_MOCHA_TEST_CMD = (
    "UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 "
    '"webapp/tests/mocha/unit/**/*.spec.js" "webapp/tests/mocha/unit/*.spec.js"'
)

# The committed karma-unit.conf.js is written for interactive use (autoWatch,
# singleRun false, plain ChromeHeadless), so grading overrides those on the
# CLI rather than editing a tracked file each stage would reset.
# ChromeHeadlessCI is the config's own launcher -- verified present at both
# e72032c19b97 (7366) and 386a45efe2f3 (7385) -- and adds --no-sandbox, needed
# to start Chrome as root in a container. --reporters=mocha selects
# karma-mocha-reporter, a devDependency in this era, whose nested tree
# parse_log's fallback branch reads.
KARMA_TEST_CMD = (
    "cd webapp && "
    "../node_modules/.bin/ng test webapp --watch=false --progress=false "
    "--code-coverage=false --browsers=ChromeHeadlessCI --reporters=mocha"
)

_TEST_CMD_BY_PR = {
    7335: API_TEST_CMD,
    7364: WEBAPP_MOCHA_TEST_CMD,
    7366: KARMA_TEST_CMD,
    7385: KARMA_TEST_CMD,
}

# Recorded in the generated scripts so the scoping is visible to a reviewer
# reading the artifact, not only the generator.
_SCOPE_NOTE_BY_PR = {
    7335: (
        "# Graded suite: api mocha, which runs the patched\n"
        "# api/tests/mocha/services/generate-xform.spec.js.\n"
    ),
    7364: (
        "# Graded suite: webapp mocha, which runs the patched\n"
        "# webapp/tests/mocha/unit/{bootstrapper,purger}.spec.js. The api half\n"
        "# of the Gruntfile's mochaTest:unit target is not executed - no fix\n"
        "# file in this PR is reachable from it.\n"
    ),
    7366: (
        "# Graded suite: webapp karma, the only runner reaching the fix in\n"
        "# webapp/src/ts/services/feedback.service.ts.\n"
    ),
    7385: (
        "# Graded suite: webapp karma, which runs the three patched\n"
        "# webapp/tests/karma specs. The wdio e2e spec and page object are\n"
        "# applied but not executed - they need a live CouchDB and api server.\n"
    ),
}

# The two PRs that drive a real browser. The tree pins no puppeteer to supply
# one, so chromium is installed from apt - in prepare.sh, per PR, since the
# base carries no apt layer. The mocha PRs never pay for it.
NEEDS_CHROME = {7366, 7385}

# The PRs whose graded suite loads code out of webapp/. That is a SUPERSET of
# NEEDS_CHROME: 7364 is graded by mocha from the repo root, not karma, so it
# needs no browser - but the specs it runs still reach webapp/ sources.
#
# webapp/ has its own package.json and lockfile that the root `npm ci` does not
# cover. 7364's bootstrapper.spec.js rewires webapp/src/js/bootstrapper/index.js,
# which requires ./translator, whose line 3 is `require('eurodigit')` - a
# transitive dependency resolved only by webapp/package-lock.json, declared in
# neither root nor webapp package.json. Without the webapp install mocha dies
# at load with "Cannot find module 'eurodigit'" and the whole suite reports
# nothing, which the warm run's `|| true` hides at build time (observed on the
# first pr-7364 build: image green, zero tests in every stage).
NEEDS_WEBAPP_INSTALL = {7364, 7366, 7385}

# node:12.16.1 is Debian 9 (stretch), retired to archive.debian.org, so the
# sources are repointed and the stale Release file accepted first; `-updates`
# is dropped by suffix because the archive publishes no such index (R11).
APT_CHROMIUM = """# Chromium for the karma run. The base is the stock node image - buildpack-deps
# based, so git, curl, wget, gnupg and build-essential are already there and
# chromium is the only missing piece.
sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g' /etc/apt/sources.list
sed -i '/-updates/d' /etc/apt/sources.list
apt-get -o Acquire::Check-Valid-Until=no update
apt-get install -y --no-install-recommends chromium
rm -rf /var/lib/apt/lists/*

"""

# The toolchain assertion, moved out of the base with the apt layer. npm 6 is
# what ships with node 12 and what the committed package-lock files were
# resolved against; asserting it fails the build loudly if the base image ever
# moves, rather than letting `npm ci` rewrite the lockfile at run time.
TOOLCHAIN_ASSERT = """npm --version | grep -q '^6\\.'
node --version

"""

# The base carries no ENV beyond the proxy/cert block, so everything the
# suites need from the environment is exported by the scripts that use it.
# Identical in prepare.sh and all three stage scripts, so the graded command
# sees the same environment at every stage.
#
# NODE_OPTIONS: Angular 12's karma build webpacks the whole webapp in one node
# process, against node 12's ~2GB default old-space ceiling.
# phantomjs-prebuilt publishes NO aarch64 binary (PhantomJS is discontinued).
# It is not in any lockfile - npm pulls it while building a git-URL dependency,
# which resolves its own unpinned tree - so on linux/arm64 its install.js exits
# 1 with "Unexpected platform or architecture: linux/arm64", npm ci dies, and
# webapp/node_modules never installs. Karma then reports "You seem to not be
# depending on @angular/core" and runs ZERO tests, while every `|| true` keeps
# the image green (measured on the first pr-8717 arm64 build: 59 npm ERR lines,
# 0 tests).
#
# install.js resolves its target from PHANTOMJS_PLATFORM / PHANTOMJS_ARCH
# before falling back to process.platform / process.arch, so pinning them to
# linux/x64 lets the install script complete on arm64. The downloaded binary is
# never executed - every graded suite here drives Chromium - so an unusable x64
# blob costs nothing and unblocks the whole install.
# node-sass 4.14.1 (7335/7364/7366) and 6.0.1 (7385) publish NO linux-arm64
# binary - verified against both GitHub releases, which carry only
# linux-x64-72. On arm64 the install therefore falls back to compiling libsass
# with node-gyp under emulation: very slow, and with npm 6 a failing install
# script can abort the whole `npm ci`.
#
# SASS_BINARY_NAME overrides the computed name, so the x64 binding is fetched
# and the build step is skipped. Nothing here ever loads it: Angular 12's
# `ng test` compiles styles with dart-sass, and node-sass only serves the grunt
# build this config never runs. On amd64 the value is exactly what node-sass
# would pick by itself (node 12 = ABI 72), so this is a no-op there and a fix
# on arm64.
SCRIPT_ENV = """export SASS_BINARY_NAME=linux-x64-72
export PHANTOMJS_PLATFORM=linux
export PHANTOMJS_ARCH=x64
export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_PROGRESS=false
export CHROME_BIN=/usr/bin/chromium
export NODE_OPTIONS=--max-old-space-size=4096
# npm hits ECONNRESET against registry.npmjs.org on BOTH arches here:
# QEMU plus TLS plus npm's default 15 parallel sockets is the trigger. Measured
# on pr-9422 - the amd64 half installed 3225 packages cleanly while the arm64
# half died mid-install on zwitch-1.0.5.tgz, leaving no node_modules and a warm
# run with zero tests. Throttling concurrency and raising the retry budget makes
# the emulated install survive; the guard keeps native builds at full speed.
export NPM_CONFIG_FETCH_RETRIES=5
export NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000
export NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000
export NPM_CONFIG_FETCH_TIMEOUT=600000
export NPM_CONFIG_MAXSOCKETS=5
if [ "$(uname -m)" = "aarch64" ]; then
    export NPM_CONFIG_MAXSOCKETS=3
fi
"""


def _get_test_cmd(pr_number: int) -> str:
    return _TEST_CMD_BY_PR.get(pr_number, API_TEST_CMD)


def _get_scope_note(pr_number: int) -> str:
    return _SCOPE_NOTE_BY_PR.get(pr_number, "# Graded suite: api mocha.\n")


def _get_system_setup(pr_number: int) -> str:
    setup = ""
    if pr_number in NEEDS_CHROME:
        setup += APT_CHROMIUM
    setup += TOOLCHAIN_ASSERT
    return setup


def _get_extra_install(pr_number: int) -> str:
    if pr_number in NEEDS_WEBAPP_INSTALL:
        # webapp/ has its own package.json and lockfile that the root npm ci
        # does not cover: `ng test` compiles from there, and the webapp mocha
        # specs load webapp sources whose transitive deps live only in
        # webapp/package-lock.json.
        #
        # The @medic/* shared libs must be symlinked into webapp/node_modules
        # as well as api's: the Gruntfile's linkSharedLibs() is parameterised
        # by directory and is applied to every workspace, not just api. Without
        # this the Angular compile fails with "Cannot find module
        # '@medic/<lib>'" for a dozen libs, karma reports zero tests, and the
        # stage grades nothing (the warm run's `|| true` hides it at build
        # time, so the failure only shows up as an empty TestResult).
        return """(cd webapp && npm ci) || true
mkdir -p webapp/node_modules/@medic
for lib in /home/cht-core/shared-libs/*/; do
    lib="${lib%/}"
    ln -sfn "$lib" "webapp/node_modules/@medic/$(basename "$lib")"
done
"""
    return ""


def _hardening_block() -> str:
    """Git stripping / hardening, emitted into the PR image.

    This lives in the PR layer rather than the base because the base is shared
    by all four PRs and therefore cannot hold a commit. Each PR pins the tree
    to its OWN base commit here -- through the `BASE_COMMIT` ARG the Dockerfile
    declares -- and reduces the repository to exactly that history, then
    asserts the four invariants: HEAD == base commit, no residual refs, no
    remotes, no unreachable objects.

    The base keeps its remote and full history precisely so this block can
    resolve any of the four commits locally, with no network fetch.
    """
    return """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
    fi"""


class ImageBase(Image):
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
        return NODE_IMAGE

    # One shared base for all four PRs. Images dedupe on image_full_name(), so
    # a constant tag collapses the four builds into one. This is only sound
    # because the base holds no commit: it is Node plus a full clone with its
    # history intact, and each PR image does the `git checkout ${BASE_COMMIT}`
    # that pins its own base commit.
    #
    # The tag carries the era (R4): cht_core.py's base for the 6353-7301 era
    # is tagged plain "base", and an unqualified tag here would collide with it
    # on one image name and one build directory.
    def image_tag(self) -> str:
        return "base-7385-to-7335"

    def workdir(self) -> str:
        return "base-7385-to-7335"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Complete base Dockerfile, emitted verbatim.

        The leading syntax directive makes DockerfileEnhancer.enhance() return
        this file unchanged, which is required here: its _inject_final_sanitize
        appends a git hardening block to any Dockerfile containing `git clone`,
        stripping `origin` and pruning unreachable objects. On a base shared by
        four PRs that would pin the tree to whichever PR built it first and
        make the other three base commits unreachable. Emitting the file in
        full keeps the clone, its remote and its whole history intact, so each
        PR image can check out its own commit and harden from there.
        """
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



WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}



CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _get_test_cmd(self.pr.number)
        scope_note = _get_scope_note(self.pr.number)
        system_setup = _get_system_setup(self.pr.number)
        extra_install = _get_extra_install(self.pr.number)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Stricter than `git diff --quiet`: the failure this catches is usually a
# leftover untracked file, which `git clean -qfd` does not remove.
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
            # prepare.sh runs BEFORE the Dockerfile's reset/checkout, which is
            # safe because it does its own `git checkout <base.sha>` first, and
            # everything it produces (node_modules) is gitignored - so neither
            # the `git reset --hard` that follows it nor the `git clean -qfd`
            # at the head of each stage discards the warm install.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{system_setup}{script_env}
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image keeps its remote, so the commit is normally already present;
# a commit that is not gets fetched by sha over the full URL. That drags in
# fresh git objects, so the scrub is re-run in exactly that case.
FETCHED=0
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
    FETCHED=1
fi
git checkout {sha}
bash /home/check_git_changes.sh

if [ "$FETCHED" = "1" ]; then
    git checkout --detach {sha}
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    git repack -a -d -l --quiet
    rm -f .git/objects/info/alternates
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# Every install ends in `|| true`: a native module that fails to build on one
# architecture must not abort the image build. The test stage decides whether
# the environment is usable.
npm ci || true

# api/src requires @medic/* shared libs that api/package.json does not list;
# the Gruntfile wires them up out-of-band with a per-lib `npm ci --production`
# plus symlinks into api/node_modules/@medic. Reproduced here, symlinks and
# all, so a patch touching shared-libs is live at every stage. This era has no
# npm workspaces - those arrive with 8717 - so nothing does it for us.
for lib in shared-libs/*/; do
    lib="${{lib%/}}"
    echo "Installing shared library: $(basename "$lib")"
    (cd "$lib" && npm ci --production) || true
done
cd api
npm ci || true
mkdir -p node_modules/@medic
for lib in /home/{repo}/shared-libs/*/; do
    lib="${{lib%/}}"
    ln -sfn "$lib" "node_modules/@medic/$(basename "$lib")"
done
cd /home/{repo}

{extra_install}
# Warm run: primes caches and proves the suite loads. Outcome is irrelevant.
{test_cmd} || true
# The karma command cds into webapp/, and `git clean` only cleans the tree
# below its cwd - so the cleanup below has to start from the repo root or a
# stray untracked file elsewhere survives into check_git_changes.sh.
cd /home/{repo}
git reset --hard
git clean -qfd
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
                    test_cmd=test_cmd,
                    system_setup=system_setup,
                    script_env=SCRIPT_ENV,
                    extra_install=extra_install,
                ),
            ),
            # `set -e` covers the setup commands so a failed reset or a failed
            # `git apply` aborts the stage instead of silently grading the
            # wrong tree. It is turned off again immediately before the test
            # command: a non-zero exit there is the expected baseline result,
            # and the harness reads stdout rather than the exit code, so
            # letting -e kill the shell would truncate the log parse_log needs.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -euxo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -euxo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -euxo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_hardening_block()}
"""


@Instance.register("medic", "cht_core_7385_to_7335")
class CHT_CORE_7385_TO_7335(Instance):
    """medic/cht-core, PRs 7335-7385. Test command varies by PR: api mocha,
    webapp mocha, or webapp karma. TAP output, with a karma-mocha-reporter
    fallback."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # mocha's tap reporter prints one line per test with its full nested
        # title. Failure detail lines are indented, so the ^ anchor keeps them
        # from being read as phantom tests.
        re_result = re.compile(r"^(ok|not ok)\s+\d+\s+(.*?)(?:\s+#\s*SKIP\b.*)?$")
        re_skip_directive = re.compile(r"#\s*SKIP\b", re.IGNORECASE)

        # Some cht tests interpolate Date.now() into their own titles. A title
        # that changes every run is a different id at every stage, which
        # fabricates phantom results; normalising keeps ids stable.
        re_epoch = re.compile(r"\b1[6-9]\d{11}\b")
        re_isodate = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

        def stable(title: str) -> str:
            return re_isodate.sub("<TS>", re_epoch.sub("<TS>", title))

        def parse_karma() -> None:
            """karma-mocha-reporter prints a nested tree instead of TAP, so
            test ids are rebuilt by joining the describe headers above each
            mark line, keyed on indentation."""
            # karma-mocha-reporter marks failures with U+2716 HEAVY
            # MULTIPLICATION X, not the U+2717 BALLOT X mocha's own spec
            # reporter uses. Missing it silently drops every failing test:
            # the fail stage then looks empty rather than failing, and a real
            # fail-to-pass transition is misclassified as none-to-pass.
            re_mark = re.compile(r"^(\s*)([✓✔v]|[✗✘✖x]|-)\s+(.*?)\s*$")
            re_header = re.compile(r"^(\s*)(\S.*?)\s*$")
            # Karma's banners, the `set -x` trace and the browser console are
            # all interleaved with the tree, and console messages wrap over
            # several lines at arbitrary indent - so a header is accepted only
            # if it LOOKS like a describe title. Real titles are short and free
            # of the sentence punctuation and quoting that log spew carries;
            # rejecting on that shape is what keeps a wrapped console line from
            # being adopted as a suite and prefixing every id beneath it.
            re_noise = re.compile(
                r"^(\d{2}\s+\d{2}\s+\d{4}\s+\d{2}:\d{2}:\d{2}|"
                r"\+\s|\d+\.\s|"
                r"Chrome|Firefox|Headless|Executed\b|SUCCESS\b|SUMMARY:|FAILED\b|TOTAL:|"
                r"Finished\b|\d+\s+(tests?|specs?|passing|failing|pending|completed)\b|"
                r"INFO\b|WARN\b|LOG:|ERROR:|DEBUG:|START:|Running|"
                r"\[|<|Browser\b|Connected\b|Disconnected\b|webpack\b|at\s)",
                re.IGNORECASE,
            )
            # karma's own summary lines ("i 4 tests skipped", "v 2216 tests
            # completed") are not describe titles. They escape re_noise because
            # its alternatives are ^-anchored and these lines open with a glyph
            # (U+2139 INFORMATION SOURCE, a tick), so the leading \W* is what
            # actually catches them. Left unfiltered, the summary becomes a
            # suite header and the failure-detail block re-printed beneath it
            # yields prefixed duplicates of real failures - ids that exist in
            # no other stage (2 of them on pr-8717).
            re_summary = re.compile(
                r"^[^\dA-Za-z]*\d+\s+(tests?|specs?|failed|completed|succeeded|"
                r"skipped|passing|failing|pending)\b",
                re.IGNORECASE,
            )
            # Punctuation that appears in log messages but effectively never in
            # a describe title.
            re_not_title = re.compile(r"['\"]|[.!?]$|\.\s|:\s|:$|=>|\bhttp")
            # The log holds the whole stage script - `set -x` traces, git
            # output, npm noise - and only its tail is karma's tree. Anchor on
            # the LAST browser-launch banner so nothing emitted before the run
            # can be mistaken for a suite header; without this, whatever line
            # precedes the tree (a `HEAD is now at ...`, a console message)
            # becomes a root describe and prefixes every id under it.
            lines = log.splitlines()
            start = 0
            for i, line in enumerate(lines):
                if re.search(r"(Chrome|Firefox)\w*\s+[\d.]+.*\bconnected\b", line, re.I) or re.search(
                    r"^\s*(START|Executing):", line
                ):
                    start = i + 1
            stack: list[tuple[int, str]] = []
            for line in lines[start:]:
                if not line.strip():
                    continue
                mm = re_mark.match(line)
                if mm:
                    indent, mark, title = len(mm.group(1)), mm.group(2), mm.group(3)
                    # karma's SUMMARY footer reuses the tick mark for its
                    # totals ("v 1659 tests completed"), which would otherwise
                    # be recorded as a test named after the count.
                    if re.match(
                        r"^\d+\s+(tests?|specs?|failed|completed|succeeded)\b",
                        title,
                        re.IGNORECASE,
                    ):
                        continue
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                    # Drop the trailing "(1023ms)" karma appends to slow tests.
                    title = re.sub(r"\s*\(\d+ms\)\s*$", "", title)
                    # karma-mocha-reporter prints a PENDING test with the same
                    # glyph it uses for a failure, and appends " (skipped)".
                    # Reading those as failures is wrong twice over: the failed
                    # count is inflated, and the suffix makes the id differ
                    # from the same test's id in a stage where it does run, so
                    # the two never match across stages (R3). Observed on
                    # pr-8717: 4 "multimedia should pause ... (skipped)" specs.
                    skipped_mark = bool(re.search(r"\s*\(skipped\)\s*$", title, re.I))
                    if skipped_mark:
                        title = re.sub(r"\s*\(skipped\)\s*$", "", title, flags=re.I)
                    full = stable(" ".join(p for _, p in stack) + " " + title).strip()
                    if not full:
                        continue
                    if mark == "-" or skipped_mark:
                        if full not in passed_tests and full not in failed_tests:
                            skipped_tests.add(full)
                    elif mark in ("✓", "✔", "v"):
                        if full not in failed_tests:
                            skipped_tests.discard(full)
                            passed_tests.add(full)
                    else:
                        passed_tests.discard(full)
                        skipped_tests.discard(full)
                        failed_tests.add(full)
                    continue
                hm = re_header.match(line)
                if (
                    hm
                    and not re_noise.match(hm.group(2))
                    and not re_not_title.search(hm.group(2))
                    and not re_summary.match(hm.group(2))
                    # A describe title always contains a letter. Assertion-diff
                    # lines in karma's failure-detail block ("+10", "-9") do
                    # not, and were being adopted as suite headers - prefixing
                    # every failure title re-printed beneath them and inventing
                    # ids that exist in no other stage (5 of them on pr-7385).
                    and re.search(r"[A-Za-z]", hm.group(2))
                    and len(hm.group(2)) <= 120
                ):
                    indent, text = len(hm.group(1)), hm.group(2)
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                    stack.append((indent, text))

        for raw in log.splitlines():
            m = re_result.match(raw.rstrip())
            if not m:
                continue
            status, title = m.group(1), stable(m.group(2).strip())
            if not title:
                continue
            if re_skip_directive.search(raw):
                if title not in passed_tests and title not in failed_tests:
                    skipped_tests.add(title)
            elif status == "ok":
                if title not in failed_tests:
                    skipped_tests.discard(title)
                    passed_tests.add(title)
            else:
                passed_tests.discard(title)
                skipped_tests.discard(title)
                failed_tests.add(title)

        # A TAP run always emits at least one ok/not ok line, so an empty
        # result means the log came from karma instead.
        if not (passed_tests or failed_tests or skipped_tests):
            parse_karma()

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
