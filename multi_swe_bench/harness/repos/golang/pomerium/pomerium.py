import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Toolchain pinned from the `go` directive in go.mod at PR #1479's base.sha
# (7613f4c6, measured -> "go 1.14"), NOT "golang:latest". pomerium at this era
# pulls x/sys / k8s deps that no longer assemble on a modern toolchain, so the
# era-matched image is required.
_GO_MINOR = "1.14"

# Namespace for synthetic entries recorded when a whole Go package fails to
# compile. See Pomerium.parse_log for why these exist and why they cannot
# distort the f2p/p2p/n2p classification.
_BUILD_FAILED_PREFIX = "BUILD_FAILED::"

# `-run` value chosen to match no test at all: used to compile every test binary
# (and therefore pull test-only dependencies into the module cache) without
# actually executing anything.
_NO_MATCH_RUN = "ZZZ_MSB_NEVER_MATCHES"

# Separator between a Go import path and a test name. Go's test namespace is
# per-package, but `go test ./...` prints only the bare function name on each
# result line, so a flat set of names silently merges homonyms across packages.
# Measured on PR #1479's fix stage: 812 result lines collapsed to 783 names, i.e.
# 29 results lost. `Test` alone is a top-level function in three packages
# (directory/azure, directory/github, directory/gitlab) and `TestDB` in two.
# Because reconciliation resolves a merged name in favour of failure, one
# package's failure was being attributed to every homonym -- which is how a
# single flaky test in directory/azure presented to gen_report as
# `Test(run=PASS, test=PASS, fix=FAIL)` and invalidated the whole instance.
_PKG_SEP = "::"

# Fallback qualifier for results that reach EOF without a package summary line
# (should not happen: measured 0 orphans across all three stage logs, but a
# panic that kills the test binary mid-package could produce them).
_UNKNOWN_PKG = "UNKNOWN_PKG"

# Written by prepare.sh, echoed into every stage log by the run scripts, and read
# back by parse_log. See _FLAKE_PROBE in _PREPARE_SH.
_FLAKY_FILE = "/home/.flaky_tests"
_FLAKY_MARKER = "MSB_FLAKY::"

# How many times prepare.sh runs the suite on the pristine base to look for
# nondeterminism. A test that flips on a coin toss is caught with probability
# 1 - 2^-(N-1): 75% at N=3, 94% at N=5. Each probe run costs roughly one test
# suite (~1-2 min here, build cache already warm).
_FLAKE_PROBE_RUNS = 3

# Seed quarantine, applied on top of whatever the probe finds. Empirical
# detection is sampling, so a known-bad test is pinned here rather than left to
# chance.
#
# directory/azure `Test` asserts a fixed ORDER on the slice returned by
# azure.go:105 UserGroups -> dc.CurrentUserGroups(), which is built by ranging a
# map and never sorted. Go randomises map iteration per process, so the assertion
# is a coin flip on a 2-element result. It won in the earlier build under data/
# and lost in the rebuild under pipeline_output/, where it became the sole reason
# the instance was rejected. The fix patch does not touch that package: the
# failure is upstream nondeterminism, not a regression this PR caused.
_QUARANTINED_TESTS = frozenset(
    {
        f"github.com/pomerium/pomerium/internal/directory/azure{_PKG_SEP}Test",
    }
)


def _sanitize_patch(patch: str) -> str:
    """Drop diff sections that ``git apply`` can never take.

    Binary hunks (fonts, .ico/.png/.gif) are emitted without a full index line
    -> ``cannot apply binary patch ... without full index line``, which aborts
    the WHOLE ``git apply`` under ``set -e`` so the real source changes never
    land. They are irrelevant to the Go tests.

    ``go.sum`` / ``go.work.sum`` used to be stripped here as well, because their
    hunks depend on the exact module graph and routinely fail to apply. That is
    no longer done: rewriting the patch in Python made the ``fix_patch`` recorded
    in the dataset differ from the bytes actually applied inside the container
    (PR #1479 shipped 6 diff sections but only 5 were applied), which is a
    correctness lie an agent is later graded against. The lock files are now
    skipped at apply time with ``git apply --exclude=go.sum`` in the run scripts,
    so the shipped patch and the applied patch describe the same change set and
    only the excluded path differs -- visibly, in the command line.
    """
    if not patch:
        return patch
    kept = []
    for sec in re.split(r"(?m)(?=^diff --git )", patch):
        if not sec:
            continue
        if "Binary files " in sec or "GIT binary patch" in sec:
            continue
        kept.append(sec)
    return "".join(kept)


