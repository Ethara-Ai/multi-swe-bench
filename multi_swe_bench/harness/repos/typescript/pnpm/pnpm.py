import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# node:14-bullseye. These five PRs land between 2020-11 and 2021-02, and the
# repo's .github/workflows/ci.yml at that point tests node 12.17, 14 and 15.
# 14 is the middle of that matrix.
#
# Newer is actively wrong here. On node:22 the install dies with
#     ERR_INVALID_THIS  Value of "this" must be of type URLSearchParams
# which is the break between this era's pnpm fetch stack and the global fetch
# shipped from Node 18 on. Verified: 14 and 16 both fetch fine with the pnpm
# below, 22 does not.
#
# bullseye rather than the bare node:14 tag because bullseye's apt repositories
# still resolve; the default buster ones do not.
_NODE_IMAGE = "node:14-bullseye"

# pnpm 6.35.1. The lockfile is lockfileVersion 5.2 at PRs 2965, 3035, 3080 and
# 3091, and 5.3 at 3206. pnpm 10 refuses both with
#     ERR_PNPM_LOCKFILE_BREAKING_CHANGE  Lockfile not compatible with current pnpm
# 6.35.1 is the last 6.x and reads both; verified against both lockfiles before
# committing to it. package.json only says "pnpm": ">=5" and CI of the day
# installed pnpm@dev, so there is no exact pin to honour.
_PNPM_VERSION = "6.35.1"

# npm and pnpm draw progress bars and colour with non-ASCII bytes. The harness
# decodes build output with the platform default codec (cp1252 on Windows),
# where those bytes are undefined and abort the build with
# "'charmap' codec can't decode byte ...".
_ENV_BLOCK = """ENV NPM_CONFIG_PROGRESS=false \\
    NPM_CONFIG_COLOR=false \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    CI=true"""

# The history scrub lives in the PR layer, never in the base image and never in
# prepare.sh. That is possible because DockerfileEnhancer only rewrites *base*
# Dockerfiles: a PR image's dependency is an Image rather than a string, so its
# Dockerfile is emitted verbatim. The literal SHA is interpolated for the same
# reason -- an un-enhanced Dockerfile has no ${BASE_COMMIT} ARG to read.
#
# It also does real work here. The base image holds a full clone (see the note
# on the clone line below), so every future commit is present until this block
# prunes everything unreachable from the checked-out SHA.
_HARDENING = '''RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
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
    fi'''

# Markers around each package's jest --json output. One blob per package, so the
# delimiters carry the package name and parse_log reads every pair rather than
# only the last one.
_JSON_BEGIN = "===== JEST-JSON-BEGIN"
_JSON_END = "===== JEST-JSON-END"

_TAP_BEGIN = "===== TAP-BEGIN"
_TAP_END = "===== TAP-END"

# PRs whose jest runs skip ts-jest's type-check pass.
#
# ts-jest type-checks before running, so a test patch that references API the
# fix patch has not added yet does not fail a test -- it fails the whole file:
#     test/index.ts:597:19 - error TS2339:
#         Property 'testPattern' does not exist on type 'Config'
# In PR 3035's test stage that erased 27 tests from packages/config and 14 from
# filter-workspace-packages, the entire drop from 240 passes to 180, leaving one
# "suite failed to load" marker where individual results should be. Transpiling
# without the type check lets them run and fail on their assertions instead.
# Measured on packages/config: 0 tests reported with diagnostics on, 28 with
# them off, the single failure being exactly the test the fix patch repairs.
#
# Scoped to 3035 by explicit instruction, not because the problem is unique to
# it. The same type-check erasure mutes signal in 3080, 3091, 3206 and 2965,
# and switching it on for them would convert some of their none-to-pass results
# into failed-to-passed ones. Those four have already been graded and accepted,
# and widening this would change their numbers on the next rebuild, so they are
# deliberately left as they are.
#
# Genuinely broken suites still fail either way: an unresolvable import is a
# runtime error, not a type error, and prepare.sh separately asserts that every
# graded package resolves.
_NO_TYPECHECK_PRS = {"3035"}

