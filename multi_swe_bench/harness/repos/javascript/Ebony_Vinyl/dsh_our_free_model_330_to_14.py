import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Ebony-Vinyl/dsh-our-free-model -- single era, PRs #14 .. #330 (9 instances)
#
# Naming: this file, its Instance class and its base image tag are all named
# after the PR range it spans, #330 down to #14. The interval name itself is
# spelled HIGH_to_LOW because that is what the pipeline's own resolver parses
# (script1/validate_dataset.py:106):
#
#     ^(?P<org>[^/]+)/(?P<repo>.+?)_(?P<hi>\d+)_to_(?P<lo>\d+)$
#
# then admits a record when `lo <= pr.number <= hi`. Spelling it LOW_to_HIGH
# would invert the bounds and the interval could never match a record.
#
# Registration is DUAL, exactly as HyNetworks/hysteria_1662_to_1624.py:274-275
# does it, because Instance.create() picks the key from the record
# (instance.py:41-51):
#
#   * `Ebony-Vinyl/dsh_our_free_model_330_to_14` -- used once a record carries
#     `number_interval`. The resolver finds it from the bare repo name by way of
#     `repo.replace("-", "_")`, so the underscored spelling here is reachable
#     from the hyphenated repo in the JSONL.
#   * `Ebony-Vinyl/dsh-our-free-model` -- the fallback for a record with neither
#     `tag` nor `number_interval`, which is what the 9 rows of this dataset
#     currently carry. Without this second key every instance would be SILENTLY
#     skipped, so it stays until the rows are stamped.
#
# The org keeps its hyphen in both. Only the module, directory and interval are
# underscored, because a hyphenated package cannot be reached by
# `from .<module> import *`.
#
# Why ONE era file for a #14..#330 span -- verified at all nine base commits:
#   * engines.node is "^22.19.0 || >=24.0.0" at every one of them, so a single
#     node:22 image serves the whole set.
#   * "dependencies" is absent and "devDependencies" is {"typescript": "..."}
#     at every one of them. The offline suites import nothing outside
#     node: builtins, which is why CI states outright: "No install step and no
#     dependencies: the suites are plain Node scripts". There is therefore no
#     package manager era to split on -- no lockfile exists at any base commit.
#   * scripts/test-all.mjs exists at all nine and emits the SAME result line
#     byte-for-byte:
#       `${ok ? 'ok  ' : 'FAIL'} ${name.padEnd(14)} ${String(ms).padStart(5)} ms`
#     so one parse_log covers the span.
#
# The ONE thing that does differ across the span is the mode flag -- see
# _test_command() below.
# ---------------------------------------------------------------------------

_INTERVAL = "dsh_our_free_model_330_to_14"

# Pinned to an exact patch release, not the floating "22-bookworm" tag, so the
# shared base layer is reproducible across rebuilds. 22.23.4 satisfies the
# engines constraint below with room to spare; prepare.sh re-asserts it at
# build time rather than trusting the tag.
NODE_IMAGE = "node:22.23.4-bookworm"

# PR-independent tag => SHARED BASE (one image for all nine PRs). The base
# therefore keeps FULL git history and never mentions ${BASE_COMMIT}; the pin
# lives in prepare.sh and the history scrub in the PR layer.
# The interval NAME is HIGH_to_LOW because the pipeline's resolver parses it that
# way (see the header). The image TAG is LOW_to_HIGH because the Dockerfile QC
# reads a tag as `base-<lo>_to_<hi>` and flags an interval that does not contain
# the PR's number -- `base-330_to_14` would read as an inverted, empty range.
# Nothing parses the tag, so it is free to use the spelling its own reader
# expects; the two strings serve two different consumers on purpose.
BASE_TAG = "base-14_to_330"

