import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_IMAGE = "golang:1.25-bookworm"
_TAG = "era3"


class _ImageBase(Image):
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
        return _GO_IMAGE

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

        # SINGLE shared toolchain base for every era (tag "base"). The `# syntax`
        # directive makes DockerfileEnhancer.enhance() return this verbatim: no
        # proxy args / cert symlinks / MITM mount injected. It must NOT clone the
        # repo -- the tag is shared by all 35 PRs, so a clone here would be
        # force-pinned by the hardening pass to whichever PR built the base first,
        # breaking the rest. The clone lives per-PR in _ImageDefault.
        #
        # BOTH protobuf-codegen toolchains are installed side by side: protoc-gen-go
        # is a single-name binary that cannot hold two versions at once, and old
        # LocalAI (<= PR 4999) regenerates .pb.go with protoc-gen-go v1.31.0 /
        # grpc v1.3.0 while newer PRs use v1.34.2 / grpc HEAD. Each era's prepare.sh
        # symlinks the version its checkout expects to /go/bin/protoc-gen-go before
        # `make protogen-go`, so one base serves all eras without a codegen mismatch.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ENV GOTOOLCHAIN=auto
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    clang \\
    cmake \\
    curl \\
    git \\
    pkg-config \\
    protobuf-compiler \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.31.0 && mv /go/bin/protoc-gen-go /go/bin/protoc-gen-go-1.31.0 && \\
    go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2 && mv /go/bin/protoc-gen-go /go/bin/protoc-gen-go-1.34.2 && \\
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.3.0 && mv /go/bin/protoc-gen-go-grpc /go/bin/protoc-gen-go-grpc-1.3.0 && \\
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@1958fcbe2ca8bd93af633f11e97d44e567e945af && mv /go/bin/protoc-gen-go-grpc /go/bin/protoc-gen-go-grpc-new

{self.clear_env}

CMD ["/bin/bash"]
"""


class _ImageDefault(Image):
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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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
                "extract_packages.sh",
                """#!/bin/bash
_extract_go_packages() {
    local PKGS
    PKGS=$(grep -h '^diff --git a/' "$@" 2>/dev/null \\
        | sed 's|diff --git a/||;s| b/.*||' \\
        | grep '\\.go$' \\
        | grep -v '^vendor/' \\
        | xargs -I{} dirname {} \\
        | sort -u)

    local result=""
    for pkg in $PKGS; do
        if [ -d "$pkg" ] && compgen -G "$pkg/*.go" > /dev/null 2>&1; then
            result="$result ./$pkg"
        fi
    done
    echo "$result"
}

_extract_test_names_in_pkg() {
    local pkg_path="$1"
    shift
    for patch in "$@"; do
        awk -v pkg="$pkg_path" '
            /^diff --git / {
                in_pkg = ($3 ~ ("^a/" pkg "/[^/]+_test\\.go$"))
            }
            in_pkg && /^\\+func +Test[A-Z]/ {
                if (match($0, /Test[A-Z][A-Za-z0-9_]*/)) {
                    print substr($0, RSTART, RLENGTH)
                }
            }
        ' "$patch" 2>/dev/null
    done | sort -u
}

GO_TEST_PKGS=$(_extract_go_packages "$@")
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Repo is already cloned and checked out at ${{BASE_COMMIT}} by the Dockerfile;
# no git checkout here (it would fight the hardening pass that follows).
git reset --hard
bash /home/check_git_changes.sh

export PATH="$PATH:/go/bin"
# Select the protobuf codegen version this era's checkout expects
# (both are pre-installed in the shared base).
ln -sf /go/bin/protoc-gen-go-1.34.2 /go/bin/protoc-gen-go
ln -sf /go/bin/protoc-gen-go-grpc-new /go/bin/protoc-gen-go-grpc
timeout 300 make protogen-go 2>&1 | tail -30 || true

go test -v -count=1 -timeout 3m ./pkg/utils/... 2>&1 | tail -5 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="${{SYNTH_PATCHES:-}}"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch 2>/dev/null || \
    git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null || \
    git apply --reject --whitespace=nowarn /home/test.patch 2>/dev/null || true
go mod tidy 2>/dev/null || true
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="/home/test.patch"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set +e

cd /home/{pr.repo}
(git apply --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null || \
    (git apply --3way --whitespace=nowarn /home/test.patch 2>/dev/null && \
     git apply --3way --whitespace=nowarn /home/fix.patch 2>/dev/null) || \
    (git apply --reject --whitespace=nowarn /home/test.patch 2>/dev/null; \
     git apply --reject --whitespace=nowarn /home/fix.patch 2>/dev/null) || true)
go mod tidy 2>/dev/null || true
export PATH="$PATH:/go/bin"
source /home/extract_packages.sh /home/test.patch /home/fix.patch
if [ -z "$GO_TEST_PKGS" ]; then
    echo "No buildable packages derived from patches; exiting cleanly."
    exit 0
fi
_synth_patches="/home/test.patch /home/fix.patch"
echo "Testing packages: $GO_TEST_PKGS"
SECONDS=0
BUDGET=450
for pkg in $GO_TEST_PKGS; do
    if [ "$SECONDS" -gt "$BUDGET" ]; then
        echo "Budget ${{BUDGET}}s exceeded (elapsed=${{SECONDS}}s); skipping remaining"
        break
    fi
    echo "==> $pkg (elapsed=${{SECONDS}}s)"
    pkg_out=$(timeout 90 go test -v -count=1 -timeout 60s "$pkg" 2>&1)
    echo "$pkg_out"
    if [ -n "$_synth_patches" ] && echo "$pkg_out" | grep -q '\[build failed\]'; then
        pkg_path="${{pkg#./}}"
        for tn in $(_extract_test_names_in_pkg "$pkg_path" $_synth_patches); do
            echo "--- FAIL: $tn (synthetic: build failed in $pkg)"
        done
    fi
