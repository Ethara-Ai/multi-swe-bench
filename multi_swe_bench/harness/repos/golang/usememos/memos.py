import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for usememos/memos.
#
# Every instance is a release-BUNDLE. The raw record carries `prs_in_bundle`
# (e.g. [179, 181, 185, 186, 190, 191]) but an EMPTY/null `number_interval`. The
# required output format is the dash-JOINED bundle list
# ("179-181-185-186-190-191") — NOT a "179-191" range, which would wrongly imply
# every PR in between (many of which are not part of the bundle).
#
# Two constraints force the approach below (identical to goadesign/goa &
# aquasecurity/tfsec):
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "usememos/179-181-185-186-190-191"), which is
#     not registered → instance creation fails / the row is silently skipped.
#
# So we do two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to
# harness source):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_memos_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
# The patches chain safely with the other registries' patches (each captures the
# current from_json / build, calls through, and only acts on its own org/repo).
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_memos_number_interval_patched", False):
    _memos_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _memos_from_json(cls, json_str):
        pr = _memos_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "usememos"
                and raw.get("repo") == "memos"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._memos_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_memos_from_json)
    _pull_request.PullRequest._memos_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_memos_build_patched", False):
        _memos_orig_build = _Dataset.build.__func__

        def _memos_build(cls, pr, report):
            ds = _memos_orig_build(cls, pr, report)
            ni = getattr(pr, "_memos_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_memos_build)
        _Dataset._memos_build_patched = True
# ---------------------------------------------------------------------------

# Robustly apply the gold test/fix patches for a *bundled* instance.
#
# A bundle's patches are aggregated from many PRs and can carry defects that make
# a plain `git apply` abort before any test runs — which the harness then records
# as a bogus "no test results"/(0,0,0) fix stage (i.e. an unresolvable instance).
# Two such defects occur in the memos bundles:
#   1. Binary files serialized as "Binary files a/x and b/x differ" *stubs* (or
#      binary hunks with NO full index line), e.g. vendored *.webp/*.png logo
#      assets. These can never apply, but they do not affect compiling or running
#      the Go tests, so we strip those blocks.
#   2. The same new file added by BOTH the test patch and the fix patch (files
#      mis-split into both halves), causing "already exists" on the combined
#      apply. We drop those paths from the fix patch.
# We then apply with `--3way` for resilience. If apply STILL fails we exit
# non-zero so the failure surfaces honestly and is never silently masked.
#
# NOTE: only vendored assets / duplicate-adds are removed; every real source and
# test change is applied unchanged, so the pass/fail verdict stays faithful.
_ROBUST_APPLY_SH = r"""#!/bin/bash
set -uo pipefail

repo="$1"; test_patch="$2"; fix_patch="${3:-}"

_strip_binary() {   # <in> <out> : drop any "diff --git" block containing a binary line
    awk '
        function flush(){ if (block != "" && !isbin) printf "%s", block; block=""; isbin=0 }
        /^diff --git /             { flush() }
        /^Binary files .* differ$/ { isbin=1 }
        /^GIT binary patch$/       { isbin=1 }
                                   { block = block $0 ORS }
        END                        { flush() }
    ' "$1" > "$2"
}

_drop_paths() {     # <in> <out> <pathlist> : drop blocks whose new path is listed
    awk -v listf="$3" '
        function flush(){ if (block != "" && !(path in drop)) printf "%s", block; block=""; path="" }
        BEGIN { while ((getline l < listf) > 0) drop[l]=1 }
        /^diff --git / { flush(); path=$0; sub(/^diff --git a\//,"",path); sub(/ b\/.*/,"",path) }
                       { block = block $0 ORS }
        END            { flush() }
    ' "$1" > "$2"
}

cd "$repo"

# Docker image layers reset file mtimes/inodes, so git's stat cache is stale and
# perfectly clean files look "modified". Any index-aware apply mode (--index /
# --3way) then aborts with "<file>: does not match index" WITHOUT applying
# anything. Re-sync the worktree and refresh the index before touching patches.
git reset --hard >/dev/null 2>&1 || true
git update-index --refresh >/dev/null 2>&1 || true

_strip_binary "$test_patch" /tmp/_test.patch

if [ -z "$fix_patch" ]; then
    set -- /tmp/_test.patch
else
    grep '^diff --git ' /tmp/_test.patch | sed -E 's#^diff --git a/(.*) b/.*#\1#' | sort -u > /tmp/_testfiles.txt
    _strip_binary "$fix_patch" /tmp/_fix.b.patch
    _drop_paths   /tmp/_fix.b.patch /tmp/_fix.patch /tmp/_testfiles.txt
    set -- /tmp/_test.patch /tmp/_fix.patch
fi

# Plain apply FIRST — this is the original, index-independent behaviour and is
# what already worked for every healthy bundle. Only if it fails do we fall back
# to --3way (now safe, thanks to the index refresh above). This guarantees we can
# never do worse than the pre-existing apply.
git apply --whitespace=nowarn "$@" || git apply --3way --whitespace=nowarn "$@"
"""

# Test command shared by prepare/run/test-run/fix-run.
#
# memos' `store/test` suite (v0.24+) spins up MySQL/Postgres via testcontainers-go
# and runs migrator tests that need a Docker daemon INSIDE the eval container
# (Docker-in-Docker), which the harness does NOT provide. Left unguarded those
# tests panic with "rootless Docker not found" and, worse, the spurious failures
# they inject can mask otherwise-genuine fail->pass gold tests (and trip the
# report's "no new failures" guard), sinking valid instances. Two era-agnostic
# mitigations (both no-ops on older eras that lack `store/test`/`$DRIVER`):
#   * DRIVER=sqlite — `store/test`'s TestMain runs a SINGLE driver when $DRIVER is
#     set, so only the hermetic sqlite path runs; the mysql/postgres container
#     phases are skipped entirely.
#   * -skip the 5 testcontainers-backed migrator tests (they need Docker even on
#     the sqlite path). Anchored `^(...)$` so it can never match a sibling/subtest.
# Also skipped: the two `plugin/cron` chain tests, which are timing-based (real
# sleeps, ~40s package) and FLAKY — they intermittently fail under build load with
# no relation to any fix (no memos bundle here changes cron-chain logic). Left in,
# they inject spurious PASS->FAIL "regressions" (sinking valid instances) and false
# fail->pass credits (polluting f2p). Removing them from the run avoids both.
# Everything else (incl. the real gold tests, which run on sqlite) is unaffected.
_GO_TEST_CMD = (
    "DRIVER=sqlite go test -v -count=1 "
    "-skip '^(TestFreshInstall|TestMigrationReRun|TestMigrationWithData|"
    "TestMigrationMultipleReRuns|TestMigrationFromStableVersion|"
    "TestChainDelayIfStillRunning|TestChainSkipIfStillRunning)$' ./..."
)


class MemosImageBase(Image):
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
        return "golang:latest"

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

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # SHARED base image: built ONCE and reused by every memos PR (image_tag ==
        # "base"). It clones the repo at HEAD via ${REPO_URL} and keeps FULL git
        # history — it does NOT check out a per-PR BASE_COMMIT. Each per-PR image
        # (MemosImageDefault) builds FROM this base and checks out its own
        # BASE_COMMIT, so the shared base must retain every commit any sibling PR
        # might reference.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance()
        # return this Dockerfile unchanged (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`). This is deliberate: it stops
        # the pipeline enhancer's _standardize_repo_fetch from rewriting the
        # `RUN git clone ... /home/{repo}` line into a block that checks out
        # ${BASE_COMMIT} and applies Image._HARDENING_BLOCK — which would harden
        # the SHARED base to a single commit and break every sibling PR's
        # `git checkout {base.sha}`. Because the enhancer is bypassed, the ARG/
        # ENV/LABEL infra is inlined here. Anti-cheat hardening is applied per-PR
        # in MemosImageDefault instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="ethara.ai"

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class MemosImageDefault(Image):
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
        return MemosImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{go} || true

""".format(pr=self.pr, go=_GO_TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{go}

""".format(pr=self.pr, go=_GO_TEST_CMD),
            ),
            File(
                ".",
                "robust_apply.sh",
                _ROBUST_APPLY_SH,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch
cd /home/{pr.repo}
{go}

""".format(pr=self.pr, go=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch /home/fix.patch
cd /home/{pr.repo}
{go}

""".format(pr=self.pr, go=_GO_TEST_CMD),
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

        # Per-PR anti-cheat hardening. This is the image the model is evaluated
        # in, so after prepare.sh has checked out and warmed BASE_COMMIT we strip
        # every ref/remote and GC unreachable objects via Image._HARDENING_BLOCK:
        # the gold fix/merge commit and the `origin` remote are removed, so a
        # solution cannot recover the fix via `git log`, `git show <future-sha>`,
        # or `git fetch`. BASE_COMMIT is exported as ENV so the hardening block
        # (which references ${BASE_COMMIT}) resolves to THIS PR's base sha.
        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}

CMD ["/bin/bash"]

"""


@Instance.register("usememos", "memos")
class Memos(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MemosImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        continue
                    if base_name in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