# `--mode` was introduced with the 2.0.0 tree. At #14/#17/#22/#47 (1.3.x)
# scripts/test-all.mjs parses only `--only` and has no mode concept, and that
# era's CI ran the suite set bare (`npm test`). From #191 on, the runner
# validates --mode against all|contributor|release and CI runs
# `--mode contributor` for pull requests. Passing --mode to the 1.3.x runner
# would be silently ignored (it would run every suite anyway), so the flag is
# gated on the PR number rather than sent unconditionally.
#
# This selects the command PER PR, not per script: all three run scripts of a
# given instance embed the identical command, which is what makes the f2p
# comparison meaningful.
#
# The 1.3.x roster therefore still runs the three maintainer suites that
# `--mode contributor` drops for #191+ (`catalog`, `manifest`, `release`).
# test.patch cannot disturb them -- package.json's "files" list names neither
# scripts/ nor .github/ at those commits, and every test_patch file for
# #14/#17/#22/#47 lives in one of those two directories -- but fix.patch is
# under no such restriction and #47's does edit shipped content. Since the
# command cannot be narrowed on this tree, the exclusion is applied downstream
# in parse_log instead; see _MAINTAINER_ONLY_SUITES below.
MODE_FLAG_MIN_PR = 191


# Three suites in the roster judge RELEASE ENGINEERING -- the state of the
# published release artifacts -- rather than the code a contributor changed.
# They are maintainer scope, and the repository says so itself: from #191,
# `scripts/test-all.mjs --mode contributor` drops exactly these three from the
# roster (quoting the 1.4.x runner):
#
#     ['catalog', 'catalog-test.mjs', mode === 'contributor' ? ['--contributor'] : []],
#     ...
#     ].filter(([name]) => (mode === 'contributor' ? !['manifest', 'release'].includes(name)
#
# i.e. `manifest` and `release` are filtered out wholesale, and `catalog` is
# run with `--contributor`, which skips its revision cross-check ("revision
# cross-check skipped" in its own output).
#
# The 1.3.x era (#14/#17/#22/#47) has no `--mode` flag at all, so the runner
# always executes the full maintainer roster and the harness has to apply the
# same exclusion itself. Each of the three breaks a different way:
#
#   catalog  -- asserts a property of UPSTREAM GIT HISTORY, not of the code
#               under test: catalog-test.mjs recomputes the recorded catalog
#               revision with `git log -1 --format=%H -- <release paths>` and
#               compares it to the revision the catalog records claim.
#               fix_patch updates those records to the commit the PR was
#               really merged as, which cannot exist inside an image pinned to
#               the PR's BASE commit with every later commit pruned away.
#
#   manifest  -- cross-checks the committed digests in feed/manifest.json
#   release      against the bytes on disk. A fix_patch that edits any shipped
#               file (#47 grows client.js by 185 bytes) invalidates those
#               digests unless the author also re-ran
#               `node scripts/build-manifest.mjs` and committed the result.
#               #47's diff touches only client.js and two docs files, so the
#               digests go stale the moment the fix lands and both suites
#               report drift ("client.js: size 117070 in the manifest, 117255
#               on disk"). That is the authoring omission the repo's own
#               contributor mode exists to forgive, not a defect in the change.
#
# All three therefore pass in the run and test stages and FAIL in the fix
# stage -- a PASS->FAIL transition that Report.check() rule 2 rejects outright,
# invalidating the PR for a reason that has nothing to do with its changes.
#
# Cost of the wider scope: #22 did earn `release` as a genuine f2p transition
# and now loses it. It keeps five others (feed, forward, offline, retry-safety,
# updater), so it stays valid, and matching the repository's own contributor
# contract is worth more than that one credit.
_MAINTAINER_ONLY_SUITES = frozenset({"catalog", "manifest", "release"})


def _excluded_suites(pr: PullRequest) -> frozenset[str]:
    """Suites the harness must drop because `--mode contributor` cannot."""
    if pr.number < MODE_FLAG_MIN_PR:
        return _MAINTAINER_ONLY_SUITES
    return frozenset()


def _test_command(pr: PullRequest) -> str:
    if pr.number >= MODE_FLAG_MIN_PR:
        return "node scripts/test-all.mjs --mode contributor"
    return "node scripts/test-all.mjs"


CHECK_GIT_CHANGES = """#!/bin/bash
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

"""

ENV_BLOCK = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
"""

# scripts/test-all.mjs spawns each suite as a child of its own and forwards the
# child's stdout/stderr verbatim, so the stage log interleaves child chatter
# with the runner's own result lines. parse_log() separates them by column:
# see the note on the regex in parse_log().
RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
"""
)

