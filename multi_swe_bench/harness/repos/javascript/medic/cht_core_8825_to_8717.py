"""medic/cht-core -- era 2 of 3, PR interval 8825 -> 8717 (node 16, npm
workspaces, Angular 16, sass, mocha 10 + karma 6.4).

Era boundary. At PR 8717's base (314e79061acf) package.json declares

    "engines":    { "node": ">=16.12.0", "npm": ">=8.3.1" }
    "workspaces": ["./shared-libs/*"]

where every base at or below 7385 still declares `"node": ">=8.11.0"` with no
workspaces key at all. The runner moved with it: grunt is gone from the tree
and the root scripts are `npm run unit` -> `node scripts/build/cli npmCiModules`
+ `unit-webapp` / `unit-admin` / `unit-shared-lib` / `unit-api`, with node-sass
replaced by sass and Angular 10/12 by Angular 16. At PR 9312's base
(8516ec914f01) engines move again, to `"node": ">=20.11.0", "npm": ">=10.2.4"`
with Angular 17. Three declarations, three base images:

    cht_core_9422_to_9312.py   2 PRs               node:20.11.1-bookworm
    cht_core_8825_to_8717.py   this file   4 PRs   node:16.20.2-bullseye
    cht_core_7385_to_7335.py   4 PRs               node:12.16.1

Registration. This era answers to `medic/cht_core_8825_to_8717`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="cht_core_8825_to_8717". The plain key `medic/cht-core` is NOT
aliased here: it is already owned by cht_core.py (the 6353-7301 era) and, with
three toolchain eras in play, aliasing it would silently route a PR at the
wrong base image (R26 / section 17.4 option 2 is only correct when a single era
serves every row).

Image layout follows the reference pair (apache/druid base-3284-to-2285 /
pr-2285). The base is the plain toolchain image plus a clone -- no apt layer,
no ENV beyond the proxy/cert block, no CMD but the inherited one.
`node:16.20.2-bullseye` is buildpack-deps based and already ships git, curl,
wget, gnupg and build-essential, so the only system package this era adds is
chromium, and it is added in prepare.sh, in the PR layer, only for the PRs that
launch a browser. The PR image declares `ARG BASE_COMMIT`, runs prepare.sh
first, then resets and checks out that commit before the hardening block.

Graded suites, one per PR, chosen from where each PR's test patch lives:

    8717  webapp/tests/karma/ts/          -> webapp karma          (mocha reporter)
    8721  webapp/tests/karma/ts/          -> webapp karma          (mocha reporter)
    8785  webapp/tests/karma/ts/          -> webapp karma          (mocha reporter)
    8825  shared-libs/search/test/        -> shared-libs mocha     (TAP)

Transition shape, measured. TWO of the three karma PRs grade none-to-pass, for
the same underlying reason: the Angular builder type-checks and bundles every
spec as ONE unit, so a single unresolved symbol anywhere means the compile
fails, karma reports zero tests, and the whole test stage is NONE.

    8785  ADDS a spec importing webapp/src/ts/services/indexed-db.service.ts,
          a FILE the fix patch creates       -> test stage 0 tests -> n2p
    8721  MODIFIES an existing spec, but the new assertions call
          `displayTrainingCards()`, a METHOD the fix patch adds to
          training-cards.service.ts. Measured:
          "error TS2339: Property 'displayTrainingCards' does not exist"
          at training-cards.service.spec.ts:613 -> test stage 0 tests -> n2p
          (2212 n2p, 0 failures at fix, valid)
    8717  MODIFIES a spec that resolves entirely against base-commit symbols
          -> test stage runs -> ordinary f2p (2 f2p, measured)

Do not assume "modifies an existing spec" implies f2p: 8721 disproves it. Only
a spec whose every referenced symbol already exists at the base commit can run
at the test stage.

The cost of the n2p shape is report.py's step-4 anomaly rule
(report.py:228-237): PASS at run, NONE at test, FAIL at fix invalidates the
whole instance. With the test stage empty, EVERY test is NONE there, so one
flaky karma failure at the fix stage drops 8721 or 8785 entirely. Look for a
flake before looking for a config bug.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# package.json declares engines node >=16.12.0 at all four base commits
# (2023-11 through 2024-01), and Angular 16 requires ^16.14 || >=18.10, so the
# newest 16.x is the one runtime that satisfies both. bullseye rather than
# slim/alpine because the karma PRs install a real Chromium from apt.
NODE_IMAGE = "node:16.20.2-bullseye"

# The committed karma-unit.base.conf.js is written for interactive use
# (autoWatch, singleRun false, plain ChromeHeadless), so grading overrides
# those on the CLI rather than editing a tracked file each stage would reset.
# ChromeHeadlessCI is the config's own launcher -- verified present at
# 314e79061acf, webapp/tests/karma/karma-unit.base.conf.js:24-28 -- and adds
# --no-sandbox, needed to start Chrome as root in a container.
# --reporters=mocha selects karma-mocha-reporter (a root devDependency in this
# era) and drops the coverage reporter the base config also lists; parse_log's
# fallback branch reads that reporter's nested tree.
#
# `ng` comes from the ROOT node_modules: @angular/cli ^16.2.2 is a root
# devDependency, and webapp/package.json does not carry its own copy.
KARMA_TEST_CMD = (
    "cd webapp && "
    "../node_modules/.bin/ng test webapp --watch=false --progress=false "
    "--code-coverage=false --browsers=ChromeHeadlessCI --reporters=mocha"
)

# Mirrors shared-libs/search's own "test" script
# (`nyc --nycrcPath='../nyc.config.js' mocha ./test`) with nyc dropped: coverage
# instrumentation changes nothing about which tests run, and nyc's wrapper adds
# a second exit path that can swallow the reporter's last line. `mocha ./test`
# (not a glob) is the upstream invocation and loads both files in that
# directory. --exit matches the other suites: these specs leave open handles
# and mocha 4+ otherwise hangs after printing its summary.
#
# No per-lib install is needed: shared-libs/* are npm workspaces in this era,
# so the root `npm ci` installs them and hoists mocha/chai/sinon to the root
# node_modules that `npx` resolves upward to.
SEARCH_TEST_CMD = (
    "cd shared-libs/search && "
    "UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 ./test"
)

_TEST_CMD_BY_PR = {
    8717: KARMA_TEST_CMD,
    8721: KARMA_TEST_CMD,
    8785: KARMA_TEST_CMD,
    8825: SEARCH_TEST_CMD,
}

# Recorded in the generated scripts so the scoping is visible to a reviewer
# reading the artifact, not only the generator.
_SCOPE_NOTE_BY_PR = {
    8717: (
        "# Graded suite: webapp karma, which runs the patched\n"
        "# webapp/tests/karma/ts/components/sender.component.spec.ts. The wdio\n"
        "# e2e spec and page object are applied but not executed - they need a\n"
        "# live CouchDB and api server.\n"
    ),
    8721: (
        "# Graded suite: webapp karma, which runs the patched\n"
        "# webapp/tests/karma/ts/services/training-cards.service.spec.ts. The\n"
        "# wdio e2e spec is applied but not executed.\n"
    ),
    8785: (
        "# Graded suite: webapp karma, which runs the new indexed-db.service\n"
        "# spec and the patched telemetry.service spec.\n"
    ),
    8825: (
        "# Graded suite: shared-libs/search mocha, the only runner reaching the\n"
        "# fix in shared-libs/search/src/generate-search-requests.js.\n"
    ),
}

# The three PRs that drive a real browser. The tree pins no puppeteer to supply
# one, so chromium is installed from apt - in prepare.sh, per PR, since the
# base carries no apt layer. 8825 never pays for it.
NEEDS_CHROME = {8717, 8721, 8785}

# node:16.20.2-bullseye is Debian 11. The suites still resolve on the live
# mirrors, so unlike the stretch era there is no archive.debian.org repointing
# to do - but bullseye-security reached EOL on 2026-09-08 and its Release file
# now carries an elapsed Valid-Until, which apt reports as
#
#   E: Release file for .../bullseye-security/InRelease is expired
#      (invalid since 11h 58min 7s). Updates for this repository will not be
#      applied.
#
# That is an E:, so `apt-get update` exits 100 and `set -e` kills the build
# (observed on pr-8785). Check-Valid-Until=no accepts the stale index, the same
# R11 treatment the stretch era needs for the same underlying reason: these
# images pin a fixed old commit, so a frozen package index is what we want.
APT_CHROMIUM = """# Chromium for the karma run. The base is the stock node image - buildpack-deps
# based, so git, curl, wget, gnupg and build-essential are already there and
# chromium is the only missing piece.
#
# Acquire::Retries=5 because apt does NOT retry by default and this host's
# network drops connections under load: pr-8721 died with
# "Failed to fetch .../libxcb-shape0_1.14-3_amd64.deb - Error reading from
# server. Remote end closed connection", apt exit 100, build dead after the
# other nine images had already succeeded. One dropped socket should not cost
# a 70-minute image.
#
# bullseye-security is dropped rather than repointed: archive.debian.org carries
# /debian bullseye (200) but has no /debian-security bullseye-security yet (404),
# and deb.debian.org has already pulled that pool. bullseye-updates goes with it
# for the same reason. What remains, archive.debian.org/debian bullseye main, has
# chromium 120.0.6099.224-1~deb11u1 - the same build the security suite carried -
# so nothing is lost by dropping the suite.
sed -i '/-security/d; /-updates/d' /etc/apt/sources.list
sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list
apt-get -o Acquire::Check-Valid-Until=no -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium
rm -rf /var/lib/apt/lists/*

"""

# The toolchain assertion, moved out of the base with the apt layer. npm 8 is
# what ships with node 16.20.2 and what satisfies the era's `"npm": ">=8.3.1"`;
# asserting it fails the build loudly if the base image ever moves, rather than
# letting `npm ci` rewrite the lockfile at run time.
TOOLCHAIN_ASSERT = """npm --version | grep -q '^8\\.'
node --version

"""

# The base carries no ENV beyond the proxy/cert block, so everything the
# suites need from the environment is exported by the scripts that use it.
# Identical in prepare.sh and all three stage scripts, so the graded command
# sees the same environment at every stage.
#
# NODE_OPTIONS: Angular 16's karma build webpacks the whole webapp in one node
# process, against node 16's ~2GB default old-space ceiling.
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
SCRIPT_ENV = """export PHANTOMJS_PLATFORM=linux
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
    return _TEST_CMD_BY_PR.get(pr_number, KARMA_TEST_CMD)


def _get_scope_note(pr_number: int) -> str:
    return _SCOPE_NOTE_BY_PR.get(pr_number, "# Graded suite: webapp karma.\n")


def _get_system_setup(pr_number: int) -> str:
    setup = ""
    if pr_number in NEEDS_CHROME:
        setup += APT_CHROMIUM
    setup += TOOLCHAIN_ASSERT
    return setup


def _get_extra_install(pr_number: int) -> str:
    if pr_number in NEEDS_CHROME:
        # webapp/ and admin/ keep their own package.json + lockfile, which the
        # root install does not cover, and `ng test` compiles from webapp/.
        # The repo's own entry point for that is `node scripts/build/cli
        # npmCiModules` (scripts/build/index.js, MODULES = ['webapp','admin']),
        # used here rather than two hand-written `npm ci` calls so the set of
        # modules stays whatever the tree says it is.
        #
        # 8825 does not get this: shared-libs/search is a workspace of the root
        # package and reaches nothing under webapp/ or admin/, so the install
        # would be several minutes of image build for no reachable code.
        return "node scripts/build/cli npmCiModules || true\n"
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
    # The tag carries the era (R4): every cht-core config shares one image
    # name, so an unqualified "base" would collide with cht_core.py's on one
    # image and one build directory.
    def image_tag(self) -> str:
        return "base-8825-to-8717"

    def workdir(self) -> str:
        return "base-8825-to-8717"

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
            # everything it produces (node_modules, webapp/.angular) is
            # gitignored - so neither the `git reset --hard` that follows it nor
            # the `git clean -qfd` at the head of each stage discards the warm
            # install.
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
#
# One `npm ci` is enough for the shared libs in this era: package.json declares
# "workspaces": ["./shared-libs/*"], so npm installs each lib and links it into
# node_modules/@medic itself. The per-lib `npm ci --production` + hand-built
# symlink dance that the grunt-era config needs is gone with grunt.
npm ci || true

{extra_install}
# Warm run: primes caches and proves the suite loads. Outcome is irrelevant.
{test_cmd} || true
# The graded command cds into webapp/ or shared-libs/search, and `git clean`
# only cleans the tree below its cwd - so the cleanup below has to start from
# the repo root or a stray untracked file elsewhere survives into
# check_git_changes.sh.
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


@Instance.register("medic", "cht_core_8825_to_8717")
class CHT_CORE_8825_TO_8717(Instance):
    """medic/cht-core, PRs 8717-8825. Test command varies by PR: webapp karma
    or shared-libs mocha. TAP output, with a karma-mocha-reporter fallback."""

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
