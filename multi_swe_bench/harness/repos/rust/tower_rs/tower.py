import base64
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Both image classes below override `dockerfile()` wholesale, which bypasses the
# validation the shared `Image.dockerfile()` performs on `pr.repo` before it is
# interpolated into RUN/WORKDIR paths. Every interpolated component is therefore
# routed through the shared `_safe_path_component` here, so both paths carry the
# same guarantee (see multi_swe_bench/harness/image.py).
#
# `pr.base.sha` is the one value that helper does not cover: upstream it arrives
# as the `${BASE_COMMIT}` build-arg, but this registry substitutes it as a literal
# into `git checkout --detach` (build args are only supplied when `dependency()`
# returns a str — see build_dataset.build_image — and the per-PR layer depends on
# an Image). A sha is validated as hex so it cannot carry shell metacharacters
# into that command.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _safe_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(f"unsafe base sha for Dockerfile interpolation: {sha!r}")
    return sha


class TowerImageBase(Image):
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
        return "rust:latest"

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

        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        # REPO_URL is declared as an ARG and consumed here, so the
        # `--build-arg REPO_URL=...` that build_dataset already passes for every
        # str-dependency image is honoured instead of discarded against a
        # hardcoded URL.
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        # ── WHY THE `# syntax` DIRECTIVE IS LOAD-BEARING ──────────────────────
        # This base carries the constant tag `base`, so ALL bundles in this
        # dataset resolve to ONE image. `dependency()` returns a str, which means
        # DockerfileEnhancer would otherwise rewrite the clone above into
        # `git checkout ${BASE_COMMIT}` followed by Image._HARDENING_BLOCK
        # (`git gc --prune=now --aggressive`).
        #
        # BASE_COMMIT is a single build-arg value — whichever bundle happens to
        # trigger the base build. Hardening a SHARED base therefore detaches it to
        # one bundle's commit and prunes every object not reachable from it, so
        # every other bundle's `git checkout {base.sha}` in prepare.sh fails
        # against a pruned object and the instance captures zero tests. Measured
        # in-container on this dataset: 1 of 8 base shas resolves, 7 report MISS.
        #
        # `enhance()` returns content verbatim when it already carries this
        # directive, so the base keeps FULL history. Per-PR hardening is applied
        # in TowerImageDefault instead, where the sha is known and unshared.
        #
        # Because the enhancer is skipped, its ARG/ENV/LABEL block is skipped too
        # and is written by hand here (same shape as the rust_clippy shared base).
        # Without it these images carry no provenance labels at all — they would
        # inherit only `image.source=github.com/rust-lang/docker-rust` from
        # rust:latest. BASE_COMMIT is deliberately NOT declared: one shared base,
        # many base shas, so any single value would be wrong.
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} shared base (all bundles)" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # `origin` is deliberately LEFT IN PLACE at this layer. `git remote remove
        # origin` also deletes refs/remotes/origin/*, and one bundle here
        # (671-672-673) has its base sha on the `v0.4.x` branch rather than
        # master — dropping the remote-tracking refs would leave that commit
        # unreferenced. `gc.auto 0` guarantees nothing prunes it before the per-PR
        # layer checks it out; that layer then removes the remote, deletes every
        # ref and prunes, so the graded image still ships with no upstream and no
        # reachable future history.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

{label_block}

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{repo}

RUN git config --local gc.auto 0 && \\
    git config --local fetch.recurseSubmodules false && \\
    git config --local remote.pushDefault ""

WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class TowerImageDefault(Image):
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
        return TowerImageBase(self.pr, self.config)

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

cargo test --workspace --all-features || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
cargo test --workspace --all-features

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
cargo test --workspace --all-features

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
cargo test --workspace --all-features

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

        prepare_commands = "RUN bash /home/prepare.sh"
        repo = _safe_path_component(self.pr.repo)

        # Per-PR anti-cheat hardening. This image depends on an Image (the shared
        # base), so DockerfileEnhancer emits its Dockerfile verbatim — it only
        # auto-injects hardening into str-dependency images, and BASE_COMMIT is
        # not passed as a build arg for FROM-an-image builds. Bake the canonical
        # block from image.py with the LITERAL base.sha instead, so the fix
        # commits cannot be read back out of git history (git log / show /
        # checkout) or re-fetched from origin inside the graded container.
        #
        # Placed AFTER prepare.sh on purpose: prepare.sh still needs the full
        # history to reach this bundle's base commit (one of them lives on the
        # v0.4.x branch), and it warms the cargo target/ cache. Pruning first
        # would break the checkout.
        hardening = self._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", _safe_sha(self.pr.base.sha)
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