# Binary changes MUST be excluded, and absence of a "GIT binary patch" stanza
# is NOT evidence that there are none.
#
# The fix_patch of #47, #191 and #193 records PNG changes as a bare
#
#     Binary files a/docs/.../x.png and b/docs/.../x.png differ
#
# with NO payload and an abbreviated index line. `git apply` then refuses the
# whole patch -- "cannot apply binary patch to '<path>' without full index
# line" -- so the fix stage captures zero tests and Report.check() rule 1
# rejects the instance. Nothing in the test command is at fault and nothing in
# the log says "binary", which is what makes this one expensive to find.
#
# apply_patches derives the exclusion list from the patch bodies themselves, so
# it stays correct if another PR is added later. It is not tolerant: a genuine
# apply failure still aborts the script.
APPLY_FN = r"""
apply_patches() {
    local excl=() f
    for f in $(awk '/^diff --git /{p=$3; sub(/^a\//,"",p)} /^Binary files .* differ$/{if(p!="")print p}' "$@" | sort -u); do
        excl+=("--exclude=$f")
        echo "apply_patches: excluding $f (binary change carries no payload in the patch)"
    done
    git apply --whitespace=nowarn "${excl[@]}" "$@"
}
"""

APPLY_TEST = APPLY_FN + "apply_patches /home/test.patch\n"
APPLY_BOTH = APPLY_FN + "apply_patches /home/test.patch /home/fix.patch\n"

TEST_BLOCK = """
__TEST_CMD__

"""

RUN_SH = RUN_HEADER + TEST_BLOCK
TEST_RUN_SH = RUN_HEADER + APPLY_TEST + TEST_BLOCK
FIX_RUN_SH = RUN_HEADER + APPLY_BOTH + TEST_BLOCK

# ---------------------------------------------------------------------------
# prepare.sh -- three sections, in order: PIN, PROVISION, GATE.
#
# PROVISION is empty by evidence, not by omission: at all nine base commits the
# manifest declares no "dependencies" and only "typescript" under
# "devDependencies" (needed by `npm run typecheck`, which is a separate CI job
# and not the graded command). Running `npm install` here would be worse than
# useless -- no lockfile exists at any base commit, so it would resolve
# whatever the registry serves today into an era it was never tested against.
#
# Because nothing is installed, the GATE cannot be an import check. It instead
# makes the runner itself prove it loads and can build its suite roster:
# `--only <name that matches nothing>` walks the full argv parser and suite
# table, then exits 1 with a known message WITHOUT executing a single suite.
# That message is present at all nine base commits. A runner that cannot parse,
# or a tree checked out to the wrong place, fails the grep.
# ---------------------------------------------------------------------------
PREPARE_SH = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
# ---- Section 1: PIN ----------------------------------------------------
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION ----------------------------------------------
# There is nothing to install, and this section PROVES that rather than
# asserting it. At every base commit in #14..#330 the manifest declares no
# "dependencies", and its only devDependency is typescript, which belongs to
# the separate `npm run typecheck` CI job and not to the graded command. The
# check below fails the build if that ever stops holding, so a commit that
# introduces a runtime dependency cannot silently run un-provisioned.
node -e "const p = require('./package.json'); const d = Object.keys(p.dependencies || {}); if (d.length > 0) { console.error('package.json declares runtime dependencies that prepare.sh does not install: ' + d.join(', ')); process.exit(1) } console.log('no runtime dependencies to install')"

# ---- Section 3: GATE ---------------------------------------------------
node --version
node -e "const v = process.versions.node.split('.').map(Number); if (!((v[0] === 22 && v[1] >= 19) || v[0] >= 24)) { console.error('node ' + process.versions.node + ' does not satisfy engines ^22.19.0 || >=24.0.0'); process.exit(1) }"
test -f scripts/test-all.mjs
# The runner reports an empty suite selection by printing a known message and
# exiting NON-ZERO, so a success here would itself be wrong. Both outcomes are
# asserted and neither is tolerated: the `if` fails the build on an unexpected
# exit 0, and the grep fails it when the runner no longer loads or no longer
# builds its suite table. No suite is executed, so this cannot mask a genuinely
# failing test. No step in this script tolerates a non-zero exit status.
if node scripts/test-all.mjs --only msb-no-such-suite >/tmp/msb_gate.log 2>&1; then
    echo "gate: runner exited 0 for a selection matching no suite" >&2
    exit 1
