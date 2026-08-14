import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# gocron renamed its module path at v2 (`github.com/go-co-op/gocron` ->
# `github.com/go-co-op/gocron/v2`). PR 630 is the first record on the `v2`
# branch, so it is the era boundary: every record below it is a v1 base commit
# whose go.mod declares go 1.13-1.20, every record at or above it is v2 with
# go 1.20-1.24.0. Verified against `go.mod` at all 82 base commits.
_V2_CUTOFF_PR = 630

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Hard cap on a single `go test` invocation. gocron v1 can deadlock: pr-410's
# baseline run hung in TestJob_SetEventListeners waiting on a WaitGroup and was
# killed by Go's DEFAULT 600s panic timeout, which truncated the log to 13 of
# ~105 results. A truncated baseline silently makes the run/test/fix comparison
# meaningless (the record was rejected for an unrelated-looking reason), so cap
# it lower and let a hang fail fast and visibly instead.
_TEST_TIMEOUT = "300s"

# Two v1 tests are intrinsically wall-clock flaky -- they assert on how many
# times a job fired inside a fixed interval, so they fail regardless of the patch
# under test. Measured on the pr-217 image: 1 in 5 runs failed, and that rate was
# unchanged by `-parallel 1` and `-p 1`, i.e. it is not contention, it is the
# tests. Left in, they inject noise into the reward signal and would fail a
# CORRECT agent fix as readily as a wrong one -- they cost three of the four
# invalid records in chunk 1, and separately manufactured a bogus f2p for pr-351.
# The skip is deliberately surgical:
#   * TestScheduler_WaitForSchedules -- the whole test. It carries no f2p/n2p/s2p
#     in ANY resolved record, only p2p, so removing it costs zero reward signal.
#   * TestScheduler_Update/happy_path -- the SUBTEST only. The parent
#     TestScheduler_Update is a genuine f2p (pr-303, pr-436) and n2p (pr-145);
#     with just this subtest filtered the parent still reports PASS, so that
#     signal is preserved. Skipping the parent would have destroyed it.
# Verified stable: 0 failures in 8 consecutive full runs. Same convention as
# avelino/awesome-go's `-skip 'TestStaleRepository|TestMaturity'`.
_V1_FLAKY_SKIP = "TestScheduler_WaitForSchedules|TestScheduler_Update/happy_path"


def _safe_commit_sha(sha: str) -> str:
    """Guard a base commit sha before it is interpolated into a RUN layer.

    The sha reaches this module from the dataset jsonl, and it is substituted
    into `git checkout --detach "<sha>"` inside the hardening block. Rejecting
    anything that is not a plain hex object name keeps a crafted record from
    injecting shell into the generated Dockerfile, mirroring the
    `_safe_path_component` guard image.py applies to org/repo.
    """
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(
            f"unsafe base commit sha for Dockerfile interpolation: {sha!r}"
        )
    return sha