# ── LOG PARSING ───────────────────────────────────────────────────────────────
# `cargo test --workspace` runs one binary per crate/target and libtest names are
# unique only WITHIN a binary, so bare names collide across the workspace. Verified
# on this dataset, not hypothetical: `when_inner_is_not_ready` exists in both
# tower-buffer and tower-spawn-ready; `new_service` in tower-buffer and tower-hedge;
# `poll_ready` in three separate tower test targets. Collapsing those into one set
# entry lets a FAIL in one crate erase a PASS in another, and it corrupted the
# "does this test already exist at base" analysis. Every id is therefore qualified
# with the binary that produced it, taken from cargo's `Running ...` header, with
# the content hash stripped so the id is stable across the run/test/fix stages.
#
# Cargo colourises those headers when it thinks the sink supports it, so escapes
# are stripped first -- otherwise the header is missed and every following test is
# attributed to the previous binary.
_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RE_TEST_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAILED|ignored)\b"
)
_RE_RUNNING = re.compile(r"^\s*Running\s+(?P<rest>.+?)\s*$")
_RE_DOCTESTS = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RE_DEP_HASH = re.compile(r"-[0-9a-fA-F]{6,}$")

# rustdoc names a doc-test after the SOURCE LINE it starts on, e.g.
# `tower-service/src/lib.rs - Service (line 243)`. The line number is not part of
# the test's identity -- it moves whenever anything above the example is edited, so
# an unchanged doc-test reappears as a brand-new N2P entry. `gen_eval_reports`
# requires an agent to reproduce every n2p id, so a line-numbered id silently
# demands the gold patch's exact byte layout in a documentation file.
_RE_DOCTEST_LINE_SUFFIX = re.compile(r"\s*\(line \d+\)\s*$")


def _binary_label(running_rest: str) -> str:
    """Stable test-binary label from a cargo `Running ...` line.

    Handles both spellings:
        Running unittests src/lib.rs (target/debug/deps/tower-9a1b2c3d4e5f)
        Running target/debug/deps/buffer-9a1b2c3d4e5f      (older cargo)
    """
    m = re.search(r"\(([^()]+)\)\s*$", running_rest)
    token = m.group(1) if m else running_rest.split()[-1]
    stem = token.replace("\\", "/").rsplit("/", 1)[-1]
    return _RE_DEP_HASH.sub("", stem) or stem