fi
grep -q 'selected no suite' /tmp/msb_gate.log
echo "DEPS_OK"
"""
)


class DshOurFreeModelImageBase_330_TO_14(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Line 1 is the BuildKit syntax directive, which is MANDATORY here:
        # DockerfileEnhancer.enhance() returns this text untouched when it is
        # present (image.py:317-318). Without it, _standardize_repo_fetch()
        # would rewrite the clone below into clone + checkout ${BASE_COMMIT} +
        # the hardening block -- and since this tag is PR-independent, that one
        # shared image would be pinned to whichever PR the builder happened to
        # reach first, and stripped of the history the other eight need.
        # Opting out means nothing is injected, so the proxy ARGs, the ENV
        # block and the certificate farm are written out by hand below.
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT
# ^ DECLARED, and never expanded anywhere below. The harness passes
# --build-arg BASE_COMMIT to every base build (build_dataset.py:629), so
# declaring it silences BuildKit's unused-arg warning. CONSUMING it is what
# would pin this shared image to one PR's commit and strand the other eight.

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CI=true \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} shared base (node22, dependency-free offline suites)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

# Certificate trust is established BEFORE the first network call below.
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    bash \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN node --version && npm --version

WORKDIR /home/

# A FULL clone, deliberately. This base is shared by every PR in the dataset,
# so each one must still be able to reach its own base commit from here; the
# scrub happens later, in the per-PR layer. The repo's own catalog suite also
# walks file history, which is why CI checks out with fetch-depth: 0.
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class DshOurFreeModelImageDefault_330_TO_14(Image):
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
        return DshOurFreeModelImageBase_330_TO_14(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return (
            script.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__TEST_CMD__", _test_command(self.pr))
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(PREPARE_SH)),
            File(".", "run.sh", self._render(RUN_SH)),
            File(".", "test-run.sh", self._render(TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        # Never enhanced: dependency() returns an Image, so enhance() hands the
        # raw text back at image.py:315-316. Everything this layer needs is
        # written out here, including the history scrub that a per-PR base
        # would otherwise have done.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("Ebony-Vinyl", "dsh-our-free-model")
@Instance.register("Ebony-Vinyl", _INTERVAL)
class DSH_OUR_FREE_MODEL_330_TO_14(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DshOurFreeModelImageDefault_330_TO_14(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # One test == one suite, named as scripts/test-all.mjs names it. The
        # runner prints exactly one of these per suite:
        #
        #   ok   recovery         812 ms
        #   FAIL channel-pack    1204 ms
        #
        # from `${ok ? 'ok  ' : 'FAIL'} ${name.padEnd(14)} ${ms.padStart(5)} ms`
        # -- identical at all nine base commits.
        #
        # The anchor is load-bearing, so the line is only rstripped, NEVER
        # lstripped. Two other kinds of line can otherwise be mistaken for a
        # result:
        #   * forwarded child output, which the suites print at column 0 as
        #     free text ("ok  source qoder: ..."); it never matches because the
        #     name group is a single token followed by digits and " ms".
        #   * the runner's own failure detail, which it re-prints from the
        #     child with a seven-space indent. lstripping would turn such a
        #     line into a phantom result, and a phantom that flips between
        #     stages corrupts the f2p classification.
        result_re = re.compile(
            r"^(?P<status>ok|FAIL)\s+(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s+\d+ ms$"
        )

        excluded = _excluded_suites(self.pr)
        seen: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()

            match = result_re.match(line)
            if not match:
                continue

            name = match.group("name")

            if name in excluded:
                continue

            # Suite names are unique within a roster, so this is defensive. It
            # is positional and therefore stable across the three stages, which
            # is what cross-stage name consistency requires.
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"

            if match.group("status") == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

        # The runner has no skip concept: a suite either exits 0 or it does not,
        # so skipped_tests stays empty by construction.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
