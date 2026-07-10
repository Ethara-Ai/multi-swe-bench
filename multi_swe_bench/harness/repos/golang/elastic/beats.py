import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for elastic/beats.
#
# Each instance is a release-delta BUNDLE. The raw record carries
# `prs_in_bundle` (e.g. [25187, 25795, ...]) but an EMPTY `number_interval`
# (all 151 records ship `null`). The required output format is the dash-JOINED
# bundle list ("25187-25795-...") -- NOT a "22000-30000" range, which would
# wrongly imply every PR in between.
#
# Two constraints force the approach below (identical to aquasecurity/tfsec):
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it -- the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "elastic/25187-25795-..."), which is not
#     registered -> instance creation fails. Routing must stay "elastic/beats".
#
# So we do two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to
# harness source):
#   1. PullRequest.from_json -- re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_beats_number_interval` (routing key stays "").
#   2. Dataset.build -- stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_beats_number_interval_patched", False):
    _beats_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _beats_from_json(cls, json_str):
        pr = _beats_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "elastic"
                and raw.get("repo") == "beats"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._beats_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_beats_from_json)
    _pull_request.PullRequest._beats_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_beats_build_patched", False):
        _beats_orig_build = _Dataset.build.__func__

        def _beats_build(cls, pr, report):
            ds = _beats_orig_build(cls, pr, report)
            ni = getattr(pr, "_beats_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_beats_build)
        _Dataset._beats_build_patched = True
# ---------------------------------------------------------------------------


# beats' go.mod `go` directive rose across the release lines this dataset spans
# (PR numbers 22756-49990). golang:1.25 + GOTOOLCHAIN=auto would build all of
# them, but we keep the historical era split so early PRs pin a
# period-appropriate toolchain image. Returns (base golang image, shared base
# tag). The tag is era-derived (NOT per-PR) so the base is built ONCE per era
# and reused as the FROM parent of every PR image in that era.
def _beats_era(number: int) -> tuple[str, str]:
    if number < 30000:
        return "golang:1.17-bullseye", "beats-22000-30000-base"
    if number < 48000:
        return "golang:1.24-bookworm", "beats-30000-48000-base"
    return "golang:1.25-bookworm", "beats-48000-99999-base"


class BeatsImageBase(Image):
    """SINGLE SHARED base (one per era, tag e.g. `beats-22000-30000-base`), built
    ONCE and reused as the FROM parent of every per-PR image in that era. It
    clones the full repo history + keeps `origin`, so each per-PR image can
    `git checkout` its own base commit, and it warms the Go module cache so the
    common dependency download is not repeated for every PR. It carries NO
    per-PR base commit.

    The leading `# syntax=docker/dockerfile:1.6` directive makes
    DockerfileEnhancer.enhance() return this Dockerfile VERBATIM (it
    early-returns when the directive is already present). That is deliberate: it
    stops the enhancer from rewriting the clone into a `git checkout
    ${BASE_COMMIT}` + history-strip block, which would pin this shared base to
    whichever PR built it first and prune away every other PR's commit
    ("reference is not a tree"). The git-history hardening is instead applied
    PER-PR in BeatsImageDefault, AFTER prepare.sh checks out that PR's base
    commit -- keeping this ONE shared base reusable by every PR in the era.

    We hand-write only the ARGs/ENV/LABEL we want and DELIBERATELY OMIT the
    enhancer's proxy and certificate injection: no proxy build-args, no proxy/SSL
    ENVs, no CA-cert symlink block, and no MITM secret mount. The `ca-certificates`
    apt package below is the standard CA bundle required for HTTPS `git clone`
    and `go mod download`, not injected proxy/cert config."""

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
        return _beats_era(self.pr.number)[0]

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return _beats_era(self.pr.number)[1]

    def workdir(self) -> str:
        return _beats_era(self.pr.number)[1]

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        # `-buildvcs` was introduced in Go 1.18; on the era-1 image (golang:1.17)
        # `go test` aborts with "unknown flag -buildvcs" for every invocation,
        # zeroing all test results. Go 1.17 also has no VCS-stamping step, so it
        # never needed the guard. Emit GOFLAGS only for the Go 1.18+ eras
        # (PR # >= 30000); omit it entirely on era-1.
        goflags_line = (
            '\n    GOFLAGS="-buildvcs=false"' if self.pr.number >= 30000 else ""
        )

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    GOTOOLCHAIN=auto{goflags_line}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make sudo wget unzip \\
    python3 python3-dev python3-venv python3-pip \\
    libpcap-dev librpm-dev libaio-dev libssl-dev libffi-dev \\
    iproute2 netcat-openbsd \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
# Warm the SHARED module cache from the repo's current (latest) go.mod so the
# common deps live in this single base layer instead of being re-downloaded by
# every PR. Per-SHA go.sum differences are filled in by each PR's prepare.sh.
# `|| true`: the master go.mod may reference modules a given network can't
# reach; that must not fail the shared base build.
RUN go mod download || true

CMD ["/bin/bash"]
"""


class BeatsImageDefault(Image):
    """Per-PR image: FROM the shared era base, check out THIS PR's base commit
    (in prepare.sh), warm its per-SHA deps, then strip git history so the
    evaluated agent cannot recover the fix from git log/show. Only the
    PR-specific delta is applied on top of the reused shared base."""

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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves dockerfile() verbatim, so
        # the hardening below is applied by hand (anchored on HEAD), not by the
        # enhancer. This is what lets the base stay shared across the era.
        return BeatsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(
                ".",
                "filter_binary_diffs.sh",
                """#!/bin/bash
# Reads a patch from $1 (or stdin if "-"), drops every "diff --git" block that
# contains an abbreviated binary marker ("Binary files X and Y differ" or
# "GIT binary patch"). Emits the remainder on stdout. ~19% of beats PRs in
# this dataset include binary diffs (.png, .zip, .log) without delta data,
# which would otherwise make `git apply` fail outright.
exec awk '
function emit() {
  if (block != "" && !skip_block) printf "%s", block
  block = ""
  skip_block = 0
}
/^diff --git / { emit() }
/^Binary files .* and .* differ$/ { skip_block = 1 }
/^GIT binary patch$/ { skip_block = 1 }
{ block = block $0 "\\n" }
END { emit() }
' "${1:-/dev/stdin}"
""",
            ),
            File(
                ".",
                "compute_scope.sh",
                """#!/bin/bash
# Computes SCOPE = list of changed Go package directories from /home/test.patch
# and /home/fix.patch. Sourced by run.sh / test-run.sh / fix-run.sh so the
# monorepo doesn't try to compile every sub-beat on every PR.
PATCH_LIST=""
[ -f /home/test.patch ] && PATCH_LIST="$PATCH_LIST /home/test.patch"
[ -f /home/fix.patch ] && PATCH_LIST="$PATCH_LIST /home/fix.patch"

SCOPE=""
if [ -n "$PATCH_LIST" ]; then
  SCOPE=$(cat $PATCH_LIST 2>/dev/null \\
    | grep -E "^diff --git" \\
    | awk '{print $3}' \\
    | sed 's|^a/||' \\
    | grep '\\.go$' \\
    | xargs -I{} dirname {} 2>/dev/null \\
    | sort -u \\
    | sed 's|^|./|' \\
    | tr '\\n' ' ')
fi
export SCOPE
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Pre-fetch Go modules so test runs don't redo network work each invocation.
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test in baseline."
  exit 0
fi
echo "Baseline test scope: $SCOPE"
# Run each package independently so one unbuildable package (e.g. Linux-only
# build tags on ARM64) doesn't abort the full sweep before producing markers.
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/filter_binary_diffs.sh /home/test.patch > /tmp/test.patch.filtered
git apply --whitespace=nowarn /tmp/test.patch.filtered
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test."
  exit 0
fi
echo "test-patch test scope: $SCOPE"
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/filter_binary_diffs.sh /home/test.patch > /tmp/test.patch.filtered
bash /home/filter_binary_diffs.sh /home/fix.patch  > /tmp/fix.patch.filtered
git apply --whitespace=nowarn /tmp/test.patch.filtered /tmp/fix.patch.filtered
source /home/compute_scope.sh
if [ -z "$SCOPE" ]; then
  echo "No Go packages touched by patches; nothing to test."
  exit 0
fi
echo "fix-patch test scope: $SCOPE"
for pkg in $SCOPE; do
  go test -v -count=1 -timeout 15m "$pkg" || true
done

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Git-history hardening for the per-PR (agent) image, applied AFTER
        # prepare.sh has checked out this PR's base commit -- so the commit to
        # KEEP is the current HEAD (not a ${BASE_COMMIT} build-arg, which is not
        # passed to FROM-an-image builds). The shared base deliberately keeps
        # full history + origin (see BeatsImageBase); this strips the remote and
        # every ref/commit not reachable from HEAD, so the evaluated agent
        # cannot recover the fix from git log/show/history. Mirrors the harness
        # Image._HARDENING_BLOCK, anchored on HEAD.
        harden = f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
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
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{harden}

{self.clear_env}
"""


@Instance.register("elastic", "beats")
class BEATS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BeatsImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        for line in clean_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if "/" in name:
                    continue
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
