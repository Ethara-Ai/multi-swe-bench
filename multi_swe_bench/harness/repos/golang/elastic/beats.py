import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
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
#     (instance.py: name becomes "elastic/25187-25795-..."). For a RAW
#     `_lht_final.jsonl` build those bundle keys are NOT registered, so routing
#     must stay "elastic/beats" -- hence stash-only, never assign.
#
# TWO INPUT SHAPES, both already correct -- do not "fix" one and break the other:
#   (a) RAW build input  -- carries `prs_in_bundle`, `number_interval` empty.
#       Routing falls back to "elastic/beats"; the Dataset.build patch below
#       stamps the dash-joined value onto the OUTPUT row only.
#   (b) DELIVERY input ('Delevery jsonal/elastic__beats_final.jsonl') -- carries
#       a populated `number_interval` and NO `prs_in_bundle`. The stash stays
#       unset, so the patch below is a no-op, and `Dataset.build` carries the
#       value through natively (dataset.py: `number_interval=pr.number_interval`).
#       Routing uses the bundle key, which IS registered -- see the
#       _BUNDLE_NIS_BEATS block at the end of this file (PIPELINE.md 11b).
# Either way the emitted value is the explicit dash-joined PR list
# ("146-147-150-155-157"), never a range ("146-157").
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
            ' \\\n    GOFLAGS="-buildvcs=false"' if self.pr.number >= 30000 else ""
        )

        # MITM proxy/cert scaffolding. This base keeps the `# syntax` opt-out
        # (see class docstring: auto-injection would rewrite the shared clone
        # into a per-PR `git checkout ${BASE_COMMIT}` and prune the era base),
        # so DockerfileEnhancer never runs on it. PIPELINE.md 2a covers exactly
        # this case -- "add MITM by hand" for opt-out images. We reference the
        # canonical constants off DockerfileEnhancer instead of copy-pasting
        # them, so the emitted block is verbatim-identical to image.py and
        # cannot drift if image.py changes. image.py itself is NOT modified.
# Debian 11 (bullseye) reached EOL: deb.debian.org still serves the
        # bullseye-security INDEX but the pool has been pruned, so individual
        # .deb fetches 404 and era-1's apt install dies with exit 100. Repoint
        # bullseye at archive.debian.org and drop the -security/-updates suites
        # (archive.debian.org carries no Release file for bullseye-security).
        # Mirrors Image._get_apt_update_command's buster/stretch/jessie rewrite;
        # bullseye is not in that helper's DEPRECATED_DEBIAN_IMAGES list, and
        # this base is `# syntax`-opt-out so the helper never runs on it anyway.
        if "bullseye" in image_name:
            apt_sources_fix = (
                "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g'"
                " /etc/apt/sources.list && \\\n"
                "    sed -i '/security/d;/bullseye-updates/d' /etc/apt/sources.list\n\n"
            )
            apt_novalid = " -o Acquire::Check-Valid-Until=false"
        else:
            apt_sources_fix = ""
            apt_novalid = ""

        proxy_args = DockerfileEnhancer._PROXY_ARGS
        env_block = DockerfileEnhancer._ENV_BLOCK
        cert_symlinks = DockerfileEnhancer._CERT_SYMLINKS

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{proxy_args}

{env_block} \\
    GOTOOLCHAIN=auto{goflags_line}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{cert_symlinks}

WORKDIR /home/

{apt_sources_fix}RUN apt-get{apt_novalid} update && apt-get install -y --no-install-recommends \\
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

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

