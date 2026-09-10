"""babel/babel -- PR interval 14065 -> 12725 (yarn berry via the in-repo
`yarnPath` release, gulp `build-dev`, jest 26/27, node:14-bullseye).

Era boundary. Every base commit in this range carries a `.yarnrc.yml` whose
`yarnPath` points at a yarn release committed under `.yarn/releases/`, and
`nodeLinker: node-modules`:

    12725  ecfe20395b  yarnPath .yarn/releases/yarn-2.4.0.cjs  jest ^26.6.1
    12738  05fa18e652  yarnPath .yarn/releases/yarn-2.4.0.cjs  jest ^26.6.1
    13204  10f4d08efb  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^26.6.1
    13248  fa01fbe052  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^26.6.1
    13429  d3f4c22c28  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^27.0.0
    13525  4ee78eb3f1  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^27.0.0
    13560  bfcf783834  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^27.0.0
    13656  ebc55398a3  yarnPath .yarn/releases/yarn-2.4.1.cjs  jest ^27.0.0
    13782  ad59a2c618  yarnPath .yarn/releases/yarn-3.1.0.cjs  jest ^27.2.0
    14065  7d32f49625  yarnPath .yarn/releases/yarn-3.1.1.cjs  jest ^27.4.0

The 2.4 -> 3.1 step (13782 is the first base to add `"packageManager":
"yarn@3.1.0"`) is NOT an era boundary here: the release is a file in the tree,
so the install command is the same node invocation at every base and corepack
is never involved. That is why one range file owns all ten PRs.

    babel_14065_to_12725.py   this file   10 PRs   node:14-bullseye

node:14-bullseye is the one image every base in the range fits. yarn 2.4.x
supports node 10/12/14 and was never released for node 16; yarn 3.1 needs
>=12.20; jest 26.6 and 27.x both run on 14; and 14065's jest.config.js selects
`./test/jest-light-runner` only when the node version satisfies
`^12.22 || ^13.7 || >=14.17`, which 14.21.x does. Debian 11 is not EOL, so no
archive.debian.org repointing is needed (R11 does not apply), and the stock
node image is buildpack-deps based -- git, make, gcc and curl are already
there, so this era adds no apt layer at all.

Build is mandatory, not an optimisation. Every workspace package declares
`"main": "./lib/index.js"` and jest.config.js sets `moduleNameMapper: null`,
so the suites load `packages/*/lib`, never `packages/*/src`. Upstream CI proves
it: `.github/workflows/ci.yml` builds `packages/*/lib/**/*` as an artifact
before the lint and test jobs. `make build-no-bundle` is the target used --
`gulp build-dev` plus the flow typings, without the rollup bundle and without
`build-standalone` -- because no PR in this range touches
packages/babel-standalone. It is re-run in EVERY stage, after `git apply`:
the fix patches edit `src/`, so a build that only happened in prepare.sh would
leave all three stages grading the base-commit `lib/` and produce 0 f2p.

Rebuilding is safe under R21. `make build-no-bundle` depends on `clean` and
`clean-lib`, and both only remove ignored paths -- `clean` is `rm -f .npmrc;
rm -rf coverage packages/*/npm-debug* node_modules/.cache` plus `test-clean`
(`rm -rf */*/test/tmp */*/test-fixtures.json`), and `clean-lib` is
`rm -rf */*/lib`. `.gitignore` covers `/packages/*/lib`, `/codemods/*/lib`,
`/eslint/*/lib` and the generated runtime helpers, so no tracked file moves and
`git status --porcelain` stays empty. `make bootstrap` and `bootstrap-only` are
NOT used: they depend on `clean-all`, which deletes node_modules on every call.

Graded suite, scoped per PR to the packages its own test patch writes into.
Babel's full suite is ~30k fixtures; running it three times per instance for
ten instances is hours of wall clock for signal that lives in one or two
packages. The scope is fixed per PR and therefore identical in all three
stages, which is what R3 requires -- it is the test SELECTION that must not
move between stages, not the size of the tree.

    12725  babel-parser, babel-template
    12738  babel-parser, babel-plugin-proposal-class-static-block
    13204  babel-generator, babel-plugin-proposal-object-rest-spread,
           babel-preset-env
    13248  babel-plugin-transform-block-scoping
    13429  babel-plugin-proposal-class-properties
    13525  babel-types
    13560  babel-plugin-proposal-class-static-block,
           babel-plugin-transform-new-target
    13656  babel-plugin-proposal-class-properties,
           babel-plugin-proposal-private-property-in-object
    13782  babel-eslint-parser
    14065  babel-cli, babel-core

13782 is the only PR whose fix patch changes dependencies -- it adds eslint 8
to eslint/babel-eslint-parser/package.json and rewrites yarn.lock. prepare.sh
therefore does a throwaway `git apply` of test.patch + fix.patch and installs
once with them applied, purely to pull the new resolutions into yarn's global
cache, then resets. Nothing survives that but the cache, so the fix stage can
install offline. It is done for every PR rather than only 13782 so the four
staged scripts stay one code path.

Registration. This era answers to `babel/babel_14065_to_12725`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="babel_14065_to_12725" (R26/R27). The plain key `babel/babel`
is NOT aliased here: it is already owned by babel_dispatcher.py, whose _ERAS
table covers 4892-10852 only, and aliasing it would route those older PRs at
this era's yarn-berry base image.

`_sanitize_patch` is carried even though no patch in this range needs it --
none of the twenty patches contains a binary hunk or a committed build-output
path (verified by scanning every `test_patch`/`fix_patch` in the JSONL for
`GIT binary patch`). It is copied rather than imported (R25) so that the day a
patch does need it, the fix lands in this file and nowhere else.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections `git apply` cannot use (R19).

    Two classes: a binary hunk with no `index <old>..<new>` line, and a change
    to committed build output that does not exist at the base commit. `git
    apply` is atomic, so one such section rejects the whole patch and the stage
    produces no output at all.

    The header is parsed non-greedily because babel ships fixture directories
    with spaces in their names -- PR 14065 adds
    `packages/babel-cli/test/fixtures/babel/dir --out-dir --watch with external
    dependencies/`, which an `\\S+` header pattern would not match at all,
    leaving its payload attached to the previous section.

    No patch in this range trips either rule; the helper is a no-op today and
    is kept so a future patch that does trip one is fixed here.
    """
    if not patch:
        return patch

    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    generated = re.compile(r"(^|/)(build|dist|target)/")

    sections: list[list[str]] = []
    for line in patch.splitlines(keepends=True):
        if header.match(line.rstrip("\n")):
            sections.append([line])
        elif sections:
            sections[-1].append(line)

    kept: list[str] = []
    for section in sections:
        body = "".join(section)
        path = header.match(section[0].rstrip("\n")).group(2)
        if "GIT binary patch" in body and "\nindex " not in body:
            continue
        if generated.search(path):
            continue
        kept.append(body)

    return "".join(kept)