_TEST_PKG_RE = re.compile(r"^diff --git a/packages/([^/]+)/test/", re.M)
_ANY_PKG_RE = re.compile(r"^diff --git a/packages/([^/]+)/", re.M)
_TEST_FILE_RE = re.compile(r"^diff --git a/(packages/[^/]+/test/\S+)", re.M)


def _tape_candidates(pr: PullRequest) -> list[str]:
    """Test files that may belong to a tape package rather than a jest one.

    This repo is mid-migration from tape to jest, and which runner a package
    uses depends on the commit. At PR 2965's base, supi has no jest config and
    runs its suite through
        test:tap  cd ../.. && c8 ... ts-node packages/supi/test --type-check
    while by PR 3080's base the same package has moved to jest.

    Only the checked-out tree knows which, so the choice is made in the script at
    run time; this just supplies the candidate files. Non-.ts files are dropped
    because tape files are executed directly.
    """
    return sorted(
        f
        for f in set(_TEST_FILE_RE.findall(pr.test_patch or ""))
        if f.endswith(".ts") and not f.endswith(".d.ts")
    )


def _test_packages(pr: PullRequest) -> list[str]:
    """Packages whose tests this PR touches -- where jest is run.

    Derived from the PR's own test patch rather than hardcoded, because the five
    PRs here hit very different parts of the workspace: 3091 is only `headless`,
    while 3206 spans `config`, four `plugin-commands-*` packages and `pnpm`.
    """
    return sorted(set(_TEST_PKG_RE.findall(pr.test_patch or "")))


def _compile_packages(pr: PullRequest) -> list[str]:
    """Packages needing a rebuild: the graded ones plus everything the fix edits.

    This matters more than it looks. Every package declares "main": "lib/index.js"
    and cross-package imports resolve through the workspace symlink to that
    compiled output, not to src. A fix patch edits src/. Without a rebuild after
    the patch is applied the fix is simply invisible to the tests, and the
    instance reads as unresolved with nothing in the log to explain why.

    Each package is rebuilt through its own `compile` script, not through a bare
    `tsc --build`. That distinction is not cosmetic. Most packages compile with
        rimraf lib tsconfig.tsbuildinfo && tsc --build
    but packages/pnpm compiles with
        ... && rimraf dist && pnpm run bundle && shx cp -r node-gyp-bin dist/...
    and its tests spawn the real CLI through bin/pnpm.js, which does
    `require('../dist/pnpm')` -- the esbuild bundle, which tsc never produces.
    Skipping the script leaves every spawning test failing with
        Error: Cannot find module '../dist/pnpm'
    which showed up as 110 identical "Exit code 1" failures having nothing to do
    with the patch. The rimraf only clears the target package, so dependencies
    keep their build info and the rebuild stays incremental: measured at 8s for
    packages/pnpm inside a built image.

    tsc --build follows project references, so naming a package here also
    rebuilds its dependencies. The bare-tsc branch is only a fallback for a
    package that declares no compile script.
    """
    pkgs = set(_test_packages(pr))
    pkgs |= set(_ANY_PKG_RE.findall(pr.fix_patch or ""))
    return sorted(pkgs)


def _sh_list(names: list[str]) -> str:
    return " ".join(f'"{n}"' for n in names)


