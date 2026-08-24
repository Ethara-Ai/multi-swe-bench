"""leapdao/leap-contracts harness config.

Dataset shape: ONE pull request (#267). Rule-4 row 1, so the base is per-PR and
carries the COMPLETE history scrub; see the "Image layout" note below.

Toolchain, read off the repo at base commit 9389ccd (2019-12-31):

  * Node 10          -- .travis.yml pins `node_js: 10`; scrypt@6.0.3 is a native
                        dependency that does not build on Node >= 12.
  * Yarn 1 classic   -- yarn.lock is v1; the node:10 image already ships it.
  * Truffle 5.0.31   -- pinned by yarn.lock, resolved from `truffle@^5.0.0`.
  * Ganache-cli 6.5.1 -- started by the repo's own scripts/test.sh.
  * solc 0.5.2       -- truffle-config.js pins `compilers.solc.version = 0.5.2`.

Two things about that toolchain need explicit handling, and both were verified
inside a real container before this file was written.

1. Debian buster is archived.
   node:10.24.1-buster is Debian 10, which moved to archive.debian.org, so a
   plain `apt-get update` fails on both deb.debian.org and security.debian.org.
   The sed pair below is the same rewrite `Image._get_apt_update_command()`
   applies to its own DEPRECATED_DEBIAN_IMAGES list -- node:10 is simply not on
   that list, and this file overrides dockerfile() anyway, so it is spelled out
   here.

2. Truffle can no longer download solc 0.5.2.
   All three of truffle 5.0.31's built-in compilerRoots are dead or moved
   (relay.trufflesuite.com, solc-bin.ethereum.org, ethereum.github.io/solc-bin),
   so `truffle compile` fails with

       - Fetching solc version list from solc-bin. Attempt #1..#3
       Error: Could not find a compiler version matching 0.5.2.

   before it ever reaches the network check. VersionRange.load() consults the
   local cache first, so the fix is to seed that cache at BASE build time from
   binaries.soliditylang.org, the host solc-bin actually redirects to today.
   The cache lives at ~/.config/truffle/compilers/node_modules/ and the file
   name must be the exact `soljson-v<version>+commit.<hash>.js` truffle asks
   for. Verified: with the file in place `truffle compile` reports
   "Compiled successfully using: solc: 0.5.2+commit.1df8f40c".

Image layout -- the `mvdan/sh.py` / `Borewit/music_metadata.py` shape
---------------------------------------------------------------------
  base-pr-<N>   toolchain, the seeded solc, the clone, this PR's base commit,
                then the COMPLETE history scrub: `Image._HARDENING_BLOCK`
                verbatim -- gc, repack and all four integrity asserts. Nothing
                is left for the PR layer to finish.

  pr-<N>        deliberately thin: the patches, the run scripts, prepare.sh,
                CMD. NO scrub block at all.

The scrub lives in the base and only in the base. It opens with
`git checkout --detach "${BASE_COMMIT}"`, so it can only run where BASE_COMMIT
is a real value -- and it is real here because `dependency()` returns a str,
which is what makes build_dataset.py:625-629 pass REPO_URL and BASE_COMMIT as
build args. An Image-dependency layer receives no build args at all.

That is also why the tag is `base-pr-<N>` rather than a shared era tag: the
prune needs a pinned HEAD, and pinning a SHARED base would fix it to whichever
PR built it first. With a one-PR dataset this costs nothing -- two images either
way.

No binary lift is needed. Both patches were checked for `Binary files ... differ`
sections and have none, so there is nothing that has to be read out of git
objects before the prune.

Test reporting
--------------
Mocha's `tap` reporter is used instead of the default `spec`. spec prints only
the leaf title of a passing test but spreads a failing test's title over several
indented lines of the numbered failure list, so pass names and fail names never
match and f2p comes out empty. TAP prints `test.fullTitle()` on one line for
pass, fail and pending alike -- here that is
`Contract: PoaOperator Test Slot Management should ...`, because truffle wraps
each `contract(...)` block in a `describe('Contract: ...')`.

Truffle 5.0.31's CLI has no `--reporter` flag; the reporter is a truffle-config
key (`mocha: { reporter }`) that truffle forwards straight to Mocha. Rather than
sed-editing the existing object literal, enable-tap.sh appends one CommonJS
statement to the end of truffle-config.js. It runs inside the three run scripts,
never in prepare.sh, so the working tree that check_git_changes.sh inspects at
build time stays clean.

Measured in the container, all three stages, before this file existed:

  baseline (no patch)     122 pass / 0 fail
  test patch only         116 tests, 96 pass / 20 fail
  test patch + fix patch  125 pass / 0 fail

so the f2p set is real and non-empty.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# solc 0.5.2, the version truffle-config.js pins. The `+commit.<hash>` suffix is
# part of the file name truffle looks for in its cache, so it cannot be derived
# from the version alone. sha256 recorded from the file actually used for the
# verified compile.
SOLC_VERSION = "0.5.2"
SOLC_BUILD = "v0.5.2+commit.1df8f40c"
SOLC_SHA256 = "f855a1e0816ac4426b8a9eebf37273b89acbb18b076a519935a05ca212c71f34"
SOLC_URL = f"https://binaries.soliditylang.org/bin/soljson-{SOLC_BUILD}.js"

# Truffle's compiler cache. VersionRange.getSatisfyingVersionFromCache() reads
# path.resolve(os.homedir(), ".config", "truffle", "compilers", "node_modules").
TRUFFLE_COMPILER_CACHE = "/root/.config/truffle/compilers/node_modules"

# One place for the test command so run.sh / test-run.sh / fix-run.sh cannot
# drift apart.
#
# `yarn run test` is the repo's own scripts/test.sh, used deliberately instead of
# a hand-rolled `truffle test`: that script starts ganache-cli with the four
# hard-coded private keys and balances the suites depend on (heartbeats.js and
# poaOperator.js assert against alicePriv 0x278a5de7..., which is accounts[0]
# there) and with --gasLimit 9000000, which several deployments need. Re-typing
# those keys here would be a second source of truth for them.
#
# The reporter comes from enable-tap.sh, not from a CLI flag, so the argument
# hand-off through yarn -> scripts/test.sh -> truffle is left untouched.
TEST_CMD = "yarn run test"


class LeapContractsImageBase(Image):
    """Per-PR base for leapdao/leap-contracts.

    Pinned to this PR's BASE_COMMIT and carrying the COMPLETE history scrub --
    gc, repack and all four integrity asserts -- which is why `pr-<N>` has no
    scrub block at all. See the module docstring for why the tag is per-PR.
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

    def dependency(self) -> Union[str, "Image"]:
        # 10.24.1 is the last Node 10. Pinned rather than floating on `node:10`
        # so the image cannot silently change under a rebuild.
        return "node:10.24.1-buster"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # Debian 10 is archived. Both deb.debian.org and security.debian.org 404
        # for buster now, and the last Release file is long past its
        # Valid-Until, so the date check has to be switched off as well.
        # buster-updates has no archive counterpart at all and is dropped.
        apt_block = (
            "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list \\\n"
            " && sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list \\\n"
            " && sed -i '/buster-updates/d' /etc/apt/sources.list \\\n"
            " && printf 'Acquire::Check-Valid-Until \"false\";\\n' > /etc/apt/apt.conf.d/99no-check-valid \\\n"
            " && apt-get update \\\n"
            " && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates curl netcat-openbsd python2.7 build-essential \\\n"
            " && rm -rf /var/lib/apt/lists/*"
        )

        # node-gyp shells out to `python`/`python2` when it builds scrypt@6.0.3,
        # a plain (not optional) dependency of this repo. buster is the last
        # Debian that still carries python2.7, and the node:10 image leaves it
        # unlinked.
        python_block = (
            "RUN ln -sf /usr/bin/python2.7 /usr/local/bin/python2 \\\n"
            " && ln -sf /usr/bin/python2.7 /usr/local/bin/python"
        )

        # Seed truffle's compiler cache -- see point 2 of the module docstring.
        # Done in the BASE because it needs the network and belongs to the
        # toolchain, not to the PR. The checksum turns a silently truncated or
        # redirected download into a failed build instead of a compile error
        # that reads like a bug in the contracts.
        solc_block = (
            f"RUN mkdir -p {TRUFFLE_COMPILER_CACHE} \\\n"
            f" && curl -fsSL -o {TRUFFLE_COMPILER_CACHE}/soljson-{SOLC_BUILD}.js \\\n"
            f'      "{SOLC_URL}" \\\n'
            f' && echo "{SOLC_SHA256}  {TRUFFLE_COMPILER_CACHE}/soljson-{SOLC_BUILD}.js" | sha256sum -c -'
        )

        # The COMPLETE scrub -- gc, repack and all four integrity asserts -- lives
        # here and only here. `Image._HARDENING_BLOCK` is used verbatim rather
        # than a hand-rolled variant so the asserts can never quietly diverge
        # from the harness's own definition; it already carries the submodule
        # pass as its second RUN.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere and cannot drift.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file:
        #
        #     if cls.SYNTAX_DIRECTIVE in raw: return raw     (image.py:316-317)
        #
        # and the directive has to stay. Dropping it to re-enable the enhancer
        # would let _standardize_repo_fetch rewrite the clone and append a SECOND
        # copy of the hardening block.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            apt_block,
            python_block,
            solc_block,
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class LeapContractsImageDefault(Image):
    """Per-PR image: stage the patches and run scripts, install dependencies and
    warm the contract build.

    Carries no history scrub -- `base-pr-<N>` already ran the complete one.
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
        return LeapContractsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "enable-tap.sh",
                """#!/bin/bash