class Babel14065To12725ImageBase(Image):
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
        # See the module docstring: yarn 2.4.x (12725-13656) never shipped node
        # 16 support, yarn 3.1 (13782, 14065) needs >=12.20, and 14065's
        # jest-light-runner is only selected on >=14.17. 14.21.x is the single
        # version inside all three windows.
        return "node:14-bullseye"

    # One shared base for all ten PRs. Images dedupe on image_full_name(), so a
    # constant tag collapses ten builds into one clone. That is only sound
    # because this image holds NO commit: it is node plus a full-history clone,
    # and each PR image does its own `git checkout ${BASE_COMMIT}`.
    #
    # The tag carries the range (R4): every babel config in this directory
    # shares one image name, so a bare "base" would collide with the tags
    # babel_classic_jest.py and babel_dispatcher.py already use.
    def image_tag(self) -> str:
        return "base-14065-to-12725"

    def workdir(self) -> str:
        return "base-14065-to-12725"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Complete base Dockerfile, emitted verbatim.

        The leading syntax directive makes DockerfileEnhancer.enhance()
        (image.py:317) return this text unchanged, which is required for a
        shared base (R10). Left to the enhancer, `_standardize_repo_fetch`
        would rewrite the clone into `clone + git checkout ${BASE_COMMIT} +
        hardening`, pinning a base that ten PRs share to whichever PR happened
        to build it first and pruning the other nine base commits out of the
        object store. Emitting the file in full keeps the clone, its remote and
        its whole history intact.
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