class PnpmImageBase(Image):
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
        return _NODE_IMAGE

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

        # `git -C /home clone <url> <dir>` rather than `git clone <url> <path>`.
        # Both put the repo in /home/<repo> -- -C only chdirs first -- but the
        # spelling is load-bearing and must not be "tidied up".
        #
        # DockerfileEnhancer touches a base Dockerfile two ways:
        #   1. _standardize_repo_fetch replaces any line matching
        #      ^RUN\\s+git\\s+clone\\s+... with its own clone/checkout/scrub block
        #      terminated by CMD. `git -C /home clone` does not match that anchor.
        #   2. _inject_final_sanitize appends the scrub before the last CMD, and
        #      its only escape is
        #          if not any(tok in content for tok in
        #                     ("git clone", "git fetch", "git remote add")):
        #              return content
        #      "git -C /home clone" contains none of those three tokens.
        #
        # Net effect: the base ends at the clone plus CMD, and the scrub lives
        # only in the PR layer, as required. Two consequences. The base keeps a
        # *full* clone, so no base-commit seeding is needed and each PR checks
        # out its own SHA regardless of branch -- PR 2965's base is on `master`
        # while the other four are on `main`. And because the whole history is
        # present, the PR-layer scrub is what actually removes future commits.
        #
        # Caveat: this rests on a substring check in
        # multi_swe_bench/harness/image.py. If that guard is reworded the scrub
        # silently reappears in the base. The durable fix is a real opt-out in
        # the enhancer.
        code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'

        return f"""FROM {image_name}

{self.global_env}

{_ENV_BLOCK}

RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@{_PNPM_VERSION} --no-audit --no-fund

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PnpmImageDefault(Image):
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
        return PnpmImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _graded_command(self) -> str:
        """The command run identically in all three stages.

        Two things happen before jest, and both are required for the stages to be
        comparable.

        The install: a patch can change what the tree depends on, and the image's
        node_modules only knows the base commit. PR 3206's fix adds a dependency
        on can-write-to-dir, and without a fresh install the compile died with
            src/index.ts(8,39): error TS2307: Cannot find module 'can-write-to-dir'
        Because every compile script begins `rimraf lib`, that failure did not
        merely skip one package: it deleted the compiled output of config and
        then took every package that imports it down too, collapsing 291 passing
        tests to 139. PR 2965 needs it for the opposite reason -- its fix patch
        creates packages/merge-lockfiles outright, and a package that has never
        been installed has no node_modules to run from. The store is already warm
        from the image build, so this is mostly relinking.

        The rebuild: see _compile_packages. It runs after the patch is applied,
        and it is the only reason a fix patch is visible to the tests at all.

        ts-jest diagnostics are turned off, and this is the difference between
        measuring individual tests and measuring nothing. ts-jest type-checks
        before running, and a test patch necessarily references API the fix patch
        has not added yet, so the whole file dies at compile:
            test/index.ts:597:19 - error TS2339:
                Property 'testPattern' does not exist on type 'Config'
        One unresolved symbol takes down every test in the file. In PR 3035's
        test stage that erased 27 tests from packages/config and another 14 from
        filter-workspace-packages -- the entire drop from 240 passes to 180 --
        and left a single "suite failed to load" marker in place of them.
        Transpiling without the type check lets those tests run and fail
        individually on their assertions, which is what a failed-to-passed
        transition is supposed to look like. Measured on packages/config: 0 tests
        reported with diagnostics on, 28 with them off, the one failure being
        exactly the test the fix patch repairs.

        Genuinely broken suites still fail: an unresolvable import is a runtime
        error, not a type error, and prepare.sh separately asserts each graded
        package resolves.

        The jest.config.js gate: this repo is mid-migration from tape to jest.
        At PR 2965's base commit supi has no jest config and runs its suite as
            test:tap  cd ../.. && c8 ... ts-node packages/supi/test --type-check
        Pointing jest at it does not fail cleanly. Jest falls back to its default
        testMatch, picks up the two files named *.test.ts, finds no ts-jest
        transform for them and reports "Jest encountered an unexpected token" --
        two invented suite-load failures in every stage, for tests that were
        never jest's to run. Packages carrying a jest config are graded; the
        tape-era ones are left alone.

        The registry mock: several of these packages are end-to-end and talk to a
        local npm registry. Their own _test script is shaped like
            cross-env PNPM_REGISTRY_MOCK_PORT=7770 pnpm run test:e2e
            test:e2e -> registry-mock prepare && run-p -r registry-mock test:jest
        Calling _test directly is not an option, because jest then sits behind
        cross-env and run-p with no way to hand it --json. So the port is read
        back out of that same script and the mock started here instead. Packages
        whose _test is a bare `jest` report no port, skip the mock, and unset the
        variable so a port from an earlier iteration cannot leak into them.

        The mock is waited on by polling the port rather than by a fixed sleep.
        Measured startup is about 2s, but a sleep short enough to be cheap is
        also short enough to silently truncate a slow start, and the resulting
        failures look exactly like real test failures.
        """
        test_pkgs = _test_packages(self.pr)
        compile_pkgs = _compile_packages(self.pr)

        # See _NO_TYPECHECK_PRS. Rendered as an empty string elsewhere so those
        # PRs' scripts stay byte-identical to the ones they were graded with.
        diagnostics = ""
        if str(self.pr.number) in _NO_TYPECHECK_PRS:
            diagnostics = " \\\n        --globals '{\"ts-jest\":{\"diagnostics\":false}}'"

        return f"""cd /home/{self.pr.repo}