# ---------------------------------------------------------------------------
# Dockerfile fragments.
#
# These are plain (non-f) strings on purpose: they contain literal ``${...}``
# shell expansions and ``{ ... }`` command groups that an f-string would try to
# interpolate. Repo/org names are spliced in by concatenation.
# ---------------------------------------------------------------------------

# golang:1.14 is Debian *buster*, which was retired to archive.debian.org on
# 2024-06-30. Every deb.debian.org / security.debian.org index for it now 404s,
# so ANY `apt-get update` fails -- including one run by a downstream consumer
# layering on top of this image. That is exactly what aborted the OpenHands
# `eval-agent-server:<sha>-pomerium_m_pomerium-pr-1479-source-minimal` prebuild
# ~6s in: its `base-image-minimal` stage runs `apt-get update` as the first
# command of a single `RUN set -eux`.
#
# This image still installs nothing (golang:1.14 already ships git and
# /etc/ssl/certs/ca-certificates.crt, all it needs). The rewrite exists purely so
# that the apt configuration handed to consumers resolves. Archived Release files
# are permanently past their Valid-Until date, so that check must be disabled
# too -- omitting it is why the generic Image._get_apt_update_command fix is not
# sufficient here.
_APT_ARCHIVE_REPAIR = """\
RUN set -eux; \\
    if grep -qE 'buster|stretch|jessie' /etc/apt/sources.list 2>/dev/null; then \\
        sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list; \\
        sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list; \\
        sed -i 's|security.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list; \\
        sed -i '/buster-updates/d; /stretch-updates/d; /jessie-updates/d' /etc/apt/sources.list; \\
        { echo 'Acquire::Check-Valid-Until "false";'; \\
          echo 'Acquire::Retries "5";'; } > /etc/apt/apt.conf.d/99msb-archive; \\
        apt-get update; \\
        rm -rf /var/lib/apt/lists/*; \\
    fi"""


def _chmod_line(repo: str) -> str:
    # The downstream evaluation harness (OpenHands' agent-server) layers on top
    # of this image and then switches to a non-root UID, but only chowns
    # /workspace -- the checkout at /home/<repo> stays root:root 0755 and the
    # agent cannot write to it. Relaxing the mode inside the RUN that created the
    # tree costs no extra layer; a later `chmod -R` would duplicate ~100 MB.
    return "    chmod -R a+rwX /home/" + repo + "; \\\n"

# Body of the history scrub. Identical in effect to Image._HARDENING_BLOCK, but
# emitted as continuation lines of a SINGLE `RUN` (see _clone_and_harden).
_HARDEN_BODY = """\
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
    if [ -f .gitmodules ]; then \\
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
    fi; \\
"""

# Must be the LAST thing in the RUN. The final assertion is also the marker that
# DockerfileEnhancer._inject_final_sanitize looks for: when it finds this string
# before the trailing CMD, with no further `git clone` / `git fetch` /
# `git remote add` after it, it leaves the Dockerfile alone instead of appending
# a second (and, here, layer-splitting) copy of Image._HARDENING_BLOCK.
_HARDEN_ASSERTS = """\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"\
"""


def _clone_and_harden(org: str, repo: str) -> str:
    """Fetch the repo and destroy its future history inside ONE layer.

    The standard path (a bare ``RUN git clone``, rewritten by
    DockerfileEnhancer._standardize_repo_fetch into clone / checkout / prune as
    separate ``RUN`` instructions) leaks the entire solution. Docker layers are
    immutable: the clone layer keeps every object, and the later prune only
    changes the final filesystem view. Verified on the shipped artifact
    ``mswebench_pomerium_m_pomerium_base-pr-1479.tar``: layer 9 (91.3 MB, the
    clone) still contains ``refs/heads/main`` at 8639d614, 10281 reachable
    commits, and commit f1daf336f -- "auth0: implement directory provider
    (#1479)" -- i.e. the exact gold patch the agent is asked to write. The prune
    layer's own ``rev-list --all --count`` assertion passes regardless, because
    it only inspects the merged view.

    Doing clone + prune in one ``RUN`` means the layer diff is computed after the
    objects are already gone, so they are never written to any layer at all.

    ``_standardize_repo_fetch`` only rewrites a line matching
    ``^RUN\\s+git\\s+clone\\s+\\S+\\s+/home/<repo>$``; a multi-command
    ``RUN set -eux; ...`` does not match, so this survives the enhancer intact
    while still receiving its infrastructure block (TARGETARCH, proxy ARGs,
    REPO_URL / BASE_COMMIT ARGs, CA-cert symlinks, OCI labels).
    """
    default_url = "https://github.com/" + org + "/" + repo + ".git"
    return (
        "RUN set -eux; \\\n"
        '    : "${BASE_COMMIT:?BASE_COMMIT build-arg is required}"; \\\n'
        '    git clone "${REPO_URL:-' + default_url + '}" /home/' + repo + "; \\\n"
        "    cd /home/" + repo + "; \\\n"
        '    git checkout --detach "${BASE_COMMIT}"; \\\n'
        "    if [ -f .gitmodules ]; then git submodule update --init --recursive; fi; \\\n"
        + _HARDEN_BODY
        + _chmod_line(repo)
        + _HARDEN_ASSERTS
    )