class Babel14065To12725ImageDefault(Image):
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
        return Babel14065To12725ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        # Exported identically by prepare.sh and all three stage scripts, so
        # the graded command sees the same environment at every stage.
        #
        # YARN_ENABLE_IMMUTABLE_INSTALLS: yarn berry turns immutable installs
        # on by itself whenever CI is set, and 13782's fix patch rewrites
        # yarn.lock -- without this the fix stage's install aborts with
        # "The lockfile would have been modified by this install" and the whole
        # stage grades against eslint 7.
        # YARN_NODE_LINKER: the value .yarnrc.yml already declares, repeated so
        # a stray global config cannot flip the install to PnP, which jest 26
        # in this range cannot resolve through.
        # NODE_OPTIONS: `gulp build-dev` transpiles every package in one node
        # process against node 14's ~1.7GB default old-space ceiling.
        # BABEL_ENV is deliberately NOT exported: babel.config.js branches on
        # it, `make build-no-bundle` needs `development`, and the graded jest
        # run needs `test`. Each sets it on its own command line, the way
        # upstream's Makefile does.
        script_env = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
export YARN_ENABLE_TELEMETRY=0
export NODE_OPTIONS=--max-old-space-size=4096
"""

        # 14065 only, and it is a correctness fix rather than a convenience.
        # packages/babel-cli/test/index.js:94 (assertTest) spawns the real CLI
        # as a child process and asserts its stderr is EMPTY. The caniuse-lite
        # pinned by this era's 2022-02 lockfile is years stale now, so
        # browserslist prints
        #
        #     Browserslist: caniuse-lite is outdated. Please run:
        #       npx browserslist@latest --update-db
        #
        # to stderr on every single invocation, and the assertion fails even
        # though the compiled output is correct. Measured on the first pr-14065
        # run: 61 occurrences of that warning, and 56 of the 60 failures in the
        # run stage were this one assert -- 52 baseline failures that have
        # nothing to do with the pull request. The remaining 4 are the same
        # warning leaking into babel-core's async .mjs tests, which compare a
        # child process's stderr the same way.
        #
        # BROWSERSLIST_IGNORE_OLD_DATA is browserslist's own documented switch
        # for pinned or offline builds; it suppresses the warning and changes
        # nothing about which targets are resolved or what is compiled.
        #
        # Scoped to this one PR rather than added to the shared env above: no
        # other scope in this range spawns the CLI or resolves browserslist
        # targets, and a shared-env change would alter the staged scripts of
        # all ten PRs and invalidate ten cached images to fix one.
        if self.pr.number == 14065:
            script_env += "export BROWSERSLIST_IGNORE_OLD_DATA=1\n"

        # Packages whose test/ tree this PR's own test patch writes into. Kept
        # here, at the point of use, rather than in a module constant (R17).
        # jest reads positional arguments as regexes over test file paths, so
        # these are directory prefixes, not globs.
        test_scope = {
            12725: "packages/babel-parser/test packages/babel-template/test",
            12738: (
                "packages/babel-parser/test "
                "packages/babel-plugin-proposal-class-static-block/test"
            ),
            13204: (
                "packages/babel-generator/test "
                "packages/babel-plugin-proposal-object-rest-spread/test "
                "packages/babel-preset-env/test"
            ),
            13248: "packages/babel-plugin-transform-block-scoping/test",
            13429: "packages/babel-plugin-proposal-class-properties/test",
            13525: "packages/babel-types/test",
            13560: (
                "packages/babel-plugin-proposal-class-static-block/test "
                "packages/babel-plugin-transform-new-target/test"
            ),
            13656: (
                "packages/babel-plugin-proposal-class-properties/test "
                "packages/babel-plugin-proposal-private-property-in-object/test"
            ),
            13782: "eslint/babel-eslint-parser/test",
            14065: "packages/babel-cli/test packages/babel-core/test",
        }[self.pr.number]

        # `--runInBand` is R14: jest's default runner forks a worker per core
        # and those pools deadlock under qemu with no timeout on the harness
        # side. It also makes the verbose tree arrive in a single, ordered
        # stream, which is what parse_log's file/describe stack reads.
        # At 14065 -- and only there -- jest.config.js replaces the runner with
        # the in-repo ./test/jest-light-runner, which drives piscina worker
        # THREADS and ignores maxWorkers. That runner cannot be swapped out:
        # packages/babel-core/test and packages/babel-cli/test both carry
        # `{"type": "module"}`, so their specs are native ESM that the stock
        # jest-runner cannot load. Threads are in-process and do not hit the
        # fork deadlock the rule is about.
        # `--silent` is what makes the verbose tree machine-readable.
        # DefaultReporter.printTestFileHeader emits a file's console output
        # BETWEEN the `PASS <file>` header and the tree VerboseReporter appends
        # after it, so an unsilenced log interleaves arbitrary indented message
        # bodies with the describe() headings parse_log reads for nesting.
        # Failure `●` blocks are printed after the tree and are handled there;
        # console output is the case that cannot be handled by position, so it
        # is suppressed at the source.
        # No `--` separator before the flags (R15).
        # `2>&1` because jest writes its reporter output to stderr; the harness
        # captures both streams but does not order them, and parse_log depends
        # on a `PASS <file>` header preceding the tests it owns.
        test_cmd = (
            "BABEL_ENV=test yarn jest --verbose --ci --runInBand --silent "
            f"{test_scope} 2>&1"
        )

        # P13. No install runs inside a graded stage for nine of the ten PRs:
        # prepare.sh already installed at this exact base commit, none of those
        # nine patches touches package.json or yarn.lock, and an install here
        # would mean the grading run needs the network and can resolve a
        # different tree in each of the three stages.
        #
        # 13782 is the documented exception, not an oversight: its FIX patch
        # adds eslint 8 to eslint/babel-eslint-parser/package.json and rewrites
        # yarn.lock, so the fix stage genuinely has a different dependency set
        # from the run stage -- that difference IS the pull request. It is run
        # in all three of its stages so the three stay comparable, and WITHOUT
        # `|| true` so a failed resolution aborts the stage instead of quietly
        # grading the wrong eslint. prepare.sh has already pulled both
        # resolutions into yarn's global cache, so it resolves without network.
        stage_install = "yarn install\n" if self.pr.number == 13782 else ""

        # P14 hard gate. Non-tolerant, and it runs after the install and the
        # build but before the warm run. Every install in prepare.sh ends in
        # `|| true` so a native module that cannot build on one architecture
        # does not abort the image build; the cost is that a half-finished
        # install ALSO leaves a green image whose three graded stages report
        # zero tests, which report.py reads as "0 failures" rather than
        # "broken image".
        #
        # `packages/babel-core/lib` is the assertion that matters most here.
        # Every workspace declares `"main": "./lib/index.js"` and jest.config.js
        # sets `moduleNameMapper: null`, so the suites load `lib/`, never
        # `src/`. If `make build-no-bundle` silently produced nothing, jest
        # would still start and every test would error on a missing module --
        # the exact shape that reads as a parse_log bug instead of a failed
        # build.
        deps_gate = (
            "\n"
            "# Hard gate (non-tolerant). See the comment in files() for why the\n"
            "# installs above are `|| true` and why nothing here is.\n"
            "test -d node_modules\n"
            "test -x node_modules/.bin/jest\n"
            "test -d packages/babel-core/lib\n"
            + "".join(f"test -d {d}\n" for d in test_scope.split())
        )

        # Written over the node image's bundled yarn 1.22 shim. Yarn classic
        # does forward to `yarnPath` on its own, but the version that forwards
        # differs between node image builds; execing the release the tree
        # itself pins removes the question. Resolved at call time, not baked
        # in, because the filename moves with the base commit
        # (yarn-2.4.0.cjs -> yarn-2.4.1.cjs -> yarn-3.1.0.cjs -> yarn-3.1.1.cjs).
        yarn_shim = """cat > /usr/local/bin/yarn <<'YARN_SHIM'
