import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/aquasecurity/tfsec/"

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for aquasecurity/tfsec.
#
# Each instance is a release-delta BUNDLE. The raw record carries
# `prs_in_bundle` (e.g. [1005, 1006, 1009]) but an EMPTY `number_interval`.
# The required output format is the dash-JOINED bundle list ("1005-1006-1009") —
# NOT a "146-157" range, which would wrongly imply every PR in between.
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "aquasecurity/1005-1006-1009"), which is not
#     registered → instance creation fails.
#
# So, following the ytdl-org/youtube-dl convention, we do two import-time
# monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_tfsec_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_tfsec_number_interval_patched", False):
    _tfsec_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _tfsec_from_json(cls, json_str):
        pr = _tfsec_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "aquasecurity"
                and raw.get("repo") == "tfsec"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._tfsec_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_tfsec_from_json)
    _pull_request.PullRequest._tfsec_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_tfsec_build_patched", False):
        _tfsec_orig_build = _Dataset.build.__func__

        def _tfsec_build(cls, pr, report):
            ds = _tfsec_orig_build(cls, pr, report)
            ni = getattr(pr, "_tfsec_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_tfsec_build)
        _Dataset._tfsec_build_patched = True
# ---------------------------------------------------------------------------


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Test names are kept package-qualified
    (`pkg/path::TestName`); subtests appear as `TestName/sub`."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        test = ev.get("Test")
        action = ev.get("Action")
        pkg = ev.get("Package", "") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if pkg.startswith(_REPO_PREFIX):
            pkg = pkg[len(_REPO_PREFIX):]
        name = f"{pkg}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # Enforce TestResult disjoint-set invariant.
    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class TfsecImageBase(Image):
    """SINGLE SHARED base (tag `base`), built ONCE and reused as the FROM parent
    of every per-PR image. It clones the full repo history + keeps `origin`, so
    each per-PR image can `git checkout` its own base commit, and it warms the
    Go module cache (`/go/pkg/mod`) so the common dependency download is not
    repeated for all 56 PRs. It carries NO per-PR base commit.

    tfsec spans go.mod `go 1.16`-`1.18` across the dataset (master is `go 1.23`).
    golang:1.23 + GOTOOLCHAIN=auto builds every one of them: 1.16-1.18 compile
    directly under the 1.23 toolchain (Go is backward compatible; language
    semantics are gated by the go.mod `go` directive), and master's `go 1.23`
    go.mod is downloadable so the cache-warming `go mod download` succeeds.

    The leading `# syntax=docker/dockerfile:1.6` directive makes
    DockerfileEnhancer.enhance() return this Dockerfile VERBATIM (it
    early-returns when the directive is already present). That is deliberate: it
    stops the enhancer from rewriting the clone into a `git checkout
    ${BASE_COMMIT}` + history-strip block, which would pin this shared base to
    whichever PR built it first and prune away every other PR's commit
    ("reference is not a tree"). The git-history hardening is instead applied
    PER-PR in TfsecImageDefault, AFTER prepare.sh checks out that PR's base
    commit -- keeping this ONE shared base reusable by every PR."""

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
        return "golang:1.23"

    def image_prefix(self) -> str:
        return "mswebench"

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

        org = self.pr.org
        repo = self.pr.repo

        # This Dockerfile is emitted verbatim (the leading `# syntax` directive
        # makes DockerfileEnhancer.enhance() early-return), so the enhancer's
        # infrastructure block is NOT applied here. The canonical MITM
        # proxy/cert scaffolding is therefore RE-ADDED BY HAND below, in the
        # enhancer's own order (ARGs -> ENV -> LABEL -> cert symlinks), and is
        # sourced DIRECTLY from the DockerfileEnhancer constants rather than
        # retyped -- so the emitted text matches `image.py` verbatim and cannot
        # drift out of sync with it (PIPELINE.md 2a/8).
        #
        # Nothing rogue is added: no hardcoded proxy IP, no GIT_SSL_NO_VERIFY,
        # no sslVerify=false, no --insecure, no `update-ca-certificates` beyond
        # the injected symlinks. `_MITM_MOUNT` stays LATENT here exactly as it
        # is in image.py (never injected); wiring it would also require
        # `docker build --secret id=mitm_ca,src=<ca.crt>`.
        #
        # The proxy ARGs keep their EMPTY defaults (= passthrough) unless a
        # build passes `--build-arg http_proxy=...`; build_dataset.py only ever
        # passes REPO_URL/BASE_COMMIT, so under the normal pipeline this is
        # scaffolding + CA trust, not an active interception.
        #
        # The Go-specific ENV (CGO_ENABLED/GOTOOLCHAIN/GOFLAGS) is kept in its
        # own ENV so the canonical block stays byte-identical to image.py; the
        # canonical block already supplies DEBIAN_FRONTEND/LANG/TZ with exactly
        # the values this base used before. The plain `ca-certificates` apt
        # package below is unrelated and pre-existing -- it is the standard CA
        # bundle required for HTTPS `git clone` and `go mod download`.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

ENV CGO_ENABLED=0 \\
    GOTOOLCHAIN=auto \\
    GOFLAGS="-buildvcs=false -mod=mod"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

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


class TfsecImageDefault(Image):
    """Per-PR image: FROM the shared base, check out THIS PR's base commit,
    install its (per-SHA) deps, then strip git history so the evaluated agent
    cannot recover the fix from git log/show. Only the PR-specific delta is
    applied on top of the reused shared base."""

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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves dockerfile() verbatim, so
        # the hardening below is applied by hand (anchored on HEAD), not by the
        # enhancer. This is what lets the base stay shared.
        return TfsecImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
rm -rf vendor
go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Go package dirs the PR's test patch touches. Per-dir `( cd && go test )`
# is robust to any nested sub-modules (defensive — tfsec has one go.mod).
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_BASELINE_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.test')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
EXCLUDES=(--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \
    --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.tar' \
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.test')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | sed -E 's#/[^/]+$##' | sort -u; }} || true)
RAN=0
for d in $TEST_DIRS; do
    if [ -d "$d" ]; then ( cd "$d" && go test -json -count=1 . ) 2>&1 || true; RAN=1; fi