def _copy_and_harden(repo: str) -> str:
    """Offline variant, used when ``config.need_clone`` is false.

    NOTE: this path cannot give the same guarantee. ``COPY`` is necessarily its
    own layer, so whatever history the build context's working copy carries is
    already committed to the image before any prune can run. Prefer
    ``need_clone: true`` for anything that ships. The trailing slashes also keep
    the line from matching _standardize_repo_fetch's ``COPY`` pattern, which
    would otherwise replace it with the layer-splitting clone form.
    """
    return (
        "COPY " + repo + "/ /home/" + repo + "/\n"
        "\n"
        "RUN set -eux; \\\n"
        '    : "${BASE_COMMIT:?BASE_COMMIT build-arg is required}"; \\\n'
        "    cd /home/" + repo + "; \\\n"
        '    git checkout --detach "${BASE_COMMIT}"; \\\n'
        + _HARDEN_BODY
        + _chmod_line(repo)
        + _HARDEN_ASSERTS
    )


class PomeriumImageBase(Image):
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
        return f"golang:{_GO_MINOR}"

    def image_tag(self) -> str:
        # Per-PR (kubo model): this base bakes `git checkout ${BASE_COMMIT}` +
        # a history prune, so it must belong to exactly one PR.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo
        org = self.pr.org

        if self.config.need_clone:
            fetch = _clone_and_harden(org, repo)
        else:
            fetch = _copy_and_harden(repo)

        # No inline `# syntax` directive, so DockerfileEnhancer.enhance()
        # (build_dataset.py -> image.py) still runs and injects the shared
        # infrastructure: TARGETARCH, the proxy ARGs, REPO_URL / BASE_COMMIT,
        # the SSL_CERT_FILE / CA_CERT_PATH ENVs + CA-cert symlinks, OCI labels.
        # What it must NOT do any more is rewrite the fetch, hence the single-RUN
        # form above and the trailing CMD + assertion marker below.
        return "\n".join(
            [
                f"FROM {image_name}",
                "",
                self.global_env,
                "",
                _APT_ARCHIVE_REPAIR,
                "",
                "WORKDIR /home/",
                "",
                fetch,
                "",
                f"WORKDIR /home/{repo}",
                "",
                self.clear_env,
                "",
                'CMD ["/bin/bash"]',
                "",
            ]
        )


# ---------------------------------------------------------------------------
# Shell scripts.
#
# Built with str.replace rather than str.format so that shell parameter
# expansions (${GOPROXY:-}) and command groups do not have to be brace-escaped.
# ---------------------------------------------------------------------------

# go.sum / go.work.sum are lock files whose hunks depend on the exact module
# graph and routinely fail to apply; `-mod=mod` regenerates whatever is missing.
# Excluding them here rather than in _sanitize_patch keeps the patch that ships
# in the dataset byte-identical to the patch that is applied.
_GO_SUM_EXCLUDES = """\
PATCH_EXCLUDES=(--exclude=go.sum "--exclude=*/go.sum" \\
                --exclude=go.work.sum "--exclude=*/go.work.sum")
"""