#!/bin/sh
YARN_CJS=$(ls /home/babel/.yarn/releases/yarn-*.cjs 2>/dev/null | head -1)
if [ -z "$YARN_CJS" ]; then
    echo "yarn shim: no bundled release under /home/babel/.yarn/releases" >&2
    exit 1
fi
exec node "$YARN_CJS" "$@"
YARN_SHIM
chmod +x /usr/local/bin/yarn
"""

        return [
            File(".", "fix.patch", filtered_fix_patch),
            File(".", "test.patch", filtered_test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Stricter than `git diff --quiet`: what this catches is usually a leftover
# untracked file, which `git clean -qfd` does not remove.
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
            # prepare.sh runs at BUILD time (R16), before the Dockerfile's own
            # reset/checkout, which is safe because it checks out base.sha
            # itself first. Everything it leaves behind -- node_modules,
            # packages/*/lib, ~/.yarn/berry/cache -- is either outside the tree
            # or gitignored, so neither the `git reset --hard` that follows nor
            # the `git clean -qfd` at the head of each stage discards it.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{script_env}
{yarn_shim}
cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image keeps its remote, so the commit is normally already present;
# one that is not gets fetched by sha over the full URL.
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
fi
git checkout {sha}
bash /home/check_git_changes.sh

# Warm the install and the yarn global cache at the base commit. `|| true`
# throughout: a dependency that fails to resolve on one architecture must not
# abort the image build -- the stages decide whether the environment is usable.
yarn install || true

# Second install with BOTH patches applied, then thrown away. 13782's fix patch
# adds eslint 8 to eslint/babel-eslint-parser/package.json and rewrites
# yarn.lock; without those resolutions in ~/.yarn/berry/cache its fix stage
# would have to reach the registry at run time. `enableGlobalCache: true` in
# .yarnrc.yml puts the cache outside the work tree, so the reset below leaves
# it in place. Applied for every PR so this script stays one code path.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
yarn install || true
git checkout -- . || true
git clean -qfd

git reset --hard
git checkout {sha}
bash /home/check_git_changes.sh

# Build once so the image ships a warm packages/*/lib; every stage rebuilds
# anyway, but a cold first build inside the graded run is minutes of wall clock
# charged to the run stage.
yarn install || true
make build-no-bundle || true
{deps_gate}
# Warm run: primes caches and proves the suite loads. Outcome is irrelevant.
# The gate above already proved the tree loads; this only warms caches.
{test_cmd} || true

cd /home/{repo}
git reset --hard
git clean -qfd
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    org=self.pr.org,
                    sha=self.pr.base.sha,
                    script_env=script_env,
                    yarn_shim=yarn_shim,
                    deps_gate=deps_gate,
                    test_cmd=test_cmd,
                ),
            ),
            # `set -eo pipefail` covers the setup commands, so a failed reset,
            # a failed `git apply` or a failed build aborts the stage instead
            # of silently grading the wrong tree. It is turned off again
            # immediately before the graded command: a non-zero exit there is
            # the expected baseline result, and the harness reads the log
            # rather than the exit code, so letting -e kill the shell would
            # truncate exactly the output parse_log needs. `|| true` is never
            # put on the test command itself (it would disarm pipefail for the
            # whole script).
            #
            # `make build-no-bundle` is NOT `|| true`: after this point the
            # tree is patched, and a build that fails must fail the stage
            # rather than let jest grade the previous stage's lib/.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
{stage_install}make build-no-bundle
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    script_env=script_env,
                    stage_install=stage_install,
                    scope_note=(
                        f"# Graded suite: {test_scope}\n"
                        "# Identical in all three stages -- only the patches applied differ.\n"
                    ),
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{stage_install}make build-no-bundle
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    script_env=script_env,
                    stage_install=stage_install,
                    scope_note=(
                        f"# Graded suite: {test_scope}\n"
                        "# Identical in all three stages -- only the patches applied differ.\n"
                    ),
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{script_env}
cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{stage_install}make build-no-bundle
{scope_note}set +e
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    script_env=script_env,
                    stage_install=stage_install,
                    scope_note=(
                        f"# Graded suite: {test_scope}\n"
                        "# Identical in all three stages -- only the patches applied differ.\n"
                    ),
                    test_cmd=test_cmd,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        """PR layer: patches and scripts, the warm install/build, then the
        checkout that pins this PR's base commit, then hardening.

        dependency() returns an Image, so DockerfileEnhancer leaves this text
        alone (image.py:315, R9) -- the hardening block below is written out
        rather than inherited for that reason. It is a literal copy of
        Image._HARDENING_BLOCK, kept in-file so a change to this era cannot
        reach any other (R25).
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
{prepare_commands}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
    fi