done
if [ "$RAN" = 0 ]; then echo "NO_TEST_DIRS"; exit 0; fi
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
        # full history + origin (see TfsecImageBase); this strips the remote and
        # every ref/commit not reachable from HEAD, so the evaluated agent
        # cannot recover the fix from git log/show/history. Mirrors the harness
        # _HARDENING_BLOCK, anchored on HEAD. The .gitmodules branch is a no-op
        # for tfsec (single module, no git submodules) but is kept for parity.
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

        # Canonical MITM proxy/cert scaffolding, re-added by hand for the same
        # reason as in TfsecImageBase: this image's dependency() returns an
        # Image, so DockerfileEnhancer.enhance() early-returns and injects
        # nothing. Emitted from the enhancer constants (verbatim, no drift) and
        # placed right after FROM so prepare.sh's `go mod download` runs with
        # the proxy/CA env in scope. ENV would otherwise be inherited from the
        # base, but the enhancer re-emits this block on every image it touches,
        # so mirroring it here keeps per-PR Dockerfiles auditable on their own
        # (PIPELINE.md 8 greps `workdir/.../images/*/Dockerfile`).
        #
        # NOTE: no build args are passed for an Image-typed dependency
        # (build_dataset.py:623), so these ARGs always take their empty
        # defaults here -- i.e. passthrough, and a non-empty proxy baked into
        # the base would be reset to empty at this layer.
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


@Instance.register("aquasecurity", "tfsec")
class TFSEC(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TfsecImageDefault(self.pr, self._config)

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
        return parse_go_test_log(log)


# ---------------------------------------------------------------------------
# === bundle number_interval routing (prs_in_bundle dash-joined) ===
#
# PIPELINE.md 11b: the JSONL and the registry ship together, and the trajectory
# team's harness routes via Instance.create() -> "{org}/{number_interval}".
# Every dash-joined bundle value must therefore be a registered key or the run
# dies with `ValueError: Instance 'aquasecurity/<ni>' is not registered.`
#
# BUNDLE-level, not PR-level: exactly ONE key per instance (19 keys for the 19
# bundles in aquasecurity__tfsec_usable19.jsonl), each the FULL dash-joined
# list -- never a "1466-1474"-style range, which would wrongly imply every PR
# in between. Values verified byte-identical (set AND order) against
# `prs_in_bundle` in the source aquasecurity__tfsec_lht_final.jsonl.
#
# tfsec is SINGLE-ERA (one shared golang:1.23 base, one class), so all bundle
# keys point at TFSEC. The original "aquasecurity/tfsec" registration above is
# kept as-is. Nothing else is touched: no image.py, no base format, no
# dockerfiles, no run scripts.
#
# Data-derived -> REGENERATE this list whenever the bundles change.
# ---------------------------------------------------------------------------
_BUNDLE_NIS_TFSEC = [
    "1466-1474",
    "1441-1442-1443-1444",
    "1438-1468-1500-1503-1504-1505-1506-1507-1508",
    "1368-1408-1426-1427-1428-1430",
    "1106-1108-1114-1115-1116-1131-1133",
    "1080-1085-1091-1093",
    "1034-1039-1041-1045-1058-1059-1063-1064-1065-1068-1070-1074-1075-1077-1078",
    "1026-1027-1028-1030-1031-1032-1033-1036-1042-1046-1053",
    "992-993-995-996",
    "982-983-984-985",
    "922-924-925-926-927-928-929-930-931-932-933-934-935-936-937",
    "866-1094-1095-1100-1101-1103-1105-1109",
    "770-1012-1013",
    "1081-1083-1089",
    "997-998-999-1001-1002-1004",
    "978-979-980-981",
    "912-913-914-916-918",
    "967-968-969-970",
    "947-948-949-950-951-953-954-955-956",
]

for _ni in _BUNDLE_NIS_TFSEC:
    # Instance.register() overwrites silently (harness/instance.py), so a key
    # already bound to a DIFFERENT class would mis-route without a word. The
    # org is shared with trivy, whose own bundle keys live under
    # "aquasecurity/..." too -- fail loud rather than clobber one.
    _existing = Instance._registry.get(f"aquasecurity/{_ni}")
    if _existing is not None and _existing is not TFSEC:
        raise RuntimeError(
            f"tfsec bundle key 'aquasecurity/{_ni}' already registered to "
            f"{_existing.__name__}; refusing to overwrite."
        )
    Instance.register("aquasecurity", _ni)(TFSEC)