_RUN_PREAMBLE = """\
cd /home/__REPO__
export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode

# Prefer the module cache baked at image-build time. prepare.sh only creates
# /home/.hermetic_ok once it has verified that cache resolves BOTH the baseline
# and the patched module graphs, so a repo whose warm-up could not fully resolve
# degrades to the previous online behaviour instead of hard-failing. Export
# GOPROXY yourself to override.
#
# Without this the test stage resolves the test patch's new imports with an
# implicit "latest" query: measured on PR #1479 it pulled gopkg.in/auth0.v4
# v4.7.0 while the fix stage's pinned go.mod pulled v4.6.0 -- two different
# module graphs for the same instance, and a hard requirement on network egress
# that makes `--network=none` evaluation impossible.
if [ -z "${GOPROXY:-}" ] && [ -f /home/.hermetic_ok ]; then
  export GOPROXY=off
  export GOSUMDB=off
fi

# Replay the quarantine list into THIS stage's log. parse_log reads it back from
# there, so all three stages are guaranteed to exclude an identical set of names
# even though only prepare.sh ever computed it -- an asymmetry here would itself
# fabricate transitions.
if [ -f __FLAKYFILE__ ]; then
  sed -e '/^$/d' -e 's|^|__FLAKYMARK__|' __FLAKYFILE__
fi
"""

_APPLY_FUNC = """\
# Apply strictly. The previous chain ended in `|| true`, which made a patch that
# applied to nothing indistinguishable from one that applied cleanly -- the stage
# then ran the UNPATCHED tree and reported a plausible-looking result. The
# `--reject` tier is gone for the same reason: it leaves a half-applied worktree
# plus .rej files and still exits 0.
apply_patches() {
  git apply --whitespace=nowarn "${PATCH_EXCLUDES[@]}" "$@" \\
    || git apply --whitespace=nowarn --3way "${PATCH_EXCLUDES[@]}" "$@" \\
    || {
      echo "FATAL: could not apply $*" >&2
      exit 1
    }
  if [ -z "$(git status --porcelain)" ]; then
    echo "FATAL: applied $* but the worktree is unchanged" >&2
    exit 1
  fi
}
"""

_PREPARE_SH = """\
#!/bin/bash
set -e

cd /home/__REPO__

__EXCLUDES__
# The base image already detached onto the base commit and deleted every ref, so
# re-checking-out here would be a no-op. Assert instead: a mismatch means this
# layer is sitting on a base image built for a different PR, which would silently
# emit a dataset entry for the wrong commit.
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

export GOFLAGS=-mod=mod
[ -f go.work ] && export GOWORK=off  # -mod=mod is invalid in workspace mode

# --- Hermetic warm-up ------------------------------------------------------
# Populate the module cache for BOTH the baseline and the patched module graphs
# while the builder still has network, so the three evaluation stages can run
# offline and deterministically. See the note in run.sh.
HERMETIC=1

go mod download all || HERMETIC=0
go build ./... || true

if git apply --whitespace=nowarn "${PATCH_EXCLUDES[@]}" \\
     /home/test.patch /home/fix.patch 2>/dev/null; then
  go mod download all || HERMETIC=0
  # Compile, do not run: this is what pulls test-only dependencies (and the
  # dependencies introduced by the fix patch) into the module cache.
  go test -run __NOMATCH__ -count=1 ./... || true
else
  echo "prepare: patched-graph warm-up skipped (patches did not apply cleanly)" >&2
  HERMETIC=0
fi

# The build cache now contains objects compiled from the FIX patch. Shipping
# those would put solution-derived artifacts inside the task image, so wipe it.
# The module cache holds only third-party sources and is deliberately kept.
git checkout -- .
git clean -fdq
go clean -cache

# Re-warm the build cache from the pristine baseline only.
go build ./... || true
go test -run __NOMATCH__ -count=1 ./... || true

# --- Flake probe -----------------------------------------------------------
# Run the pristine baseline suite several times and record every test whose
# verdict is not identical across all of them. A nondeterministic test is not a
# property of any patch, but the three evaluation stages are three separate
# processes, so one can hand gen_report a PASS->FAIL transition that looks
# exactly like a P2P regression and invalidates the instance. Detecting it here,
# on the unpatched tree, is the only place where "this test cannot be trusted"
# can be established independently of the patches being graded.
#
# The name is package-qualified to match parse_log; see _PKG_SEP.
: > __FLAKYFILE__
_probe="$(mktemp -d)"
_i=1
while [ "$_i" -le __PROBERUNS__ ]; do
  go test -v -count=1 ./... 2>&1 | awk '
      $1 == "---" && ($2 == "PASS:" || $2 == "FAIL:" || $2 == "SKIP:") {
          n++; st[n] = substr($2, 1, length($2) - 1); nm[n] = $3; next
      }
      ($1 == "ok" || $1 == "FAIL" || $1 == "?") && NF >= 3 {
          for (i = 1; i <= n; i++) print $2 "__PKGSEP__" nm[i] " " st[i]
          n = 0; next
      }
  ' | sort -u > "$_probe/run$_i"
  _i=$((_i + 1))
done

cat "$_probe"/run* | sort > "$_probe/all"
{
  # A stable test contributes the same "name status" pair to all N runs.
  uniq -c "$_probe/all" | awk -v n=__PROBERUNS__ '$1 != n { print $2 }'
  # ...and a test seen under two different verdicts is unstable even if the
  # counts happen to balance (e.g. it reports twice within one run).
  sort -u "$_probe/all" | awk '{ print $1 }' | uniq -d
} | sort -u > __FLAKYFILE__
rm -rf "$_probe"

echo "prepare: quarantined $(wc -l < __FLAKYFILE__) nondeterministic test(s)" >&2

# Restore, then verify. This is the first point in the build where a mutation
# could actually have survived, so it is the first point where the guard means
# anything -- the previous placement ran it twice before the only step capable
# of dirtying the tree.
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh

if [ "$HERMETIC" = "1" ]; then
  touch /home/.hermetic_ok
else
  echo "prepare: module cache incomplete; stages will require network" >&2
fi

# Same rationale as the chmod in the base image: the downstream harness runs as
# a non-root UID. Done here, inside the RUN that populated these trees, so it
# costs no extra layer.
chmod -R a+rwX /home/__REPO__ || true
chmod -R a+rwX "$(go env GOMODCACHE)" 2>/dev/null || true
chmod -R a+rwX "$(go env GOCACHE)" 2>/dev/null || true
chmod a+r __FLAKYFILE__ 2>/dev/null || true
"""

