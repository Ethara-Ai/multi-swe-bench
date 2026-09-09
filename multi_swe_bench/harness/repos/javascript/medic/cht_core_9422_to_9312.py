"""medic/cht-core -- era 3 of 3, PR interval 9422 -> 9312 (node 20, npm 10,
npm workspaces, Angular 17, mocha 10 + karma 6.4).

Era boundary. At PR 9312's base (8516ec914f01) package.json declares

    "engines": { "node": ">=20.11.0", "npm": ">=10.2.4" }

where every base at or below 8825 still declares `"node": ">=16.12.0",
"npm": ">=8.3.1"`; @angular/cli moves to ^17.3.2 in the same commit, and the
root `unit-webapp` script switches from `mocha 'webapp/tests/mocha/**'` to
`cd webapp && npm run unit:mocha:tz`. Node 20 is a hard requirement, not a
preference: Angular 17 refuses to run on ^16 (it needs ^18.13 || >=20.9), so
this pair cannot share the 16.20.2 base of the era below.

    cht_core_9422_to_9312.py   this file   2 PRs   node:20.11.1-bookworm
    cht_core_8825_to_8717.py   4 PRs               node:16.20.2-bullseye
    cht_core_7385_to_7335.py   4 PRs               node:12.16.1

Registration. This era answers to `medic/cht_core_9422_to_9312`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="cht_core_9422_to_9312". The plain key `medic/cht-core` is NOT
aliased here: it is already owned by cht_core.py (the 6353-7301 era) and, with
three toolchain eras in play, aliasing it would silently route a PR at the
wrong base image (R26 / section 17.4 option 2 is only correct when a single era
serves every row).

Image layout follows the reference pair (apache/druid base-3284-to-2285 /
pr-2285). The base is the plain toolchain image plus a clone -- no apt layer,
no ENV beyond the proxy/cert block, no CMD but the inherited one.
`node:20.11.1-bookworm` is buildpack-deps based and already ships git, curl,
wget, gnupg and build-essential, so the only system package this era adds is
chromium, and it is added in prepare.sh, in the PR layer, only for 9422 -- the
one PR that launches a browser. The PR image declares `ARG BASE_COMMIT`, runs
prepare.sh first, then resets and checks out that commit before the hardening
block.

Graded suites, one per PR, chosen from where each PR's test patch lives:

    9312  api/tests/mocha/                -> api mocha      (TAP)
    9422  admin/tests/unit/               -> admin karma    (mocha reporter)

9312's test patch also touches two webapp karma specs (feedback.service,
version.service) and a wdio e2e spec. Only the api half is graded: three of the
six patched specs are api mocha, that is where the new api/src/services/
deploy-info.js consumer lives, and adding the webapp karma run would put a full
Angular 17 compile into every stage of this PR for two ids. The webapp specs
are applied but not executed, which keeps test selection identical across the
three stages.

Both PRs' test patches only MODIFY specs that already exist at their base
commits, and both fix patches only modify files that already exist there, so
neither the mocha loader nor the karma bundler can wedge on a missing module.
Both instances should grade FAIL -> PASS.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# package.json declares engines node >=20.11.0 / npm >=10.2.4 at both base
# commits (2024-08 and 2024-10), and @angular/cli ^17.3.2 needs >=20.9, so
# 20.11.x is the floor the tree itself names. bookworm rather than slim/alpine
# because 9422 installs a real Chromium from apt.
NODE_IMAGE = "node:20.11.1-bookworm"

# UNIT_TEST_ENV=1 is the repo's own switch, set by the root `unit-api` script:
# api/src/environment and api/src/db export stubs when it is set, so no CouchDB
# is needed. nyc is dropped from that script's `nyc --nycrcPath=... mocha`
# form - coverage instrumentation changes nothing about which tests run, and
# nyc's wrapper adds a second exit path that can swallow the reporter's last
# line. --exit is required because these suites leave open handles and mocha 4+
# otherwise hangs after printing its summary.
#
# settings.spec.js is excluded outright because it still requires
# api/build/default-docs/settings.doc.json (verified at 8516ec914f01,
# api/tests/mocha/services/settings.spec.js:10), produced only by the full
# build - absent in all three stages, so excluding it keeps test selection
# identical across stages.
#
# One mocha process for the whole tree: the api suite is written to run that
# way, with several specs depending on state established by files loaded before
# them. Nothing in this era's test patches references a module that only the
# fix patch creates, so a glob cannot wedge the loader.
#
# api/package.json declares no dependencies of its own in this era - every api
# runtime dep (express, pouchdb-core, ...) is hoisted to the root package - so
# the root `npm ci` is the whole install this command needs.
API_TEST_CMD = (
    "UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 "
    '--exclude "api/tests/mocha/services/settings.spec.js" "api/tests/mocha/**/*.js"'
)

# The repo runs the admin suite through scripts/ci/run-karma.js, which hardcodes
# `browsers: ['Chrome_Headless'], singleRun: true` and leaves the config's own
# `reporters: ['spec']` in place. karma is invoked directly here instead, for
# one reason: --reporters mocha selects karma-mocha-reporter (a root
# devDependency in this era) so the admin log has the same nested-tree shape
# parse_log's fallback branch already reads. --single-run overrides the
# config's interactive `autoWatch: true, singleRun: false`.
#
# Chrome_Headless is the config's own custom launcher
# (admin/tests/karma-unit.conf.js:13-18) and, unlike webapp's ChromeHeadlessCI,
# it does NOT pass --no-sandbox. That is why prepare.sh installs a wrapper and
# CHROME_BIN points at it: as root in a container, plain Chromium exits
# immediately without the flag and karma reports zero tests.
ADMIN_KARMA_TEST_CMD = (
    "npx karma start admin/tests/karma-unit.conf.js "
    "--single-run --browsers Chrome_Headless --reporters mocha"
)

_TEST_CMD_BY_PR = {
    9312: API_TEST_CMD,
    9422: ADMIN_KARMA_TEST_CMD,
}

# Recorded in the generated scripts so the scoping is visible to a reviewer
# reading the artifact, not only the generator.
_SCOPE_NOTE_BY_PR = {
    9312: (
        "# Graded suite: api mocha, which runs the three patched\n"
        "# api/tests/mocha specs (generate-service-worker, config-watcher,\n"
        "# deploy-info). The two webapp karma specs and the wdio e2e spec are\n"
        "# applied but not executed - identically in all three stages.\n"
    ),
    9422: (
        "# Graded suite: admin karma, which runs the patched\n"
        "# admin/tests/unit/controllers/edit-user.spec.js - the only runner\n"
        "# reaching the fix in admin/src/js/controllers/edit-user.js. The wdio\n"
        "# e2e spec and page object are applied but not executed.\n"
    ),
}

# The one PR that drives a real browser. The tree pins no puppeteer to supply
# one, so chromium is installed from apt - in prepare.sh, per PR, since the
# base carries no apt layer. 9312 never pays for it.
NEEDS_CHROME = {9422}

# node:20.11.1-bookworm is Debian 12, on the live mirrors, so no
# archive.debian.org repointing is needed here (R11 applies only to the EOL
# stretch base the 7385-to-7335 era is stuck on).
#
# admin/tests/karma-unit.conf.js's Chrome_Headless launcher passes only
# --headless --disable-gpu --remote-debugging-port=9222; it does NOT pass
# --no-sandbox the way webapp's ChromeHeadlessCI does. Chromium refuses to
# start as root without that flag, so karma-chrome-launcher is pointed at a
# wrapper that adds it (plus --disable-dev-shm-usage, since the default 64MB
# /dev/shm crashes the renderer mid-suite). Editing the tracked karma config
# instead would be undone by the `git reset --hard` at the head of every stage.
APT_CHROMIUM = """# Chromium for the karma run. The base is the stock node image - buildpack-deps
# based, so git, curl, wget, gnupg and build-essential are already there and
# chromium is the only missing piece.
apt-get update
apt-get install -y --no-install-recommends chromium
rm -rf /var/lib/apt/lists/*

printf '#!/bin/sh\\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"\\n' \\
    > /usr/local/bin/chromium-no-sandbox
chmod +x /usr/local/bin/chromium-no-sandbox

"""

# The toolchain assertion, moved out of the base with the apt layer. npm 10 is
# what ships with node 20.11.1 and what satisfies the era's
# `"npm": ">=10.2.4"`; asserting it fails the build loudly if the base image
# ever moves, rather than letting `npm ci` rewrite the lockfile at run time.
TOOLCHAIN_ASSERT = """npm --version | grep -q '^10\\.'
node --version

"""

# The base carries no ENV beyond the proxy/cert block, so everything the
# suites need from the environment is exported by the scripts that use it.
# Identical in prepare.sh and all three stage scripts, so the graded command
# sees the same environment at every stage. CHROME_BIN is set for both PRs -
# 9312 simply never launches it.
#
# NODE_OPTIONS: browserify bundles the whole admin app in one node process, and
# the api mocha glob loads ~1200 specs into one, against node 20's ~2GB default
# old-space ceiling.
#
# COUCH_URL: required, and NOT replaceable by UNIT_TEST_ENV in this era. The
# guard moved out of api/src/environment (which honoured UNIT_TEST_ENV) into
# shared-libs/environment/src/index.js, which reads only COUCH_URL and calls
# process.exit(1) at require time if it is unset - its own error text still
# says "use UNIT_TEST_ENV=1", which is stale and misleading. Without this the
# api suite dies before emitting a single TAP line (observed on the first
# pr-9312 build: image green, zero tests). No CouchDB is contacted: api/src/db
# still stubs under UNIT_TEST_ENV, and the URL only has to parse. The value is
# the one upstream CI uses (.github/workflows/build.yml:6).
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
export COUCH_URL=http://admin:pass@localhost:5984/medic-test
export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export NPM_CONFIG_FUND=false
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_PROGRESS=false
export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
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


def _get_pre_test(pr_number: int) -> str:
    """Commands each STAGE runs after `git apply`, before the graded command.

    The admin karma config loads BUILT bundles, not sources:
    admin/tests/karma-unit.conf.js:32-33 lists
    ../api/build/static/admin/js/main.js and .../templates.js. Building them
    once in prepare.sh is NOT enough - the stage scripts patch the SOURCES, so
    without a rebuild every stage keeps executing the base-commit bundle and
    the fix patch never reaches the code under test.

    Measured before this existed: the graded spec
    "EditUserCtrl controller initialisation edits the given user" failed
    identically at the test AND fix stages, 0 f2p, and gen_report rejected the
    instance ("no test cases transitioned from failed to passed").

    Rebuilt per stage so each bundles exactly the tree it just patched. NOT
    `|| true`: a bundle that fails to build must abort the stage rather than
    silently grade stale bytes. It is identical in all three stage scripts, so
    test selection stays the same across them.
    """
    if pr_number in NEEDS_CHROME:
        return """node ./scripts/build/build-angularjs-template-cache.js
PATH="/home/cht-core/node_modules/.bin:$PATH" ./scripts/build/browserify-admin.sh
"""
    return ""


def _get_extra_install(pr_number: int) -> str:
    if pr_number in NEEDS_CHROME:
        # The admin karma config loads BUILT bundles, not sources:
        # admin/tests/karma-unit.conf.js lists '../api/build/static/admin/js/
        # main.js' and '.../templates.js' among its files. Without them karma
        # starts, finds no application code and reports zero tests in every
        # stage - which reads as a parse_log bug rather than a missing build.
        #
        # These are the two steps of scripts/build/build-prepare.sh that
        # produce them; the rest of that script (ddocs, enketo sass, lessc,
        # build-config) is skipped because nothing the admin suite loads comes
        # from it. npmCiModules is the repo's own entry point for the webapp/
        # and admin/ installs the root `npm ci` does not cover
        # (scripts/build/index.js, MODULES = ['webapp','admin']); admin/ is the
        # half that matters here, and browserify resolves admin/src/js/main.js
        # against it.
        #
        # browserify comes from the root node_modules (a root devDependency,
        # ^17.0.0), which browserify-admin.sh expects on PATH because upstream
        # only ever calls it through `npm run`.
        #
        # The output lands under api/build/, which .gitignore covers
        # (`api/build/**/*`), so the `git clean -qfd` at the head of every
        # stage leaves it in place and check_git_changes.sh still sees a clean
        # tree.
        return """node scripts/build/cli npmCiModules || true
node ./scripts/build/build-angularjs-template-cache.js || true
PATH="/home/cht-core/node_modules/.bin:$PATH" ./scripts/build/browserify-admin.sh || true
"""
    return ""


def _hardening_block() -> str:
    """Git stripping / hardening, emitted into the PR image.

    This lives in the PR layer rather than the base because the base is shared
    by both PRs and therefore cannot hold a commit. Each PR pins the tree to
    its OWN base commit here -- through the `BASE_COMMIT` ARG the Dockerfile
    declares -- and reduces the repository to exactly that history, then
    asserts the four invariants: HEAD == base commit, no residual refs, no
    remotes, no unreachable objects.

    The base keeps its remote and full history precisely so this block can
    resolve either commit locally, with no network fetch.
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

    # One shared base for both PRs. Images dedupe on image_full_name(), so a
    # constant tag collapses the two builds into one. This is only sound
    # because the base holds no commit: it is Node plus a full clone with its
    # history intact, and each PR image does the `git checkout ${BASE_COMMIT}`
    # that pins its own base commit.
    #
    # The tag carries the era (R4): every cht-core config shares one image
    # name, so an unqualified "base" would collide with cht_core.py's on one
    # image and one build directory.
    def image_tag(self) -> str:
        return "base-9422-to-9312"

    def workdir(self) -> str:
        return "base-9422-to-9312"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Complete base Dockerfile, emitted verbatim.

        The leading syntax directive makes DockerfileEnhancer.enhance() return
        this file unchanged, which is required here: its _inject_final_sanitize
        appends a git hardening block to any Dockerfile containing `git clone`,
        stripping `origin` and pruning unreachable objects. On a base shared by
        two PRs that would pin the tree to whichever PR built it first and make
        the other base commit unreachable. Emitting the file in full keeps the
        clone, its remote and its whole history intact, so each PR image can
        check out its own commit and harden from there.
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
        pre_test = _get_pre_test(self.pr.number)
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
            # everything it produces (node_modules, api/build) is gitignored -
            # so neither the `git reset --hard` that follows it nor the
            # `git clean -qfd` at the head of each stage discards the warm
            # install or the admin bundle.
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
# One `npm ci` is enough for the api suite: package.json declares
# "workspaces": ["./shared-libs/*"], so npm installs each lib and links it into
# node_modules/@medic itself, and api/package.json declares no dependencies of
# its own in this era - express, pouchdb-core and the rest are hoisted to the
# root package.
npm ci || true

{extra_install}
# Warm run: primes caches and proves the suite loads. Outcome is irrelevant.
{test_cmd} || true
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
{pre_test}{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                    pre_test=pre_test,
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
{pre_test}{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                    pre_test=pre_test,
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
{pre_test}{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    scope_note=scope_note,
                    test_cmd=test_cmd,
                    script_env=SCRIPT_ENV,
                    pre_test=pre_test,
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


@Instance.register("medic", "cht_core_9422_to_9312")
class CHT_CORE_9422_TO_9312(Instance):
    """medic/cht-core, PRs 9312-9422. Test command varies by PR: api mocha or
    admin karma. TAP output, with a karma-mocha-reporter fallback."""

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