# Switch mocha to the TAP reporter. Truffle 5.0.31 has no --reporter CLI flag;
# it reads `mocha` out of truffle-config.js and hands it to Mocha as-is.
#
# The file already ends in `module.exports = { ... }`, so one appended CommonJS
# statement is enough -- no regex has to find its way into the object literal.
#
# Called from run.sh / test-run.sh / fix-run.sh, never from prepare.sh: this
# dirties a TRACKED file, and prepare.sh asserts a clean tree at build time.
# Run from the repo root; every caller cds there first.
set -euo pipefail

if grep -q 'MSB_TAP_REPORTER' truffle-config.js; then
    exit 0
fi

cat >> truffle-config.js <<'MSB_TRUFFLE_EOF'

/* MSB_TAP_REPORTER */
module.exports.mocha = Object.assign({}, module.exports.mocha, {
  reporter: 'tap',
  timeout: 300000,
});
MSB_TRUFFLE_EOF
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
git checkout "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

# --frozen-lockfile makes yarn FAIL rather than silently rewrite yarn.lock.
# The repo's postinstall runs `cp -n .env.template .env`, and .env is covered by
# the repo's own `*.env` ignore rule, so the tree stays clean.
yarn install --frozen-lockfile

# yarn must not have altered a tracked file.
bash /home/check_git_changes.sh

