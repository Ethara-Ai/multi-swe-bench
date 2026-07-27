import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for grafana/mimir.
#
# Each instance is a release-delta BUNDLE. The raw record carries
# `prs_in_bundle` (e.g. [146, 147, 150, 155, 157]) but an EMPTY / null
# `number_interval`. The required output format is the dash-JOINED bundle list
# ("146-147-150-155-157") — NOT a "146-157" range, which would wrongly imply
# every PR between 146 and 157 is part of the bundle.
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "grafana/146-147-150-155-157"), which is not
#     registered → instance creation fails.
#
# So, following the aquasecurity/tfsec convention, we do two import-time
# monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_mimir_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_mimir_number_interval_patched", False):
    _mimir_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _mimir_from_json(cls, json_str):
        pr = _mimir_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "grafana"
                and raw.get("repo") == "mimir"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._mimir_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_mimir_from_json)
    _pull_request.PullRequest._mimir_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_mimir_build_patched", False):
        _mimir_orig_build = _Dataset.build.__func__

        def _mimir_build(cls, pr, report):
            ds = _mimir_orig_build(cls, pr, report)
            ni = getattr(pr, "_mimir_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_mimir_build)
        _Dataset._mimir_build_patched = True
# ---------------------------------------------------------------------------


# grafana/mimir — Grafana's Prometheus-compatible metrics store (Go monorepo).
#
# Discovery (dataset analysis):
#  - 95-PR Go range #929..#14832 across release branches (main + release-2.x).
#  - Test files under pkg/<service>/, integration/, cmd/<svc>/.
#  - Large monorepo: median 18 test packages per PR, max 84; mega-bundle
#    patches up to 828 files / 289 test files. Per-PR `go test` runs are
#    heavy but still much smaller than building everything.
#  - mimir uses cgo (snappy/lz4 compression and other chunk codecs), so
#    CGO_ENABLED=1 + a C toolchain are required.
#  - Per-PR: the test_patch's `*_test.go` files identify the Go packages to
#    exercise; `go test` runs each. Runs are fenced with `### MMRPKG ###`
#    markers so test ids stay unique across packages. integration/* tests
#    typically need running services (memcached/postgres/etc.) — they fail
#    or skip without them; unit tests in pkg/* are the resolvable signal.


def _test_pkgs(patch: str) -> list[str]:
    """Go package directories owning the `*_test.go` files in a patch."""
    pkgs: set[str] = set()
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
        if path.endswith("_test.go"):
            pkgs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(pkgs)


class MimirImageBase(Image):
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
        return "golang:1-bookworm"

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

        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() return this Dockerfile VERBATIM (it
        # early-returns when the directive is present). That deliberately
        # suppresses the enhancer's proxy / MITM / CA-cert injection (no proxy
        # build-args/ENVs, no cert symlinks, no MITM secret mount). The
        # `ca-certificates` apt package below is unrelated -- it is the standard
        # CA bundle for HTTPS `git clone` / `go mod download`.
        #
        # TOOLCHAIN-ONLY base (NO persistent clone), following the cloudwego/eino
        # model: the repo clone + `${{BASE_COMMIT}}` checkout live in the PER-PR
        # image (MimirImageDefault), so this ONE shared base is reusable by every
        # PR and each PR pins its own base commit. We still warm the SHARED Go
        # module cache (/go/pkg/mod) here from a THROWAWAY shallow clone so the
        # common deps download once instead of for all 95 PRs -- then remove it
        # so no /home/{repo} is baked in (the per-PR image clones fresh).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    GOTOOLCHAIN=auto \\
    GOFLAGS=-mod=mod \\
    CGO_ENABLED=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN ( git clone --depth 1 "${{REPO_URL}}" /tmp/{repo}-warm \\
      && cd /tmp/{repo}-warm && go mod download ) || true; \\
    rm -rf /tmp/{repo}-warm

RUN git config --global --add safe.directory '*'

{self.clear_env}

CMD ["/bin/bash"]
"""


class MimirImageDefault(Image):
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
        return MimirImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkgs = _test_pkgs(self.pr.test_patch)
        pkg_list = " ".join(pkgs) if pkgs else "."

        # The per-PR image clones + checks out ${BASE_COMMIT} INLINE in the
        # Dockerfile (eino model, see dockerfile()), so prepare.sh only warms the
        # build cache for this SHA's go.sum. The git-history anti-cheat is
        # enforced by the canonical Image._HARDENING_BLOCK appended afterwards.
        prepare = """#!/bin/bash
set -e
cd /home/__REPO__
git config --global --add safe.directory /home/__REPO__
go mod download 2>/dev/null || true
""".replace("__REPO__", repo)

        run_tests = """#!/bin/bash
set -uo pipefail
cd /home/__REPO__
go mod download 2>/dev/null || true

for pkg in __PKGS__; do
  [ -d "$pkg" ] || continue
  echo "### MMRPKG: $pkg ###"
  go test -v -count=1 -vet=off -timeout=20m "./$pkg/" 2>&1 || true
done
""".replace("__REPO__", repo).replace("__PKGS__", pkg_list)

        run_sh = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
""".replace("__REPO__", repo)

        excludes = (
            "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
            "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip "
            "--exclude=*.gz --exclude=*.tar --exclude=*.bin"
        )

        test_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        fix_run = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
bash /home/run_tests.sh
""".replace("__REPO__", repo).replace("__EXCLUDES__", excludes)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare),
            File(".", "run_tests.sh", run_tests),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        # Single COPY of all scripts/patches into /home/ (eino template style).
        copy_files = " ".join(f.name for f in self.files())

        # Per-PR image (cloudwego/eino model): clone full history, pin
        # ${BASE_COMMIT} inline, COPY scripts, warm the build cache (prepare.sh),
        # then the CANONICAL Image._HARDENING_BLOCK -- detach at ${BASE_COMMIT},
        # remove origin, delete all refs, reflog-expire, gc/repack, drop
        # alternates, plus the HEAD==BASE_COMMIT / empty-refs / rev-list asserts,
        # then a recursive submodule strip. dependency() returns an Image, so the
        # DockerfileEnhancer returns this Dockerfile VERBATIM -- the per-PR
        # clone/pin/harden below are kept as written (pinning here is correct: it
        # is per-PR, NOT the shared base). The hardening block is concatenated
        # RAW (not via an f-string) so its ${BASE_COMMIT} / %(refname) tokens
        # stay literal.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh || true

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("grafana", "mimir")
class Mimir(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MimirImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `go test -v` per-test result lines (possibly indented for subtests):
        #   --- PASS: TestQuery (0.01s)
        #   --- FAIL: TestStore (0.02s)
        #   --- SKIP: TestIntegration (0.00s)
        # Fenced by `### MMRPKG: <pkg> ###` so ids stay unique across packages.
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")
        pkg_re = re.compile(r"^### MMRPKG:\s+(\S+)\s+###")

        pkg = ""
        for line in clean.splitlines():
            line = line.rstrip()
            pm = pkg_re.match(line.strip())
            if pm:
                pkg = pm.group(1)
                continue
            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            tid = f"{pkg}::{name}" if pkg and pkg != "." else name
            if status == "PASS":
                passed_tests.add(tid)
            elif status == "FAIL":
                failed_tests.add(tid)
            elif status == "SKIP":
                skipped_tests.add(tid)

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