pnpm install --no-frozen-lockfile || true
bash /home/link_self.sh

for pkg in {_sh_list(compile_pkgs)}; do
    if [ -d "packages/$pkg" ]; then
        (
            cd "packages/$pkg"
            if node -e "var p=require('./package.json');process.exit(p.scripts&&p.scripts.compile?0:1)"; then
                pnpm run compile
            else
                pnpm exec tsc --build
            fi
        ) || true
    fi
done

for pkg in {_sh_list(test_pkgs)}; do
    if [ ! -d "/home/{self.pr.repo}/packages/$pkg" ]; then
        continue
    fi
    cd "/home/{self.pr.repo}/packages/$pkg"
    if [ ! -f jest.config.js ]; then
        continue
    fi
    port=$(node -e "var p=require('./package.json');var s=(p.scripts&&p.scripts._test)||'';var m=s.match(/PNPM_REGISTRY_MOCK_PORT=([0-9]+)/);process.stdout.write(m?m[1]:'')")
    mock_pid=""
    if [ -n "$port" ]; then
        export PNPM_REGISTRY_MOCK_PORT="$port"
        pnpm exec registry-mock prepare > /dev/null 2>&1 || true
        pnpm exec registry-mock > /dev/null 2>&1 &
        mock_pid=$!
        for i in $(seq 1 60); do
            if node -e "require('net').connect($port,'127.0.0.1').on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    else
        unset PNPM_REGISTRY_MOCK_PORT
    fi
    pnpm exec jest --ci --runInBand --coverage=false{diagnostics} \\
        --json --outputFile="/tmp/jest_$pkg.json" > /dev/null 2>&1 || true
    if [ -n "$mock_pid" ]; then
        kill "$mock_pid" > /dev/null 2>&1 || true
    fi
    echo "{_JSON_BEGIN} $pkg ====="
    cat "/tmp/jest_$pkg.json" 2>/dev/null || echo '{{}}'
    echo ""
    echo "{_JSON_END} $pkg ====="
done

for tf in {_sh_list(_tape_candidates(self.pr))}; do
    pkgdir=$(echo "$tf" | cut -d/ -f1-2)
    if [ ! -f "/home/{self.pr.repo}/$tf" ]; then
        continue
    fi
    if [ -f "/home/{self.pr.repo}/$pkgdir/jest.config.js" ]; then
        continue
    fi
    if [ ! -f "/home/{self.pr.repo}/$pkgdir/package.json" ]; then
        continue
    fi
    if ! node -e "var p=require('/home/{self.pr.repo}/$pkgdir/package.json');var s=p.scripts;process.exit(s&&s['test:tap']?0:1)"; then
        continue
    fi
    cd "/home/{self.pr.repo}/$pkgdir"
    port=$(node -e "var p=require('./package.json');var s=(p.scripts&&p.scripts._test)||'';var m=s.match(/PNPM_REGISTRY_MOCK_PORT=([0-9]+)/);process.stdout.write(m?m[1]:'')")
    mock_pid=""
    if [ -n "$port" ]; then
        export PNPM_REGISTRY_MOCK_PORT="$port"
        pnpm exec registry-mock prepare > /dev/null 2>&1 || true
        pnpm exec registry-mock > /dev/null 2>&1 &
        mock_pid=$!
        for i in $(seq 1 60); do
            if node -e "require('net').connect($port,'127.0.0.1').on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    else
        unset PNPM_REGISTRY_MOCK_PORT
    fi
    cd "/home/{self.pr.repo}"
    pnpm exec ts-node --transpile-only "$tf" > "/tmp/tap_out.txt" 2>&1 || true
    if [ -n "$mock_pid" ]; then
        kill "$mock_pid" > /dev/null 2>&1 || true
    fi
    echo "{_TAP_BEGIN} $tf ====="
    cat /tmp/tap_out.txt 2>/dev/null || true
    echo ""
    echo "{_TAP_END} $tf ====="