@Instance.register("tower-rs", "tower")
class Tower(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TowerImageDefault(self.pr, self._config)

    _APPLY_OPTS = "--whitespace=nowarn"

    # ── WHY THE STAGE COMMAND CARRIES ITS OWN RUNNER ──────────────────────────
    # `cargo test --workspace` builds EVERY target before running ANY of them, so
    # one test target that fails to compile yields ZERO captured tests for the
    # whole workspace. `--no-fail-fast` does not help (it governs test failures,
    # not build failures) and `cargo test` rejects `--keep-going`.
    #
    # That is the normal state of the TEST stage here: the gold tests exercise
    # APIs the fix introduces, so at least one target legitimately does not
    # compile until fix.patch lands. Measured on this dataset, 6 of 8 bundles
    # captured zero tests in the test stage for exactly that reason, which left
    # the classifier with no baseline. With no baseline every test that ran after
    # the fix looked brand new, so pre-existing tests were credited as N2P --
    # e.g. pr-303 put 49 tests in N2P that already exist at its base commit.
    #
    # So: try the whole workspace first (unchanged behaviour when everything
    # compiles), and only if that captured no test lines at all, re-run target by
    # target so a target that cannot compile loses only itself.
    #
    # The script is shipped INLINE, base64-encoded, rather than as a file baked
    # into the image. Two reasons: base64 is quote-free, so it survives being
    # embedded in a `bash -c '...'` command without any escaping hazard; and it
    # means an existing image needs no rebuild to pick up a change here.
    #
    # Every cargo call is time-capped. `--no-fail-fast` makes cargo reach test
    # binaries it used to abort before, and bundle 671 has a gold test that HANGS
    # at the base commit -- it once sat in one binary for 49 minutes with an empty
    # log. A hang would otherwise stall the whole dataset run silently.
    _RUNNER = r"""#!/bin/bash
set -uo pipefail
cd /home/tower

run_cargo() {
    timeout --kill-after=60 "$1" "${@:2}"
    rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        echo "##### TOWER_TIMEOUT after $1 s: ${*:2} #####"
    fi
    return 0
}

WS=/tmp/tower_ws.log
run_cargo 900 cargo test --workspace --all-features --no-fail-fast > "$WS" 2>&1
cat "$WS"
if grep -qE '^test .+ \.\.\. (ok|FAILED|ignored)' "$WS"; then
    exit 0
fi

echo '##### TOWER_FALLBACK per-target isolation #####'
cargo metadata --no-deps --format-version 1 > /tmp/tower_meta.json 2>/dev/null || exit 0
python3 - <<'PY' > /tmp/tower_targets.txt
import json
try:
    meta = json.load(open("/tmp/tower_meta.json"))
except Exception:
    raise SystemExit(0)
for pkg in meta.get("packages", []):
    for t in pkg.get("targets", []):
        kinds = t.get("kind", [])
        if "lib" in kinds:
            print("%s|--lib|" % pkg["name"])
            print("%s|--doc|" % pkg["name"])
        elif "test" in kinds:
            print("%s|--test|%s" % (pkg["name"], t["name"]))
PY
while IFS='|' read -r pkg flag name; do
    [ -z "$pkg" ] && continue
    if [ -n "$name" ]; then
        run_cargo 300 cargo test -p "$pkg" "$flag" "$name" --all-features --no-fail-fast 2>&1
    else
        run_cargo 300 cargo test -p "$pkg" "$flag" --all-features --no-fail-fast 2>&1
    fi
done < /tmp/tower_targets.txt
"""

    @classmethod
    def _runner_cmd(cls) -> str:
        """Decode + execute the runner; contains no quote characters."""
        blob = base64.b64encode(cls._RUNNER.encode("utf-8")).decode("ascii")
        return f"echo {blob} | base64 -d > /tmp/tower_run.sh ; bash /tmp/tower_run.sh 2>&1 || true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; git checkout -- . ; {runner}'".format(
            repo=_safe_path_component(self.pr.repo), runner=self._runner_cmd()
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "{runner}"
            "'".format(
                repo=_safe_path_component(self.pr.repo),
                opts=self._APPLY_OPTS,
                runner=self._runner_cmd(),
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "git apply {opts} /home/fix.patch || "
            "git apply {opts} --3way /home/fix.patch || true ; "
            "{runner}"
            "'".format(
                repo=_safe_path_component(self.pr.repo),
                opts=self._APPLY_OPTS,
                runner=self._runner_cmd(),
            )
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Current test binary; empty until cargo announces one, in which case ids
        # fall back to the bare libtest name rather than being dropped.
        label = ""

        for raw in test_log.splitlines():
            line = _RE_ANSI.sub("", raw).strip()

            m = _RE_DOCTESTS.match(line)
            if m:
                label = f"doc-tests {m.group('crate')}"
                continue

            m = _RE_RUNNING.match(line)
            if m:
                label = _binary_label(m.group("rest"))
                continue

            m = _RE_TEST_LINE.match(line)
            if not m:
                continue

            name = m.group("name").strip()
            if label.startswith("doc-tests"):
                name = _RE_DOCTEST_LINE_SUFFIX.sub("", name)
            test_id = f"{label}::{name}" if label else name
            status = m.group("status")

            if status == "ok":
                passed_tests.add(test_id)
            elif status == "FAILED":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # A test that failed anywhere is failed, even if an earlier line said ok.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ── number_interval FOR THE EMITTED DATASET ────────────────────────────────────
# The bundles in this dataset carry `prs_in_bundle` but no `number_interval`, and
# `PullRequest` has no field for the former, so the harness parses it away and the
# emitted *_dataset.jsonl record would come out with `number_interval: ""`.
#
# The value must be the EXPLICIT MEMBER LIST, dash-joined -- `303-308-309-310`,
# never a collapsed range like `303-402`. A range would claim every PR between the
# endpoints, and these bundles are sparse: bundle 303 spans 303..402 but contains
# 76 members, not 100. Range form would silently assert 24 PRs that are not in it.
#
# Two narrowly-scoped, idempotent monkeypatches (no edits to the shared harness):
#
#   1. Wrap PullRequest.from_json to STASH the raw list on the parsed PR as
#      `_prs_in_bundle`. number_interval is deliberately NOT set here:
#      Instance.create() routes on f"{org}/{number_interval}" and raises if that
#      key is unregistered, so populating it at parse time would break every
#      build for this repo (it registers as "tower-rs/tower").
#
#   2. Wrap Dataset.build -- the single output-serialization point, invoked from
#      gen_report AFTER all routing is done -- to fill the field in from that
#      stash when it is still empty. Routing never sees it; only the emitted
#      record does.
#
# An equivalent global patch already ships in the wasmtime registry, so this
# value was in fact already correct in the emitted file. It is duplicated here on
# purpose: relying on an unrelated repo's module being imported for THIS repo's
# output to be well-formed is a silent dependency, and it breaks the moment that
# file is renamed, removed, or fails to import. Both patches are idempotent and
# only ever fill an EMPTY field, so whichever lands first wins and the other is a
# no-op -- they cannot fight.
import json as _tower_json
import logging as _tower_logging

from multi_swe_bench.harness.dataset import Dataset as _TowerDataset


def _tower_number_interval_from_bundle(bundle) -> str:
    """Dash-joined explicit member list, de-duplicated, original order kept."""
    seen = set()
    members = []
    for n in bundle:
        if n not in seen:
            seen.add(n)
            members.append(str(n))
    return "-".join(members)


# Sentinels are checked against each class's OWN __dict__, not via getattr:
# Dataset subclasses PullRequest, so getattr would see the inherited from_json
# sentinel and wrongly skip patching Dataset.build.
if "_tower_from_json_patch" not in PullRequest.__dict__:
    _tower_orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _tower_pr_from_json_with_bundle(cls, json_str):
        pr = _tower_orig_pr_from_json(cls, json_str)
        try:
            if not getattr(pr, "_prs_in_bundle", None):
                bundle = (_tower_json.loads(json_str) or {}).get("prs_in_bundle")
                if bundle:
                    # Stash only -- number_interval stays "" so routing is unaffected.
                    pr._prs_in_bundle = list(bundle)
        except (ValueError, TypeError, AttributeError):
            # Malformed / absent prs_in_bundle must never break parsing: the PR
            # itself is already built, and a missing stash only means the emitted
            # record keeps whatever number_interval it already had.
            _tower_logging.getLogger(__name__).debug(
                "prs_in_bundle stash failed for %s; leaving number_interval alone",
                getattr(pr, "id", "<unknown>"),
                exc_info=True,
            )
        return pr

    PullRequest.from_json = _tower_pr_from_json_with_bundle
    PullRequest._tower_from_json_patch = True


if "_tower_build_patch" not in _TowerDataset.__dict__:
    _tower_orig_dataset_build = _TowerDataset.build.__func__

    @classmethod
    def _tower_dataset_build_with_interval(cls, pr, report):
        ds = _tower_orig_dataset_build(cls, pr, report)
        if not getattr(ds, "number_interval", ""):
            bundle = getattr(pr, "_prs_in_bundle", None)
            if bundle:
                ds.number_interval = _tower_number_interval_from_bundle(bundle)
        return ds

    _TowerDataset.build = _tower_dataset_build_with_interval
    _TowerDataset._tower_build_patch = True