_RUN_SH = """\
#!/bin/bash
set -e

__PREAMBLE__
go test -v -count=1 ./...
"""

_TEST_RUN_SH = """\
#!/bin/bash
set -e

__PREAMBLE__
__EXCLUDES__
__APPLY__
apply_patches /home/test.patch

go test -v -count=1 ./...
"""

_FIX_RUN_SH = """\
#!/bin/bash
set -e

__PREAMBLE__
__EXCLUDES__
__APPLY__
apply_patches /home/test.patch /home/fix.patch

go test -v -count=1 ./...
"""


def _script(template: str, repo: str, base_sha: str = "") -> str:
    body = (
        template.replace("__PREAMBLE__", _RUN_PREAMBLE)
        .replace("__EXCLUDES__", _GO_SUM_EXCLUDES)
        .replace("__APPLY__", _APPLY_FUNC)
        .replace("__NOMATCH__", _NO_MATCH_RUN)
        .replace("__PROBERUNS__", str(_FLAKE_PROBE_RUNS))
        .replace("__FLAKYFILE__", _FLAKY_FILE)
        .replace("__FLAKYMARK__", _FLAKY_MARKER)
        .replace("__PKGSEP__", _PKG_SEP)
        .replace("__BASE_SHA__", base_sha)
        .replace("__REPO__", repo)
    )
    return body + "\n"