done"""

    def files(self) -> list[File]:
        cmd = self._graded_command()
        test_pkgs = _test_packages(self.pr)
        compile_pkgs = _compile_packages(self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "link_self.sh",
                # Restores each workspace package's link to itself in its own
                # node_modules. Without this most graded suites cannot even load.
                #
                # These packages import themselves by name -- headless/test/index.ts
                # opens with `import headless from '@pnpm/headless'` -- and the
                # link that makes that resolve comes from a self-referential
                # devDependency, "@pnpm/headless": "link:".
                #
                # At the older commits the manifest still declares it. By PR 3080
                # and 3091 the manifests have dropped it while pnpm-lock.yaml
                # still records it, which is precisely the mismatch pnpm reports
                # as ERR_PNPM_OUTDATED_LOCKFILE. That leaves no install flag that
                # works: --frozen-lockfile would create the links but pnpm refuses
                # the mismatched lockfile outright, and --no-frozen-lockfile
                # installs happily but re-derives the importers from the
                # manifests, so the self-links are simply not created. The result
                # is a green build whose every suite dies with
                #     Cannot find module '@pnpm/headless'
                # and reports zero tests, which the harness cannot distinguish
                # from a package that legitimately has no tests.
                #
                # So the links are recreated here explicitly, exactly as the
                # lockfile's `link:` entries describe them. Idempotent: at the
                # commits whose manifests still declare the self-dependency,
                # pnpm has already made the link and this does nothing.
                #
                # It also runs in the graded stages, not just at build time,
                # because a patch can add a whole new package -- PR 2965 creates
                # packages/merge-lockfiles -- which no build-time pass could have
                # linked.
                """#!/bin/bash
set -e

cd /home/{repo}