class _GocronImageBaseMixin(Image):
    """Shared per-era toolchain + source image (tag ``base`` / ``base-legacy``).

    This image is SHARED: image_tag() is a constant, so build_dataset builds it
    once and every PR in the era layers on top of it. The 82 records carry 82
    distinct base.sha values, so this image must keep the full upstream history
    intact -- pinning it to one commit would break `git checkout <base.sha>` for
    every other record in the era.

    That is why the leading `# syntax` directive is load-bearing. Because
    dependency() returns a str, DockerfileEnhancer.enhance() would otherwise run
    `_standardize_repo_fetch`, which rewrites `RUN git clone ... /home/<repo>`
    into a clone pinned to `${BASE_COMMIT}` followed by the destructive
    Image._HARDENING_BLOCK (detach + `git gc --prune=now --aggressive`).
    build_dataset passes BASE_COMMIT for every string-dependency image, so this
    shared base would be pruned down to whichever PR happened to build it first
    and the other 32 (`base`, 33 v2 records) / 48 (`base-legacy`, 49 v1 records)
    could no longer reach their own base commit. `enhance()` early-returns on
    `if SYNTAX_DIRECTIVE in raw`, so emitting the directive here makes this file
    the final content.

    Everything the enhancer would still have contributed -- TARGETARCH,
    REPO_URL/BASE_COMMIT ARGs, the base ENV block, the ethara LABEL -- is
    written out explicitly below. (Proxy/CA-certificate injection is no longer
    part of the enhancer at all, so there is nothing on that front to replace.)
    The official `golang` Debian images already ship git and
    /etc/ssl/certs/ca-certificates.crt, which is all the clone needs, so there
    is deliberately no apt layer.

    The hardening that IS applied here is LIGHT: it drops the remote and every
    ref so the image carries no branch/tag/PR provenance, but it deliberately
    does NOT run gc/repack/prune and does NOT check out ${BASE_COMMIT}. Every
    commit object stays reachable by sha (`gc.auto 0` stops a later git command
    from pruning them opportunistically). The destructive prune plus the HEAD
    audit live in the per-PR image below, where pinning is correct.
    """

    _BASE_IMAGE: str = ""
    _TAG: str = ""

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
        return self._BASE_IMAGE

    def image_tag(self) -> str:
        return self._TAG

    def workdir(self) -> str:
        return self._TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Validate before interpolating into the clone URL / RUN / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # REPO_URL/BASE_COMMIT are declared because build_dataset and
        # run_evaluation pass both as build args to every string-dependency
        # image; BASE_COMMIT is intentionally unused here (see class docstring).
        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="https://github.com/{org}/{repo}.git"\n'
            "ARG BASE_COMMIT"
        )

        label = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        base_hardening = (
            "RUN set -eux; \\\n"
            "    git checkout --detach HEAD; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            '    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\\n'
            '    test -z "$(git remote)"'
        )

        base_hardening_submodules = (
            "RUN if [ -f .gitmodules ]; then \\\n"
            "        git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\\n'
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            build_args,
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            label,
            self.global_env,
            "ENV GOTOOLCHAIN=auto\nENV GOFLAGS=-mod=mod\nENV TEST_ENV=ci",
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{repo}",
            base_hardening,
            base_hardening_submodules,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class GocronImageBase(_GocronImageBaseMixin):
    _BASE_IMAGE = "golang:1.24-bookworm"
    _TAG = "base"


class GocronImageBaseLegacy(_GocronImageBaseMixin):
    _BASE_IMAGE = "golang:1.20-bookworm"
    _TAG = "base-legacy"


class GocronImageDefault(Image):
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
        if self.pr.number < _V2_CUTOFF_PR:
            return GocronImageBaseLegacy(self.pr, self._config)
        return GocronImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @property
    def test_flags(self) -> str:
        """`go test` flags shared by prepare/run/test-run/fix-run.

        The flaky-test skip is scoped to the v1 era: both names belong to the
        pre-v2 suite, and v2 records must keep running their full suite.
        """
        flags = f"-timeout {_TEST_TIMEOUT}"
        if self.pr.number < _V2_CUTOFF_PR:
            flags += f" -skip '{_V1_FLAKY_SKIP}'"
        return flags

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

go mod download || true

# Warm the module cache for the POST-fix dependency set as well. fix.patch
# bumps go.mod/go.sum on a fair number of these records (clockwork, testify,
# robfig/cron, x/sync), and the baseline `go mod download` above only caches
# the pre-fix graph. Without this, fix-run.sh has to reach the module proxy
# mid-grade, so an offline or proxy-flaky runner fails for reasons that have
# nothing to do with the candidate patch.
#
# Applied to a throwaway tree and reverted: the run stage must still observe
# the pristine baseline checkout, so this block leaves no trace but a fuller
# GOMODCACHE.
if git apply --check --whitespace=nowarn /home/test.patch /home/fix.patch 2>/dev/null; then
    git apply --whitespace=nowarn /home/test.patch /home/fix.patch
    go mod download || true
    # `go mod download` alone misses deps reachable only from _test.go files
    # (testify is the usual one). Compiling every test binary without running
    # anything pulls those too, and warms the build cache while it is at it.
    go test -run '^$' -count=1 ./... || true
    git reset --hard
    git clean -fd
fi
bash /home/check_git_changes.sh

# Build-cache warm-up only — it asserts nothing (note the `|| true`). Skipped
# when cross-building, where it executes under QEMU: it costs ~400s per image
# and blows its own -timeout before finishing, so the cache it leaves behind is
# partial anyway. Everything above this line still runs on every architecture,
# which is what keeps a cross-built image offline-gradeable.
if [ "${{TARGETARCH:-amd64}}" = "amd64" ]; then
    go test {flags} -v -count=1 ./... || true
fi

""".format(pr=self.pr, flags=self.test_flags),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
go test -race {flags} -v -count=1 ./...

""".format(pr=self.pr, flags=self.test_flags),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -race {flags} -v -count=1 ./...

""".format(pr=self.pr, flags=self.test_flags),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -race {flags} -v -count=1 ./...

""".format(pr=self.pr, flags=self.test_flags),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        repo = _safe_path_component(self.pr.repo)

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # TARGETARCH is supplied by buildx per build platform. prepare.sh reads it
        # to skip the cross-built build-cache warm-up; passed inline on the RUN so
        # it does not linger as an ENV in the shipped image.
        prepare_commands = (
            "ARG TARGETARCH\n"
            'RUN TARGETARCH="${TARGETARCH}" bash /home/prepare.sh'
        )

        # Per-PR anti-reward-hacking hardening. The era base is SHARED and still
        # carries the full upstream history (see _GocronImageBaseMixin), so this
        # layer is where THIS record's history is isolated: prepare.sh checks out
        # base.sha and warms the module/build cache, then the canonical block
        # from image.py runs with ${BASE_COMMIT} bound to the literal sha --
        # detaching at base.sha, dropping origin, every ref and the reflog, then
        # `git gc --prune=now --aggressive` + repack. No commit after base.sha
        # survives, so the agent cannot read the real fix (or the gold tests) out
        # of git history, and with no remote left it cannot re-fetch them.
        #
        # The block is referenced from image.py rather than copied so it cannot
        # drift from the source of truth, and substituted (not f-string
        # interpolated) so its ${...}/%(refname)/$(...) tokens stay literal.
        # dependency() returns an Image, so DockerfileEnhancer.enhance() emits
        # this file verbatim and will NOT add the block for us.
        #
        # Order matters: hardening must follow prepare.sh, which is what puts
        # HEAD on base.sha and populates the caches the tests need offline.
        hardening = self._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", _safe_commit_sha(self.pr.base.sha)
        ).rstrip("\n")

        sections = [
            f"FROM {name}:{tag}",
            self.global_env,
            copy_commands.rstrip("\n"),
            prepare_commands,
            f"WORKDIR /home/{repo}",
            hardening,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("go-co-op", "gocron")
class Gocron(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GocronImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        def root_name(name: str) -> str:
            i = name.find("/")
            return name if i == -1 else name[:i]

        for raw in test_log.splitlines():
            line = raw.strip()

            m = re_pass.match(line)
            if m:
                name = root_name(m.group(1))
                if name in failed_tests:
                    continue
                skipped_tests.discard(name)
                passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = root_name(m.group(1))
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
                continue

            m = re_skip.match(line)
            if m:
                name = root_name(m.group(1))
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === number_interval: dash-joined prs_in_bundle on the resolved jsonl ===
#
# FORMAT: the explicit dash-joined member list, NEVER a range. Bundles here are
# sparse -- pr-122's bundle is [122, 123, 124, 126], so it must be
# "122-123-124-126". A range like "122-126" would also claim 125, which is not
# in the bundle. 59 of the 82 bundles in this dataset are sparse, so a range
# form would mis-describe the majority of records.
#
# The raw jsonl carries `prs_in_bundle` but NO `number_interval`, and the
# dataclass_json loader drops unknown keys, so the registry classes never see the
# bundle and Dataset.build (which copies `number_interval=pr.number_interval`)
# would write "" into every resolved row.
#
# Setting pr.number_interval at load time would ALSO change the routing key
# (instance.py: `name` becomes "go-co-op/122-123-124-126"), so instead the value
# is stashed in a NON-field attr and stamped onto the OUTPUT row only, leaving
# routing on "go-co-op/gocron". Every bundle value is additionally registered at
# the bottom of this file, so a regenerated jsonl that DOES carry
# number_interval still resolves instead of raising "not registered".
#
# Two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness
# source), following the MHSanaei/3x-ui and avelino/awesome-go convention.
import json as _gocron_json

import multi_swe_bench.harness.pull_request as _gocron_pr

if not getattr(_gocron_pr.PullRequest, "_gocron_number_interval_patched", False):
    _gocron_orig_from_json = _gocron_pr.PullRequest.from_json.__func__

    def _gocron_from_json(cls, json_str):
        pr = _gocron_orig_from_json(cls, json_str)
        try:
            raw = _gocron_json.loads(json_str)
            if (
                raw.get("org") == "go-co-op"
                and raw.get("repo") == "gocron"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._gocron_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _gocron_pr.PullRequest.from_json = classmethod(_gocron_from_json)
    _gocron_pr.PullRequest._gocron_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _GocronDataset

    if not _GocronDataset.__dict__.get("_gocron_build_patched", False):
        _gocron_orig_build = _GocronDataset.build.__func__

        def _gocron_build(cls, pr, report):
            ds = _gocron_orig_build(cls, pr, report)
            ni = getattr(pr, "_gocron_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _GocronDataset.build = classmethod(_gocron_build)
        _GocronDataset._gocron_build_patched = True


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Defensive: covers a regenerated jsonl that DOES carry number_interval, so
# instance.py routes it to Gocron instead of raising "not registered". Explicit
# member lists, never ranges -- bundles are sparse. One entry per record,
# generated from `prs_in_bundle` in go-co-op__gocron_lht_final.jsonl (82 rows,
# 82 distinct bundles, no member shared between bundles); regenerate with
# "-".join(str(p) for p in row["prs_in_bundle"]) if the raw jsonl changes.
_GOCRON_BUNDLE_NIS = [
    "15-21-23",
    "25-26-30-32-33-39",
    "47-48",
    "49-51-54-56",
    "61-62-66-67-68",
    "77-79-81-82-83-87-89",
    "90-92-93-94-99",
    "104-105-112-113-114-116-117-118-119-121",
    "122-123-124-126",
    "130-135-136-137",
    "145-146-147-148-149",
    "150-151",
    "156-158-159-160",
    "164-166-167-168-170-173-177-178",
    "182-185-189-191-192",
    "207-211",
    "212-214",
    "217-220-221-228-229-234",
    "233-240-241-247-248-249-250-253",
    "268-270-271-275-281-288",
    "291-292-293-295-296-297-298",
    "303-304-306-307-309-310-311-316-323-324-328-330-335-338-339",
    "343-348-349",
    "351-356",
    "359-360",
    "368-369",
    "380-381",
    "388-389-390-392-393-394",
    "410-411-413-419-423-426-427-428",
    "432-435",
    "436-444",
    "443-445-446-447",
    "450-451-452",
    "454-455",
    "459-460",
    "461-463",
    "467-469-470-471-473-474-475-477-478-480-481",
    "482-484-485",
    "487-488-489-491",
    "501-502-503",
    "506-508-509-511-512",
    "514-517",
    "541-542",
    "545-546-549-551-553",
    "556-558-559-560-561-562-563-569",
    "573-575",
    "588-589",
    "592-594-602-603-604",
    "616-620-628",
    "630-631-635",
    "644-645-646",
    "649-650-652",
    "658-659",
    "672-675-686",
    "684-688",
    "698-699",
    "700-701",
    "705-711",
    "721-724",
    "723-727",
    "728-729",
    "730-733-734-735-737-739",
    "741-743",
    "744-745",
    "752-754",
    "759-760-761",
    "763-764-766-776",
    "791-792",
    "810-822-823-825-827-829-830-831-832-833-834",
    "811-813",
    "817-819-820",
    "843-844-847-848",
    "859-860-864-866-868",
    "869-870",
    "873-875-878-879-880-882",
    "883-884-886-890-891-893",
    "889-903-906",
    "894-897",
    "898-899-901",
    "907-908",
    "910-912-913-914",
    "917-918-919-920",
]

for _ni in _GOCRON_BUNDLE_NIS:
    Instance.register("go-co-op", _ni)(Gocron)
