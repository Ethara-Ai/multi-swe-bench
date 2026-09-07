import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# package.json declares engines node >=8.11.0 at all five base commits
# (2020-04 through 2021-09), so one pin covers the whole set.
NODE_IMAGE = "node:12.16.1"

# The graded command is selected per PR from where that PR's specs live: the
# five PRs split across three runners, and a stage only resolves an instance
# if it actually executes the specs the test patch adds.
#
#   6353  shared-libs/rules-engine/test/  -> rules-engine mocha
#   6360  api/tests/mocha/                -> api mocha
#   7081  api/tests/mocha/                -> api mocha
#   7159  webapp/tests/karma/ts/          -> webapp karma
#   7301  api/tests/mocha/                -> api mocha

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
# by files loaded before them, and in isolation they fail (measured on 7301:
# 1245 pass / 0 fail as a glob, versus 1142 pass / 7 fail one-file-per-process
# - token-login.spec.js and config.spec.js among them). A glob therefore keeps
# the cleanest baseline wherever it can run to completion.
API_TEST_CMD = (
    "UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 "
    '--exclude "api/tests/mocha/services/settings.spec.js" "api/tests/mocha/**/*.js"'
)

# The glob cannot run to completion for every PR. 6360 and 7081 each add a
# spec whose top-level `require` targets a source file that ONLY the fix patch
# creates, so in the test stage that module is legitimately absent. Loaded
# mid-glob, the failing require wedges mocha's load-error path while the files
# already loaded hold open handles: 6360 produced ZERO tap lines and hung to
# the harness timeout (`exit=124`), 7081 aborted with an empty log. The same
# file run alone exits `rc=1` in 0s.
#
# For those two PRs only, the suite runs ONE FILE PER MOCHA PROCESS, which
# confines a load failure to its own file - every other spec still reports.
# The isolation failures noted above are the price, and they are paid only
# where the alternative is no data at all. Identical in all three stages, so
# test selection parity holds either way.
API_TEST_CMD_PER_SPEC = (
    "for f in $(find api/tests/mocha -name '*.js' "
    "! -path 'api/tests/mocha/services/settings.spec.js' | sort); do "
    'UNIT_TEST_ENV=1 npx mocha --exit --reporter tap --timeout 10000 "$f" '
    "2>&1 | grep -E '^(ok|not ok)' ; done"
)

# Mirrors shared-libs/rules-engine's own "test" script. A glob is fine here:
# 6353's test patch adds cases to an existing spec whose imports all resolve
# in the base tree, so nothing wedges the loader.
RULES_ENGINE_TEST_CMD = (
    "cd shared-libs/rules-engine && "
    "npx mocha --exit --reporter tap --timeout 10000 "
    '"test/*.spec.js" "test/**/*.spec.js"'
)

# The committed karma-unit.conf.js is written for interactive use (autoWatch,
# singleRun false, plain ChromeHeadless), so grading overrides those on the
# CLI rather than editing a tracked file each stage would reset.
# ChromeHeadlessCI is the config's own launcher and adds --no-sandbox, needed
# to start Chrome as root in a container.
KARMA_TEST_CMD = (
    "cd webapp && "
    "../node_modules/.bin/ng test webapp --watch=false --progress=false "
    "--code-coverage=false --browsers=ChromeHeadlessCI --reporters=mocha"
)

# 6360 and 7081 take the per-spec runner: their test patches reference a
# source file the fix patch creates, which wedges a globbed mocha run.
_TEST_CMD_BY_PR = {
    6353: RULES_ENGINE_TEST_CMD,
    6360: API_TEST_CMD_PER_SPEC,
    7081: API_TEST_CMD_PER_SPEC,
    7159: KARMA_TEST_CMD,
    7301: API_TEST_CMD,
}

# Recorded in the generated scripts so the scoping is visible to a reviewer
# reading the artifact, not only the generator.
_SCOPE_NOTE_BY_PR = {
    6353: (
        "# Graded suite: shared-libs/rules-engine, matching the fix in that\n"
        "# lib's src/target-state.js. The test patch's karma spec is applied\n"
        "# but not executed - absent from every stage equally.\n"
    ),
    6360: (
        "# Graded suite: api mocha, which runs the patched\n"
        "# api/tests/mocha/controllers/hydration.spec.js. The lineage and e2e\n"
        "# specs are applied but not executed - the latter needs a live\n"
        "# CouchDB and api server.\n"
    ),
    7081: (
        "# Graded suite: api mocha, which runs the five patched\n"
        "# api/tests/mocha specs. The karma specs and the integration\n"
        "# migration test are applied but not executed.\n"
    ),
    7159: (
        "# Graded suite: webapp karma, the only runner reaching the fix in\n"
        "# webapp/src/ts/effects/contacts.effects.ts.\n"
    ),
    7301: (
        "# Graded suite: api mocha, which runs the patched\n"
        "# api/tests/mocha/services/generate-xform.spec.js.\n"
    ),
}