for d in packages/*/ privatePackages/*/ utils/*/; do
    if [ ! -f "$d/package.json" ]; then
        continue
    fi
    name=$(node -e "process.stdout.write(require('/home/{repo}/$d/package.json').name||'')" 2>/dev/null || true)
    if [ -z "$name" ]; then
        continue
    fi
    link="$d/node_modules/$name"
    if [ ! -e "$link" ]; then
        mkdir -p "$(dirname "$link")"
        ln -s "/home/{repo}/$d" "$link"
    fi
done

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                # The clean-tree assert runs first, before the install writes
                # node_modules and lib.
                #
                # --no-frozen-lockfile, matching the plain `pnpm install` the
                # repo's CI ran. --frozen-lockfile looks safer but simply cannot
                # install these commits: at PRs 3080 and 3091 the checked-in
                # lockfile really is out of step with the manifests, and pnpm 6
                # refuses with
                #     ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with
                #     "frozen-lockfile" because pnpm-lock.yaml is not up-to-date
                #     with packages/command/package.json
                # Nothing is lost by relaxing it. Resolution happens once here at
                # image-build time and node_modules is then baked into the image,
                # so all three graded stages share one dependency tree no matter
                # what the flag says. CI=true is why the flag has to be explicit:
                # pnpm defaults to frozen when it sees it. The cost of relaxing it
                # is that the self-links are re-derived from the manifests and so
                # go missing; link_self.sh puts them back.
                #
                # The `git checkout -- .` right after is not tidiness. The install
                # rewrites pnpm-lock.yaml in the working tree, and the fix patches
                # of PRs 2965 and 3035 both carry hunks against that same file, so
                # every fix stage died before running a single test with
                #     error: patch failed: pnpm-lock.yaml:494
                #     error: pnpm-lock.yaml: patch does not apply
                # Restoring the tracked files puts the tree back exactly on the
                # base commit so the patches apply, while node_modules and lib,
                # both untracked, survive. `git diff --quiet` then refuses to seal
                # an image whose tree would reject its own patches.
                #
                # Lifecycle scripts stay enabled. The repo's own CI runs a plain
                # `pnpm install`, its root prepare script populates the test
                # fixtures, and preinstall is only `npx only-allow pnpm`, which
                # we satisfy.
                #
                # The compile loop is what makes the per-stage tsc --build cheap:
                # it produces lib/ and the tsbuildinfo files once, so each graded
                # stage rebuilds only what its patch touched.
                #
                # The last loop refuses to seal an image whose graded stages
                # could not report anything. A missing jest, an unresolvable
                # ts-jest transform or a moved package directory all produce an
                # empty log, which reads downstream as "these tests do not exist"
                # rather than as a broken image, and the harness scores that as a
                # valid resolve. The require.resolve of the package's own name is
                # there because --listTests alone does not type-check and happily
                # succeeds on a tree whose self-links are missing, which is the
                # exact shape of the failure link_self.sh exists to prevent. It
                # skips packages absent at the base commit --
                # PR 2965 grades packages/merge-lockfiles, which that PR itself
                # creates -- and then insists at least one package was actually
                # checked, so the skip cannot quietly empty the whole check.
                """#!/bin/bash
set -e

cd /home/{repo}
bash /home/check_git_changes.sh

pnpm install --no-frozen-lockfile
git checkout -- .
git diff --quiet
bash /home/link_self.sh

for pkg in {compile_list}; do
    if [ -d "packages/$pkg" ]; then
        (
            cd "packages/$pkg"
            if node -e "var p=require('./package.json');process.exit(p.scripts&&p.scripts.compile?0:1)"; then
                pnpm run compile
            else
                pnpm exec tsc --build
            fi
        )
    fi
done

for pkg in {test_list}; do
    if [ ! -d "/home/{repo}/packages/$pkg" ]; then
        continue
    fi
    cd "/home/{repo}/packages/$pkg"
    if [ ! -f jest.config.js ]; then
        continue
    fi
    pnpm exec jest --version > /dev/null
    pnpm exec jest --listTests > /tmp/lt_$pkg.txt 2>&1
    test -s /tmp/lt_$pkg.txt
    wc -l < /tmp/lt_$pkg.txt
    node -e "var n=require('./package.json').name;require.resolve(n)"
done

""".format(
                    repo=self.pr.repo,
                    compile_list=_sh_list(compile_pkgs),
                    test_list=_sh_list(test_pkgs),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{cmd}
""".format(cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(repo=self.pr.repo, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(repo=self.pr.repo, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = _HARDENING.format(sha=self.pr.base.sha)

        # Checkout, the COPYs and the scrub all sit here rather than in the base
        # image or prepare.sh. prepare.sh only installs and compiles, against a
        # tree that is already pinned and already scrubbed.
        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}

{hardening}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("pnpm", "pnpm")
class Pnpm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PnpmImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        repo_prefix = f"/home/{self.pr.repo}/"

        # One JSON blob per graded package, each fenced by its own markers.
        # Reading every pair rather than only the last is the difference between
        # scoring all the packages a PR touches and scoring just one of them.
        blob_re = re.compile(
            re.escape(_JSON_BEGIN)
            + r"\s+(?P<pkg>\S+)\s+=====\s*(?P<body>.*?)"
            + re.escape(_JSON_END),
            re.S,
        )

        for match in blob_re.finditer(log):
            body = match.group("body")
            start = body.find("{")
            if start == -1:
                continue

            try:
                data = json.loads(body[start:])
            except Exception:
                # A truncated or interleaved blob must not be read as "no tests"
                # if it can still be recovered: retry on the outermost braces.
                end = body.rfind("}")
                if end <= start:
                    continue
                try:
                    data = json.loads(body[start : end + 1])
                except Exception:
                    continue

            for suite in data.get("testResults") or []:
                # jest reports an absolute path; make it repo-relative so the
                # id's head matches the paths listed in the patches.
                path = suite.get("name") or ""
                if repo_prefix in path:
                    path = path.split(repo_prefix, 1)[1]
                path = path.replace("\\", "/")

                assertions = suite.get("assertionResults") or []

                if not assertions:
                    # A suite that fails to load reports zero assertions plus a
                    # message. Recording it keeps a broken import visible rather
                    # than letting the file silently vanish from the counts.
                    if suite.get("status") == "failed" or suite.get("message"):
                        failed_tests.add(f"{path}::<suite failed to load>")
                    continue

                for a in assertions:
                    name = a.get("fullName") or a.get("title") or ""
                    if not name:
                        continue
                    test_id = f"{path}::{name}" if path else name
                    status = (a.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        # pending / skipped / todo / disabled all mean "did not
                        # run to a pass" without being a failure.
                        skipped_tests.add(test_id)

        # tape output, for the packages that have not migrated to jest yet.
        # tape speaks TAP: a "# name" heading opens a test and the "ok N" /
        # "not ok N" lines under it are its individual assertions. That is looser
        # than jest's JSON -- there is no per-test structure, only a stream -- so
        # a heading is folded into one test id and fails if any assertion under
        # it fails. Headings with no assertions are dropped rather than counted
        # as passes, and tape's own trailing summary headings (# tests, # pass,
        # # fail, # ok) are not tests at all.
        #
        # Each file is run separately so its path can be carried on the marker;
        # a TAP stream on its own would not say which file a test came from.
        tap_re = re.compile(
            re.escape(_TAP_BEGIN)
            + r"\s+(?P<path>\S+)\s+=====\s*(?P<body>.*?)"
            + re.escape(_TAP_END),
            re.S,
        )
        summary_re = re.compile(r"^(tests|pass|fail|ok|not ok|skip|todo)\b", re.I)

        for match in tap_re.finditer(log):
            path = match.group("path")
            current: Optional[str] = None
            counts: dict[str, list[int]] = {}

            for raw in match.group("body").splitlines():
                line = raw.strip()
                if line.startswith("#"):
                    name = line.lstrip("#").strip()
                    if not name or summary_re.match(name):
                        current = None
                        continue
                    current = name
                    counts.setdefault(current, [0, 0])
                elif line.startswith("not ok "):
                    if current is not None:
                        counts[current][1] += 1
                elif line.startswith("ok "):
                    if current is not None:
                        counts[current][0] += 1

            for name, (ok, bad) in counts.items():
                if ok + bad == 0:
                    continue
                test_id = f"{path}::{name}"
                if bad:
                    failed_tests.add(test_id)
                else:
                    passed_tests.add(test_id)

        # A retried test can be reported twice; enforce one bucket each, or the
        # stage comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# The generated dataset routes by PR number, not by repo name.
#
# Instance.create keys on f"{pr.org}/{pr.number_interval}" whenever
# number_interval is set, and falls back to f"{pr.org}/{pr.repo}" only when it is
# not. The raw dataset leaves it None, so the ("pnpm", "pnpm") registration above
# is enough to build. But build_dataset writes number_interval equal to the PR
# number into ./data/dataset/pnpm__pnpm_dataset.jsonl, so feeding that file back
# in -- which is what the multi-arch image pass takes -- fails every record with
#     ValueError: Instance 'pnpm/3206' is not registered.
#
# Registering the same class under each PR number makes the config accept either
# file. Nothing else changes: image_name() is built from pr.org and pr.repo, so
# these still resolve to mswebench/pnpm_m_pnpm and reuse the images already
# built, and the generated scripts are untouched.
for _pr_number in ("2965", "3035", "3080", "3091", "3206"):
    Instance.register("pnpm", _pr_number)(Pnpm)
