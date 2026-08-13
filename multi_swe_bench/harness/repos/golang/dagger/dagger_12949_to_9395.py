import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/dagger/dagger/"


def parse_go_test_log(log: str) -> TestResult:
    """Parse `go test -json` output. Each test emits a JSON event with
    `Action` (run/pass/fail/skip), `Package`, and `Test`. Names are kept
    package-qualified (`pkg/path::TestName`) since Go test function names
    recur across packages; subtests appear as `TestName/sub`."""
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
            pkg = pkg[len(_REPO_PREFIX) :]
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


class DaggerEra3ImageBase(Image):
    """dagger era 3 (PRs 9395-12949, v0.13->0.20): go.mod `go 1.24`-`1.25`.
    Pure-Go CI/CD container-build engine. Built with Go 1.25 (>= every go.mod
    in this era). Tests are run with `go test` on the patched unit packages;
    `core/integration/` + `e2e/` tests (which need a live dagger engine +
    container runtime) are deliberately excluded."""

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
        return "golang:1.25"

    def image_tag(self) -> str:
        return "base-go125"

    def workdir(self) -> str:
        return "base-go125"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # SHARED era base: built ONCE (image_tag "base-go125") and reused by every
        # era-3 PR. It clones the repo at default HEAD with FULL history and
        # deliberately does NOT check out a per-PR ${BASE_COMMIT} and does NOT run
        # the anti-cheat hardening block. Pinning + history-stripping a SHARED base
        # would prune every sibling PR's base.sha, and their `git checkout <sha>`
        # in prepare.sh would then fail (prepare.sh runs under `set -e`, so the
        # build of every PR but the one that seeded the base would break). The
        # per-PR checkout and the hardening happen in DaggerEra3ImageDefault, which
        # is the image the model is actually evaluated in.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this Dockerfile verbatim (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`); that is precisely what stops
        # _standardize_repo_fetch() from rewriting the clone into a
        # `git checkout ${BASE_COMMIT}` + hardening template. Because the enhancer
        # is opted out, the ARG/ENV/LABEL infra it would have injected is inlined
        # here. `origin` is dropped at this layer too, so nothing downstream can
        # `git fetch` the upstream fix even before hardening runs.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC
ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOFLAGS="-buildvcs=false -mod=mod"

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

{code}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class DaggerEra3ImageDefault(Image):
    """Per-PR image: checkout base commit, prefetch modules, run the targeted
    Go unit tests."""

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
        return DaggerEra3ImageBase(self.pr, self._config)

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
go mod download || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
# Go package dirs the PR's test patch touches, excluding integration/e2e
# suites (those need a running dagger engine + container runtime).
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
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
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.webp' \
    --exclude='*.webm' --exclude='*.age' --exclude='*.eot' --exclude='*.ttf' \
    --exclude='*.lock' --exclude='*.bin' --exclude='*.class' \
    --exclude='telemetry/telemetry' --exclude='toolchains/python-sdk-dev/dockerd/main')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
if grep -qE '^diff --git a/go\\.(mod|sum)' /home/test.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
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
    --exclude='*.gz' --exclude='*.zip' --exclude='*.woff*' --exclude='*.webp' \
    --exclude='*.webm' --exclude='*.age' --exclude='*.eot' --exclude='*.ttf' \
    --exclude='*.lock' --exclude='*.bin' --exclude='*.class' \
    --exclude='telemetry/telemetry' --exclude='toolchains/python-sdk-dev/dockerd/main')
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --reject "${{EXCLUDES[@]}}" /home/fix.patch 2>/dev/null || true
if grep -qhE '^diff --git a/go\\.(mod|sum)' /home/test.patch /home/fix.patch 2>/dev/null; then
    go mod download || true