"""


@Instance.register("babel", "babel_14065_to_12725")
class BABEL_14065_TO_12725(Instance):
    """babel/babel, PRs 12725-14065. yarn berry from the in-repo release,
    `make build-no-bundle`, jest 26/27 verbose, scoped per PR."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Babel14065To12725ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Read jest's verbose tree, naming each test by its own file.

        A bare `✓ <title>` line is not a usable id here (R20). Babel's fixture
        runners derive `it()` titles from fixture directory names, and those
        repeat across packages and across option variants inside one package --
        `class-binding` exists under both `integration/` and
        `integration-loose/` of babel-plugin-proposal-class-static-block alone.
        Collapsing them would under-count every stage identically and hide the
        f2p transition. So the file header jest prints above each block is
        tracked, the `describe()` nesting is tracked by indentation, and the
        name emitted is

            packages/babel-types/test/x.js > tsLiteralType > accepts Unary

        which is the shape report.py's `_test_name_matches_files` looks for
        (`test_name.startswith(file + " > ")`, report.py:393) with a
        repo-relative path -- jest is started from /home/babel, so the paths it
        prints already are repo-relative.

        Timing suffixes are stripped: they are the one part of the line that
        legitimately differs between the run, test and fix stages, and leaving
        them in would make the same test three different names and trip
        Report.check()'s rule 4 (R3).
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Stripped once, before the loop, not per line. The Makefile exports
        # FORCE_COLOR=true for its own recipes and jest colours by default on
        # some terminals, so the tree can arrive wrapped in SGR sequences that
        # would sit between the anchor and the glyph.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # "PASS packages/babel-types/test/x.js (5.123 s)" / "FAIL ..."
        file_re = re.compile(r"^(?:PASS|FAIL)\s+(\S+?\.m?js)(?:\s+\(.*\))?\s*$")
        # A test line: indentation, a status glyph, the title, an optional
        # duration. ✓/✔ pass, ✕/✗/× fail, ○ skipped, ✎ todo.
        test_re = re.compile(
            r"^(\s+)([✓✔✕✗×○✎])\s+(.*)$"
        )
        # Trailing "(25 ms)" / "(1.5 s)".
        timing_re = re.compile(r"\s*\((?:\d+(?:\.\d+)?)\s*(?:ms|s)\)\s*$")
        # A describe() heading: indented, no glyph, some content.
        describe_re = re.compile(r"^(\s+)(\S.*?)\s*$")

        current_file: Optional[str] = None
        in_tree = False
        stack: list[tuple[int, str]] = []

        for line in clean_log.splitlines():
            match = file_re.match(line)
            if match:
                current_file = match.group(1)
                in_tree = True
                stack = []
                continue

            if not current_file or not in_tree:
                continue

            match = test_re.match(line)
            if match:
                indent = len(match.group(1))
                glyph = match.group(2)
                title = timing_re.sub("", match.group(3)).strip()
                # jest prints "○ skipped <title>" and "✎ todo <title>"; the
                # word is reporter chrome, not part of the name, and dropping
                # it keeps a test's name identical if it is skipped in one
                # stage and run in another.
                if glyph in "○✎":
                    title = re.sub(r"^(?:skipped|todo)\s+", "", title)
                if not title:
                    continue

                parts = [current_file]
                parts.extend(label for depth, label in stack if depth < indent)
                parts.append(title)
                name = " > ".join(parts)

                if glyph in "✓✔":
                    passed_tests.add(name)
                elif glyph in "✕✗×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            # A `●` block is the failure detail jest prints AFTER the tree for
            # the same file (DefaultReporter.onTestResult: header, console,
            # tree, then failure message). Everything from there to the next
            # file header is diagnostics whose content varies between stages,
            # so reading it as describe() headings would give the same test
            # different names in different stages.
            stripped = line.strip()
            if stripped.startswith("●"):
                in_tree = False
                continue

            # Stack frames and stray runner chrome are skipped WITHOUT latching
            # the tree off: console output is emitted between the header and
            # the tree, so latching here would discard the whole file. The run
            # is `--silent`, so there should be no console body to begin with.
            if stripped.startswith(("at ", "console.", "›", "|")):
                continue

            match = describe_re.match(line)
            if match:
                indent = len(match.group(1))
                stack = [(d, label) for d, label in stack if d < indent]
                stack.append((indent, match.group(2)))

        # Failure wins over pass wins over skip: a test retried within one
        # stage, or reported by two reporters, must land in exactly one set or
        # TestResult.__post_init__ raises (R2).
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