# Warms build/contracts (gitignored) so the test stages do not pay the full
# ~40s solc pass, and -- more importantly -- proves at BUILD time that the
# compiler seeded into the base image is the one truffle-config.js asks for. A
# missing solc would otherwise surface much later as an identical failure in
# every stage, i.e. an empty f2p set.
node_modules/.bin/truffle compile

bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{base_sha}"
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

bash /home/enable-tap.sh

# scripts/test.sh launches ganache-cli in the background and calls truffle
# immediately, without waiting for port 8545. Truffle's own startup and compile
# pass is the de facto delay, and that has been enough in every observed run --
# but if it ever is not, the whole suite errors out and no TAP line is printed
# at all. Retrying once on an empty result turns that race into a slow run
# instead of an empty test set.
OUT="$({test_cmd} 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -qE '^(ok|not ok) '; then
    OUT="$({test_cmd} 2>&1 || true)"
fi
printf '%s\\n' "$OUT"
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

bash /home/enable-tap.sh

if grep -qE '^diff --git a/(package\\.json|yarn\\.lock)' /home/test.patch; then
    yarn install || true
fi

OUT="$({test_cmd} 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -qE '^(ok|not ok) '; then
    OUT="$({test_cmd} 2>&1 || true)"
fi
printf '%s\\n' "$OUT"
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/enable-tap.sh

if grep -qhE '^diff --git a/(package\\.json|yarn\\.lock)' /home/test.patch /home/fix.patch; then
    yarn install || true
fi

OUT="$({test_cmd} 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -qE '^(ok|not ok) '; then
    OUT="$({test_cmd} 2>&1 || true)"
fi
printf '%s\\n' "$OUT"
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_command = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- {tag} is pinned to this PR's base commit and has already run
        # the full scrub (gc, repack, all four asserts), so there is nothing left
        # to prune here.
        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("leapdao", "leap-contracts")
class LeapdaoLeapContracts(Instance):
    """Harness instance for leapdao/leap-contracts -- Truffle + Mocha TAP output."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LeapContractsImageDefault(self.pr, self._config)

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
        """Parse Mocha's TAP reporter output as emitted through truffle test.

            ok 1 Contract: Bridge Test Bridge should allow to propose and finalize
            not ok 58 Contract: PoaOperator Test Slot Management should allow ...
              ---
              ...
            ok 3 some pending test # SKIP -
            # tests 122
            # pass 122
            # fail 0

        The name is `test.fullTitle()`, identical on the pass, fail and pending
        lines, which is what keeps the f2p/p2p set comparison meaningful.
        Truffle prefixes each suite with `Contract: ` because it wraps every
        `contract(...)` block in a `describe()`.

        Mocha also reports a failing hook as its own entry, e.g.
        `Contract: PosOperator "after each" hook: after test`. Those appear only
        in a failing stage and never as a pass, so they drop out of f2p on their
        own; they are left in failed_tests rather than filtered, so the failure
        count still reflects what actually went wrong.

        Diagnostics are indented two spaces by the TAP reporter, so anchoring at
        `^ok` / `^not ok` cannot match a stack frame. Truffle's own compiler
        output and ganache noise never start at column 0 with those tokens.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_ok = re.compile(r"^ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = re_not_ok.match(line)
            if m:
                name = re_directive.sub("", m.group(1)).strip()
                if name:
                    failed_tests.add(name)
                continue

            m = re_ok.match(line)
            if m:
                raw_name = m.group(1)
                is_skip = bool(re_directive.search(raw_name))
                name = re_directive.sub("", raw_name).strip()
                if not name:
                    continue
                if is_skip:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        # A title that failed anywhere wins over the same title passing
        # elsewhere, so a duplicated full title never looks green by accident.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