fi
TEST_DIRS=$({{ grep -E '^diff --git a/\\S+_test\\.go' /home/test.patch \
    | sed -E 's#^diff --git a/(.+) b/.*#\\1#' \
    | grep -vE '(^|/)(integration|e2e)/' \
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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Anti-cheat hardening lives in the PR layer -- this is the image the model
        # is evaluated in, and the only layer that knows a single BASE_COMMIT.
        # prepare.sh has already checked out this PR's base.sha, so bake
        # Image._HARDENING_BLOCK with the literal sha: detach at base.sha, drop
        # origin, delete every ref (heads/remotes/tags/replace), expire the
        # reflogs, `gc --prune=now --aggressive` + repack, then ASSERT
        # HEAD == base.sha, no refs left, no remote left, and
        # `git rev-list --all --count` == `git rev-list HEAD --count`. Net effect:
        # the merge/fix commits that come after base.sha become unreachable and
        # are pruned, so `git log`, `git show <future-sha>` and `git fetch` cannot
        # be used to recover the gold patch.
        #
        # The literal sha (rather than a ${BASE_COMMIT} build-arg) is required:
        # run_evaluation.build_image only passes REPO_URL/BASE_COMMIT build-args
        # when dependency() is a str, and here it is an Image. For the same reason
        # DockerfileEnhancer.enhance() returns this Dockerfile unchanged, so the
        # layout below is exactly what gets built.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("dagger", "dagger_12949_to_9395")
class DAGGER_12949_TO_9395(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DaggerEra3ImageDefault(self.pr, self._config)

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
# number_interval for dagger/dagger bundles -- REGISTRY-SCOPED shim.
#
# The delivered JSONL carries no `number_interval`; it carries exact bundle
# membership in `prs_in_bundle`. The required value is the EXACT PRs joined
# with '-', never a range:
#
#     prs_in_bundle: [146, 147, 150, 155, 157]
#     number_interval: "146-147-150-155-157"      (NOT "146-157")
#
# A range would claim every PR in between, which is wrong -- these bundles are
# sparse (dagger anchors bundle ~20 PRs drawn from a whole release line, e.g.
# anchor 11548 bundles PRs scattered across v0.19.x, skipping many others).
#
# `Dataset.build()` copies number_interval straight off the loaded PullRequest
# into the resolved output jsonl, so filling it at load time is what makes it
# appear downstream. As this must live ONLY in the registry, two small,
# idempotent, dagger/dagger-scoped shims are installed at import time (this
# file is already imported by the package __init__, so nothing else is
# touched):
#
#   1. PullRequest.from_json / .from_dict -- for dagger/dagger records whose
#      number_interval is EMPTY, fill it from the raw record's prs_in_bundle.
#      Only empty values are filled, so an explicitly-set number_interval (e.g.
#      a legacy "dagger_12949_to_9395" era key) is never overwritten, and other
#      repos are untouched.
#   2. Instance.create -- routing looks up `dagger/<number_interval>`, and a
#      dash-joined bundle list is not a registered key. On the resulting
#      ValueError, fall back to the era class owning the bundle's ANCHOR PR
#      (pr.number) -- the PR whose base.sha the image is actually built at.
#
# Era boundaries are set by the `go` directive of go.mod AT EACH ANCHOR's
# base.sha, read from the real repo across all 132 records -- not by the
# nominal file-name ranges, which OVERLAP (era2 claims 6117-10210 and era3
# claims 9395-12949, so PRs 9395-10210 are claimed by both):
#
#     go.mod 1.16 - 1.20     PRs 1150-6001    -> era1, golang:1.20
#     go.mod 1.21 - 1.23.2   PRs 6117-9394    -> era2, golang:1.23
#     go.mod 1.24.0 - 1.25.6 PRs 9395-12949   -> era3, golang:1.25
#
# 9395 is the correct cut, not a midpoint of the overlap: PR 9395 (go 1.24.0)
# and PR 9518 (go 1.24.4) are the only records in the overlap requiring go
# >= 1.24, and GOTOOLCHAIN=local means golang:1.23 CANNOT satisfy them -- they
# must land in era3. The remaining overlap records (go 1.23.0) build fine on
# the newer toolchain, since a newer Go builds an older module unchanged.
#
# Deriving the era from pr.number instead of hardcoding bundle strings means a
# regenerated dataset with different bundles still routes without editing this
# file.
# ---------------------------------------------------------------------------

_DAGGER_ORG = "dagger"
_DAGGER_REPO = "dagger"

_DAGGER_ERA1 = "dagger_6001_to_1150"
_DAGGER_ERA2 = "dagger_10210_to_6117"
_DAGGER_ERA3 = "dagger_12949_to_9395"

# First anchor PR of each era (see the go.mod table above).
_DAGGER_ERA2_START = 6117
_DAGGER_ERA3_START = 9395


def dagger_number_interval(prs_in_bundle) -> str:
    """Dash-join a bundle's PR numbers: [146, 147, 150] -> '146-147-150'."""
    if not prs_in_bundle:
        return ""
    return "-".join(str(p) for p in prs_in_bundle)


def dagger_era_for_number(number) -> str:
    """Return the era registry key owning this anchor PR."""
    try:
        n = int(number)
    except (TypeError, ValueError):
        return ""
    if n >= _DAGGER_ERA3_START:
        return _DAGGER_ERA3
    if n >= _DAGGER_ERA2_START:
        return _DAGGER_ERA2
    return _DAGGER_ERA1


def _dagger_fill_number_interval(pr, raw) -> None:
    if not isinstance(raw, dict):
        return
    if getattr(pr, "org", "") != _DAGGER_ORG or getattr(pr, "repo", "") != _DAGGER_REPO:
        return
    if getattr(pr, "number_interval", ""):
        return
    interval = dagger_number_interval(raw.get("prs_in_bundle"))
    if interval:
        pr.number_interval = interval


if not getattr(PullRequest, "_dagger_ni_shim", False):
    _dagger_orig_from_json = PullRequest.from_json.__func__
    _dagger_orig_from_dict = PullRequest.from_dict.__func__

    # Signature-transparent (*args/**kwargs): the @dataclass_json decorator
    # REPLACES the class-body from_dict/from_json, so the live signatures are
    # dataclass_json's -- from_dict(cls, kvs, *, infer_missing=False) and
    # from_json(cls, s, *, parse_float=..., **kw). Its from_json delegates to
    # cls.from_dict(kvs, infer_missing=...), so a fixed 2-arg shim here breaks
    # every repo's loader, not just dagger's. Wrapping whatever is currently
    # installed (rather than the pristine dataclass_json function) also keeps
    # this composable with the other repo-scoped shims already in the package.
    def _dagger_from_json(cls, *args, **kwargs):
        pr = _dagger_orig_from_json(cls, *args, **kwargs)
        try:
            if args:
                _dagger_fill_number_interval(pr, _json.loads(args[0]))
        except Exception:
            pass
        return pr

    def _dagger_from_dict(cls, *args, **kwargs):
        pr = _dagger_orig_from_dict(cls, *args, **kwargs)
        try:
            raw = args[0] if args else kwargs.get("kvs")
            _dagger_fill_number_interval(pr, raw)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_dagger_from_json)
    PullRequest.from_dict = classmethod(_dagger_from_dict)
    PullRequest._dagger_ni_shim = True


if not getattr(Instance, "_dagger_route_shim", False):
    _dagger_orig_create = Instance.create.__func__

    def _dagger_create(cls, pr, config, *args, **kwargs):
        try:
            return _dagger_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == _DAGGER_ORG
                and getattr(pr, "repo", "") == _DAGGER_REPO
            ):
                era = dagger_era_for_number(getattr(pr, "number", None))
                key = f"{_DAGGER_ORG}/{era}" if era else ""
                if key and key in cls._registry:
                    return cls._registry[key](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_dagger_create)
    Instance._dagger_route_shim = True