done
echo "Total elapsed: ${{SECONDS}}s"
exit 0
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-level per-PR image FROM the shared era base. dependency() is an
        # *Image*, so DockerfileEnhancer returns this verbatim -- the clone,
        # checkout and Image._HARDENING_BLOCK below are kept exactly as written.
        # BASE_COMMIT is defaulted to this PR's sha because build_dataset only
        # passes REPO_URL/BASE_COMMIT build args for string-dependency images.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        org, repo = self.pr.org, self.pr.repo

        copy_commands = ""
        for f in self.files():
            copy_commands += f"COPY {f.name} /home/\n"

        header = f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


def _parse_go_test_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"--- PASS: (\S+)")
    re_fail = [re.compile(r"--- FAIL: (\S+)")]
    re_skip = re.compile(r"--- SKIP: (\S+)")

    def base_name(name: str) -> str:
        i = name.rfind("/")
        return name if i == -1 else name[:i]

    for raw in test_log.splitlines():
        line = raw.strip()

        m = re_pass.match(line)
        if m:
            name = m.group(1)
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(base_name(name))

        for rp in re_fail:
            m = rp.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(base_name(name))

        m = re_skip.match(line)
        if m:
            name = m.group(1)
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(base_name(name))

    passed_tests -= failed_tests
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


@Instance.register("mudler", "LocalAI_99999_to_5000")
class LocalAI_99999_to_5000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_go_test_log(test_log)


# ---------------------------------------------------------------------------
# Routing dispatcher -- REGISTRY-SCOPED (added to this LocalAI era module only;
# the package __init__.py is NOT modified).
#
# mudler__LocalAI_lht_raw.jsonl carries neither `tag` nor `number_interval`, so
# Instance.create resolves every record to the plain key "mudler/LocalAI".
# Nothing registered that key -- only the era keys -- so all 35 records failed
# with "Instance 'mudler/LocalAI' is not registered" before any image built.
#
# This class registers "mudler/LocalAI" and delegates to the era config whose
# PR-number range covers the record (contiguous, non-overlapping: 0-999,
# 1000-4999, 5000+). The delegate is resolved from Instance._registry at
# instantiation time, so it does not depend on module import order. Records that
# DO carry an era tag keep routing through their own key and never reach here.
# ---------------------------------------------------------------------------
def _localai_era_key(number: int) -> str:
    if number <= 999:
        return "mudler/LocalAI_999_to_0"
    if number <= 4999:
        return "mudler/LocalAI_4999_to_1000"
    return "mudler/LocalAI_99999_to_5000"


@Instance.register("mudler", "LocalAI")
class LocalAI(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        key = _localai_era_key(pr.number)
        era_cls = Instance._registry.get(key)
        if era_cls is None:
            raise ValueError(
                f"mudler/LocalAI: no era config registered for PR {pr.number} "
                f"(expected key {key!r})"
            )
        self._delegate = era_cls(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._delegate.parse_log(test_log)


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (added to this LocalAI
# era module only; the package __init__.py is NOT modified).
#
# The resolved dataset jsonl writes number_interval from the loaded PullRequest
# (Dataset.build -> number_interval=pr.number_interval). mudler__LocalAI_lht_raw
# carries an EMPTY number_interval and has NO `prs_in_bundle` field at all, so it
# would stay "" in the output. Each raw record is a single PR, so its bundle is
# effectively [itself] and the interval is just the PR number. (If a future
# bundled variant DOES ship prs_in_bundle, we honour the standard format: the
# EXACT PRs joined with "-", e.g. [146,147,150] -> "146-147-150", never a
# first-last range.)
#
# Two idempotent, mudler/LocalAI-scoped shims installed at import time:
#   1. PullRequest.from_json -- fill an empty number_interval (from prs_in_bundle
#      if present, else from the PR number).
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      "mudler/<interval>", which is not a registered key, so without this the
#      fill would BREAK the routing the LocalAI dispatcher above provides. Fall
#      back to "mudler/LocalAI" so the era dispatch still happens. Other repos are
#      unaffected (only mudler/LocalAI is filled / falls back).
# ---------------------------------------------------------------------------
import json as _localai_json  # noqa: E402


def _localai_interval(json_str: str, number) -> str:
    """Dash-joined prs_in_bundle if present, else the PR number as a 1-elem interval."""
    try:
        prs = (_localai_json.loads(json_str) or {}).get("prs_in_bundle") or []
    except Exception:
        prs = []
    if prs:
        return "-".join(str(p) for p in prs)
    return "" if number is None else str(number)


if not getattr(PullRequest, "_mudler_ni_shim", False):
    _mud_orig_from_json = PullRequest.from_json.__func__

    def _mud_from_json(cls, json_str):
        pr = _mud_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "mudler"
                and getattr(pr, "repo", "") == "LocalAI"
                and not getattr(pr, "number_interval", "")
            ):
                interval = _localai_interval(json_str, getattr(pr, "number", None))
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_mud_from_json)
    PullRequest._mudler_ni_shim = True


if not getattr(Instance, "_mudler_route_shim", False):
    _mud_orig_create = Instance.create.__func__

    def _mud_create(cls, pr, config, *args, **kwargs):
        try:
            return _mud_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == "mudler" and getattr(pr, "repo", "") == "LocalAI":
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_mud_create)
    Instance._mudler_route_shim = True