class PomeriumImageDefault(Image):
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
        return PomeriumImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(
                ".",
                "fix.patch",
                _sanitize_patch(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _sanitize_patch(self.pr.test_patch),
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                _script(_PREPARE_SH, repo, self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                _script(_RUN_SH, repo),
            ),
            File(
                ".",
                "test-run.sh",
                _script(_TEST_RUN_SH, repo),
            ),
            File(
                ".",
                "fix-run.sh",
                _script(_FIX_RUN_SH, repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}
"""


@Instance.register("pomerium", "pomerium")
class Pomerium(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PomeriumImageDefault(self.pr, self._config)

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

        # Only the per-test result lines are authoritative:
        #     --- PASS: TestFoo (0.00s)
        #     --- FAIL: TestFoo/subcase (0.01s)
        #     --- SKIP: TestFoo (0.00s)
        #
        # A loose pattern like `FAIL:?\s?(.+?)\s` also matches go's PACKAGE
        # summary lines, e.g.
        #     FAIL	github.com/pomerium/pomerium/internal/directory	1.234s
        # recording an import path as a phantom failed TEST and corrupting
        # failed_count / the f2p/p2p sets. Anchor to `^--- (PASS|FAIL|SKIP):`.
        re_result = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")

        # `go test ./...` emits one block per package, closed by a summary line:
        #     ok  	<pkg>	0.058s        ok  	<pkg>	(cached)
        #     FAIL	<pkg>	0.011s        FAIL	<pkg>	[setup failed]
        #     ?   	<pkg>	[no test files]
        # Blocks do not interleave even under parallel package execution, so the
        # results buffered since the previous summary all belong to <pkg>.
        # Verified against all three stage logs of PR #1479: 81/82/83 summary
        # lines and zero results left unattributed at EOF. The trailing
        # duration/bracket group is required so that ordinary output happening to
        # begin with "ok" or "FAIL" cannot be mistaken for a summary and
        # misattribute a whole block.
        re_pkg_end = re.compile(
            r"^(?:ok|FAIL|\?)\s+(\S+)\s+(?:\(cached\)|\[[^\]]*\]|[0-9.]+m?s)\s*$"
        )
        re_flaky = re.compile(rf"^{re.escape(_FLAKY_MARKER)}(\S+)$")

        # ...but anchoring alone then loses the ONE signal that matters most.
        # When a package does not COMPILE, go prints no `--- FAIL:` lines at all,
        # only a package summary:
        #     FAIL	github.com/pomerium/pomerium/internal/directory/auth0 [setup failed]
        # so the stage is invisible to the patterns above. That is precisely what
        # happens in Act 2 of PR #1479: auth0_test.go imports mock_auth0, which
        # only exists in the fix patch, so the test stage produced a result
        # BIT-IDENTICAL to the baseline (768/4/0) and report.py classified the 11
        # tests as "new" n2p instead of 11 FAIL->PASS.
        #
        # Recording a synthetic, namespaced entry restores the distinction
        # (run.failed 4 -> test.failed 5 -> fix.failed 4, which also satisfies the
        # `run.failed < test.failed` health rule). It is provably inert for
        # classification: the name is only ever added to failed_tests, so its
        # `fix` status can only be FAIL or NONE, never PASS -- and report.py's
        # classifier skips every test whose fix status is not PASS. It therefore
        # cannot manufacture a false f2p/p2p/n2p; it only makes failed_count
        # reflect what actually happened.
        #
        # Reclassifying the compile failure as f2p is still NOT possible from
        # here, though not for the reason of "a TestResult cannot name tests that
        # never ran" -- it can, self.pr.test_patch is in scope. The real obstacle
        # is that the 11 names are not derivable: they are runtime-computed
        # subtests (`TestParseServiceAccount/base64_err`), dispatched through
        # `t.Run(tc.name, ...)` over table entries. Only 6 of the 11 carry a
        # literal `name:` field a static reader could recover, and reconstructing
        # them would mean reimplementing Go's space->underscore normalisation and
        # parent-prefix join. Emitting a package-level status that report.py can
        # classify is the correct fix, and that lives in the harness.
        #
        # Deliberately narrow: the timing form (`FAIL <pkg> 0.011s`) is left
        # alone. Those packages run and report their own `--- FAIL:` lines, or
        # fail for environmental reasons identical across all three stages.
        re_pkg_build_fail = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]$")

        buckets = {
            "PASS": passed_tests,
            "FAIL": failed_tests,
            "SKIP": skipped_tests,
        }
        quarantined = set(_QUARANTINED_TESTS)
        pending: list[tuple[str, str]] = []

        def flush(pkg: str) -> None:
            for status, name in pending:
                buckets[status].add(f"{pkg}{_PKG_SEP}{name}")
            pending.clear()

        for line in test_log.splitlines():
            line = line.strip()

            flaky_match = re_flaky.match(line)
            if flaky_match:
                quarantined.add(flaky_match.group(1))
                continue

            result_match = re_result.match(line)
            if result_match:
                pending.append((result_match.group(1), result_match.group(2)))
                continue

            pkg_end_match = re_pkg_end.match(line)
            if pkg_end_match:
                pkg = pkg_end_match.group(1)
                flush(pkg)
                if re_pkg_build_fail.match(line):
                    failed_tests.add(f"{_BUILD_FAILED_PREFIX}{pkg}")

        flush(_UNKNOWN_PKG)

        # Subtests are reported separately and a retried name can appear under
        # more than one status within a single package. Reconcile with failure
        # winning, then skip, so the three sets stay disjoint -- otherwise
        # TestResult.__post_init__ raises and the instance run dies.
        passed_tests -= failed_tests | skipped_tests
        skipped_tests -= failed_tests

        # Drop nondeterministic tests entirely rather than letting them land in
        # one bucket here and another in the next stage. A test that is absent
        # from all three stages produces no transition at all, so it cannot be
        # classified as f2p/p2p/n2p and cannot invalidate the instance; the cost
        # is exactly its own p2p credit.
        passed_tests -= quarantined
        failed_tests -= quarantined
        skipped_tests -= quarantined

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