# Only PR 7159 drives a real browser (karma), and the tree pins no puppeteer
# to supply one. Because all five PRs share one base image, Chromium is
# installed unconditionally: the mocha PRs carry the layer without ever
# launching it. That is the cost of a single base - a per-PR base could skip
# it, but then it would not be a single base.
NEEDS_CHROME = {7159}


def _get_test_cmd(pr_number: int) -> str:
    return _TEST_CMD_BY_PR.get(pr_number, API_TEST_CMD)


def _get_scope_note(pr_number: int) -> str:
    return _SCOPE_NOTE_BY_PR.get(pr_number, "# Graded suite: api mocha.\n")


def _get_extra_install(pr_number: int) -> str:
    if pr_number == 6353:
        # The shared-libs loop installs with --production, which omits the
        # devDependencies rules-engine keeps its whole test toolchain in.
        return "(cd shared-libs/rules-engine && npm ci) || true\n"
    if pr_number == 7159:
        # webapp/ has its own package.json and lockfile that the root npm ci
        # does not cover, and `ng test` compiles from there.
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


# The single apt layer. node:12.16.1 is Debian 9 (stretch), retired to
# archive.debian.org, so the sources are repointed and the stale Release file
# accepted first; `-updates` is dropped by suffix because the archive
# publishes no such index. chromium is in this list because PR 7159 is graded
# by karma, which drives a real browser and the tree pins no puppeteer.
APT_LAYER = """RUN sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\
    sed -i '/-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=no update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    chromium \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*"""

# Toolchain pin, the counterpart of the reference base's `pip install`. npm 6
# is what ships with node 12 and what the committed package-lock files were
# resolved against; asserting it here fails the build loudly if the base image
# ever moves, rather than letting `npm ci` rewrite the lockfile at run time.
TOOLCHAIN_PIN = """RUN npm --version | grep -q '^6\\.' && node --version"""


def _hardening_block(sha: str) -> str:
    """Git stripping / hardening, emitted into the PR image.

    This lives in the PR layer rather than the base because the base is shared
    by all five PRs and therefore cannot hold a commit. Each PR pins the tree
    to its OWN base commit here and reduces the repository to exactly that
    history, then asserts the four invariants: HEAD == base commit, no
    residual refs, no remotes, no unreachable objects.

    The base keeps its remote and full history precisely so this block can
    resolve any of the five commits locally, with no network fetch.
    """
    return f"""# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
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

    # One shared base for all five PRs. Images dedupe on image_full_name(), so
    # a constant tag collapses the five builds into one. This is only sound
    # because the base holds no commit: it is Node plus a full clone with its
    # history intact, and each PR image's prepare.sh does the
    # `git fetch <sha>` + `git checkout <sha>` that pins its own base commit.
    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Complete base Dockerfile, emitted verbatim.

        The leading syntax directive makes DockerfileEnhancer.enhance() return
        this file unchanged, which is required here: its _inject_final_sanitize
        appends a git hardening block to any Dockerfile containing `git clone`,
        stripping `origin` and pruning unreachable objects. On a base shared by
        five PRs that would pin the tree to whichever PR built it first and
        make the other four base commits unreachable. Emitting the file in full
        keeps the clone, its remote and its whole history intact, so each PR
        image can check out its own commit and harden from there.
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    CI=true \\
    CHROME_BIN=/usr/bin/chromium \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_PROGRESS=false

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

{APT_LAYER}

{TOOLCHAIN_PIN}


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
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image has had its remote stripped, so a commit it does not already
# contain has to be fetched by sha over the full URL. That drags in fresh git
# objects, so the scrub is re-run in exactly that case.
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
# all, so a patch touching shared-libs is live at every stage.
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
git reset --hard
git clean -qfd
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
                    test_cmd=test_cmd,
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

cd /home/{repo}
git reset --hard
git clean -qfd
{scope_note}set +e
{test_cmd}
""".format(repo=self.pr.repo, scope_note=scope_note, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -euxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{scope_note}set +e
{test_cmd}
""".format(repo=self.pr.repo, scope_note=scope_note, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -euxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{scope_note}set +e
{test_cmd}
""".format(repo=self.pr.repo, scope_note=scope_note, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{self.pr.repo}

{_hardening_block(self.pr.base.sha)}

RUN bash /home/prepare.sh
"""


@Instance.register("medic", "cht-core")
class ChtCore(Instance):
    """medic/cht-core. Test command varies by PR: api mocha, rules-engine
    mocha, or webapp karma. TAP output, with a karma-mocha-reporter fallback."""

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
            # Punctuation that appears in log messages but effectively never in
            # a describe title.
            re_not_title = re.compile(r"['\"]|[.!?]$|\.\s|:\s|=>|\bhttp")
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
                    full = stable(" ".join(p for _, p in stack) + " " + title).strip()
                    if not full:
                        continue
                    if mark == "-":
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