{DockerfileEnhancer._CERT_SYMLINKS}

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md 11b: the JSONL and this registry ship together to the trajectory
# team, whose harness calls Instance.create() -> "{org}/{number_interval}".
# Every dash-joined bundle value in the delivery JSONL must therefore be a
# registered routing key, else instance.py raises
# `ValueError: Instance 'elastic/<ni>' is not registered.`
#
# Bundle-level, NOT pr-level: one key per instance (30 keys == 30 records).
# elastic/beats is a single-class repo -- the 22756..49990 era split lives in
# _beats_era(pr.number) on the IMAGE, not in the registry -- so every bundle key
# maps to the one BEATS class. The original "elastic/beats" key stays registered
# (decorator above) and remains the fallback when number_interval is empty.
#
# Data-derived from 'Delevery jsonal/elastic__beats_final.jsonl' (the 30-record
# QC-usable delivery set) -- REGENERATE when the delivery set changes.
_BUNDLE_NIS_BEATS = [
    "49990-49992-49993-50398-50415-50460-50463-50475-50484-50491-50506-50514-50519-50522-50526-50533-50535-50536-50548-50551-50558-50561",
    "49433-49436-49437-49472-49487-49498-49508-49515-49526-49531-49535-49551-49555-49569-49575-49623-49630-49664-49673-49681-49685-49688-49695-49700-49703-49712-49719-49726-49731-49738-49752-49765-49784-49787-49792-49808-49817-49820-49823-49830-49849-49853",
    "49356-49435-49438-49439-49499-49509-49516-49521-49527-49532-49536-49552-49556-49562-49570-49585-49624-49650-49651-49652-49661-49663-49666-49668-49674-49677-49682-49686-49689-49696-49701-49704-49708-49713-49720-49727-49732-49739-49753-49766-49775-49785-49788-49793-49809-49816-49821-49824-49831-49850-49851-49854-49856",
    "49355-49380-49425-49430-49447-49457-49500-49513-49522-49525-49534-49550-49554-49568-49576-49584-49593-49598-49622-49631-49659-49665-49672-49680-49684-49687-49694-49699-49707-49711-49718-49725-49730-49751-49786-49791-49818-49822-49834-49848-49852-49857-49872-49880",
    "47817-48045-48052-48053-48058-48091-48101-48105-48119-48124-48127-48130-48133-48136-48142-48162-48168-48169-48175-48179-48189-48199-48221-48231-48234-48235-48256-48264-48269-48289-48300-48313-48343",
    "47559-47716-47751-47786-47805-47812-47824-47829-47834-47849-47852-47853-47856-47884-47886-47888-47894-47901-47907-47911-47917-47924-47928-47932-47937-47939-47944-47952-47958-47961-47964-47972-47976-47981-47984-47987-47994-48002-48003-48016-48024-48035-48037-48041-48051-48056-48086-48088-48099",
    "47524-47811-47818-47819-47826-47841-47843-47845-47855-47859-47862-47863-47867-47885-47887-47889-47891-47896-47900-47902-47909-47919-47929-47934-47938-47941-47946-47953-47960-47962-47983-47986-47989-47992-47995-48001-48005-48018-48022-48027-48034-48038-48043-48060-48069-48078-48082-48085-48098-48100",
    "46983-47749-47787-47800-47810-47825-47830-47835-47844-47854-47857-47860-47861-47895-47908-47910-47918-47927-47933-47940-47945-47947-47951-47959-47974-47982-47988-47991-48000-48004-48017-48023-48036-48042-48049-48083-48087-48106-48116",
    "46508-46522-46550-46560-46563-46565-46566-46582-46586-46588-46606-46630-46660-46675-46686-46692-46709-46714-46715-46740-46758-46787-46812-46817-46840-46847-46853-46880",
    "46467-46507-46510-46524-46541-46545-46549-46559-46579-46585-46590-46592-46598-46599-46609-46612-46628-46632-46640-46649-46658-46673-46688-46691-46693-46711-46713-46717-46743-46755-46757-46771-46790-46811-46815-46832-46838-46849-46852-46878",
    "46065-48468-48482-48745-48819-48838-48858-48882-48939-48941-48942-48968-49011-49024-49036-49055-49063-49066-49079-49102-49105-49108-49120-49130-49140-49146-49182-49202-49212-49218-49226-49230-49233-49243-49246-49250-49253-49260-49268-49271-49274-49277-49281-49286-49294-49308-49322-49328-49331-49359-49369-49372-49382-49384-49397-49398-49408-49410-49422-49427-49442-49454-49466-49471-49478",
    "44295-44427-44429-44430-44455-44457-44463-44466-44486-44494-44500-44502-44516-44530-44533-44539-44560-44568-44580-44590-44610-44615-44633-44640-44645-44660-44671-44679-44694-44709-44724-44729-44734-44737-44745-44754-44764-44766-44777-44792-44806-44809-44818-44827-44833-44840-44848-44870-44875-44880-44899",
    "44142-44150-44158-44160-44161-44163-44177-44209-44226-44241-44246-44248-44260-44276-44290-44299-44314-44318-44348-44354-44357-44374-44375-44382-44389-44393",
    "43614-44140-44143-44166-44167-44169-44170-44179-44195-44210-44224-44233-44242-44249-44254-44261-44266-44277-44298-44300-44315-44319-44322-44344-44355-44358-44359-44376-44383-44390-44394-44399-44411-44415-44437",
    "42921-43615-43692-43718-44135-44136-44139-44145-44149-44152-44156-44162-44165-44172-44178-44191-44197-44206-44207-44211-44213-44220-44225-44236-44239-44251-44256-44258-44263-44280-44293-44302-44317-44321-44328-44338-44345-44347-44350-44353-44368-44369-44372-44378-44385-44392-44396-44401-44406-44408-44413-44417-44440-44456-44460-44464-44496-44497-44504-44506-44509-44513-44514-44522",
    "42611-42612-42613-42634-42654-42686-42696-42701-42717-42726-42732-42740-42759-42767-42772-42784-42812-42858-42907-42919-42928-42948",
    "42607-42609-42610-42632-42653-42687-42716-42733-42768-42773-42786-42859-42909",
    "41133-41193-41214-41219-41220-41221-41238-41241-41261-41283-41290-41301-41312-41318-41321-41343-41362-41366-41370-41375-41379-41385-41400-41414-41421-41429-41440-41456-41467-41482-41483-41497-41502-41510-41539-41545",
    "40596-40646-40647-40648-40687-40695-40697-40710-40717-40739-40760-40767-40770-40776-40782-40783-40793-40823-40825-40828-40829-40830-40831-40832-40833-40834-40835-40845-40846-40853-40862-40864-40866-40868-40871-40882-40901",
    "40099-40100-40302-40432-40439-40444-40455-40460-40464-40473-40481-40489-40493-40501-40505-40511-40516-40517-40522-40531-40534-40535-40538-40567-40575-40576-40587-40590-40591-40592-40598-40603-40605-40615-40617-40620-40621-40629-40631-40632-40640-40645-40650-40655-40657-40668-40673",
    "38557-38698-38713-38715-38733-38742-38745-38746-38748-38761-38765-38771-38772-38773-38774-38775-38794-38802-38804-38810-38811-38818-38822-38824-38832-38847-38850-38856-38859-38864-38872-38879-38886-38897-38899-38910-38928-38930-38932-38935-38937-38949-38965-38968-38973-38975-38977-38988-38995-39003-39008-39016-39037-39040-39049-39055-39059-39069-39079-39083-39084-39102-39109-39123-39129-39152-39162-39182-39185-39190-39193-39227-39265-39272",
    "37073-37359-37372-37385-37386-37388-37390-37391-37393-37399-37404-37415-37422-37433-37442-37462-37470-37483-37489-37500-37506-37511-37517-37521-37522-37530-37533-37547-37563-37568",
    "35223-35224-35524-35531-35544-35546-35554-35556-35564-35566-35568-35573-35577-35587-35590-35596-35597-35613-35622-35629-35641-35642-35654-35657-35663-35673-35675-35681-35685",
    "34991-35190-35196-35230-35239-35245-35282-35283-35284-35290-35327-35328-35342-35345-35355-35364-35375-35380-35399-35412-35441-35452-35486-35497-35510-35516-35526-35547-35563-35576-35588-35609-35643-35659-35683-35687-35694-35700-35704-35711-35743-35746-35768-35781-35786-35797-35819-35830-35838-35840-35857-35875-35876-35885",
    "34522-34523-34681-34860-34927-34944-34947-34949-34954-34962-34968-34971-34978-34979-34986-34992-35003-35010-35017-35028-35032-35033-35037-35038-35055-35057-35066-35069-35076-35085-35110-35121-35130-35136-35144-35148-35161-35162-35166-35172",
    "33948-34109-34445-34451-34453-34462-34463-34464-34466-34469-34471-34476-34501-34502-34507-34511-34593-34604-34614-34626-34630-34636-34658-34666-34743-34745-34751-34753-34755-34759-34764-34775-34790-34801-34804-34816-34825-34853-34858-34868-34869-34872-34878-34879-34924-34948-34953-34960-34967-34981-34995-35001-35016-35046-35053-35059-35067-35079-35086-35087-35104-35116-35128-35150-35167-35168",
    "32297-33023-33380-33387-33388-33390-33391-33397-33401-33412-33428-33433-33436-33448-33450-33460-33476-33517-33549-33580-33590-33622-33626-33635-33637-33652-33663-33668-33670-33675-33678-33685-33726-33808-33843-33886-33897-33900-33915",
    "32238-32399-32693-32741-32743-32757-32758-32761-32776-32778-32783-32793-32794-32796-32798-32802-32807-32812-32834-32837-32840-32845-32871-32879-32882-32884-32907-32909-32913-32920-32935-32941-32947-32956-32958-32965-32974-32984-32990-32992-33004-33017-33022-33028-33034-33059-33072-33073-33074-33079-33083-33096-33102-33116-33121-33124-33128-33142-33161-33165-33171-33174-33178-33182-33193-33251-33266-33269-33271-33274-33281-33290-33304-33325-33330-33331-33341-33343-33350-33355-33356-33360-33366-33370",
    "29477-29478-29837-29976-30016-30077-30093-30108-30111-30117-30126-30147-30172-30176-30184-30188-30193-30214-30244-30248-30272-30284-30292-30305-30307-30312-30325-30344-30352-30358-30361-30364-30379-30405-30416-30421-30519-30523-30526-30535-30538-30555",
    "28658-29977-30112-30118-30121-30127-30128-30133-30135-30138-30145-30175-30189-30194-30211-30215-30239-30245-30249-30268-30273-30285-30287-30293-30296-30308-30313-30316-30326-30336-30341-30346-30349-30353-30359-30362-30365-30380-30383-30403-30412-30417-30422-30430-30456-30467-30485-30487-30501-30506-30508-30524-30527-30536-30539-30556",
]

for _ni in _BUNDLE_NIS_BEATS:
    Instance.register("elastic", _ni)(BEATS)
