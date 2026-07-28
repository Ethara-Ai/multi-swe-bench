import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for
# affaan-m/everything-claude-code.
#
# Every instance is a release-BUNDLE. The raw record carries `prs_in_bundle`
# (e.g. [146, 147, 150, 155, 157]) but NO `number_interval` field at all. The
# required output format is the dash-JOINED bundle list
# ("146-147-150-155-157") — NOT a "146-157" range, which would wrongly imply
# every PR in between, most of which are not part of the bundle. (This dataset
# makes that especially stark: pr-290's bundle lists 93 PRs spanning 290..667,
# while the range 290-667 would imply 378.)
#
# Two constraints force the approach below (identical to usememos/memos,
# goadesign/goa & aquasecurity/tfsec):
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json schema
#     loader DROPS it — the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "affaan-m/146-147-150-155-157"), which is not
#     registered → instance creation fails / the row is silently skipped.
#
# So we do two import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to
# harness source):
#   1. PullRequest.from_json — re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_ecc_number_interval` (routing key stays "").
#   2. Dataset.build — stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
# The patches chain safely with the other registries' patches (each captures the
# current from_json / build, calls through, and only acts on its own org/repo).
import multi_swe_bench.harness.pull_request as _pull_request

if not getattr(_pull_request.PullRequest, "_ecc_number_interval_patched", False):
    _ecc_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _ecc_from_json(cls, json_str):
        pr = _ecc_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "affaan-m"
                and raw.get("repo") == "everything-claude-code"
                and raw.get("prs_in_bundle")
            ):
                # Stash only — do NOT set pr.number_interval (the routing key).
                pr._ecc_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_ecc_from_json)
    _pull_request.PullRequest._ecc_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag set above;
    # use a distinct flag and check the class's OWN __dict__ (not getattr, which
    # would see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_ecc_build_patched", False):
        _ecc_orig_build = _Dataset.build.__func__

        def _ecc_build(cls, pr, report):
            ds = _ecc_orig_build(cls, pr, report)
            ni = getattr(pr, "_ecc_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_ecc_build)
        _Dataset._ecc_build_patched = True
# ---------------------------------------------------------------------------

# Robust patch application.
#
# Bundled PRs in this dataset carry vendored binary assets (e.g.
# `assets/images/longform/*.png`) as HEADER-ONLY stubs: a `diff --git` block with
# `new file mode` + `index 0000000000..<sha>` + `Binary files /dev/null and b/... differ`
# and NO `GIT binary patch` payload. `git apply` is atomic, so it aborts the WHOLE
# patch on such a block ("cannot apply binary patch ... without full index line")
# and NOTHING gets applied — the gold fix silently never lands and the run reports
# a bogus verdict. Instances 72 and 548 both hit this (22 and 12 stub blocks).
#
# Two defects are repaired here:
#   1. Binary-stub blocks — dropped. They are vendored images with no payload to
#      apply and no test reads them.
#   2. The same new file added by BOTH the test patch and the fix patch (files
#      mis-split into both halves), which makes the combined apply fail with
#      "already exists". Those paths are dropped from the fix patch.
# We then apply with `--3way` as a fallback. If apply STILL fails we exit non-zero
# so the failure surfaces honestly instead of being masked by `|| true`.
#
# NOTE: only vendored binary assets / duplicate-adds are removed; every real source
# and test change applies unchanged, so the pass/fail verdict stays faithful.
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

# Plain apply FIRST — the original, index-independent behaviour, which already
# worked for every healthy bundle. Only on failure do we fall back to --3way (now
# safe thanks to the index refresh above), so we can never do worse than before.
git apply --whitespace=nowarn "$@" || git apply --3way --whitespace=nowarn "$@"
"""

# Re-sync node_modules when a patch changed package.json.
#
# prepare.sh installs dependencies at BUILD time from the PRE-patch package.json.
# Several bundles DECLARE new runtime dependencies as part of the change (pr-548's
# fix adds `"@iarna/toml": "^2.2.5"`), and the matching new test file `require()`s
# them. Without this re-sync that file dies with MODULE_NOT_FOUND and the runner
# reports `✗ scripts/codex-hooks.test.js exited with status 1` — a spurious
# PASS->FAIL "regression" that masks the genuine gold signal and can sink an
# otherwise-valid instance.
#
# Guarded on package.json actually having changed, so it is a no-op for bundles
# that add no dependency. `|| true` so a network-restricted eval host degrades to
# the previous behaviour instead of aborting the whole run.
# Unprivileged account the test suite runs under. Several tests assert real
# EACCES behaviour (`appendSessionContent returns false when file is read-only`,
# `saveAliases triggers inner restoreErr catch when both save and restore fail`)
# by chmod-ing a file to 0444 and expecting the write to be refused. Root BYPASSES
# file permission bits, so under the default root container those writes succeed
# and the tests fail in EVERY stage — persistent noise that can never become a
# fail->pass signal. Running the suite as a normal user makes them behave as the
# author intended. Measured on pr-183: 976P/2F as root -> 978P/0F as this user,
# with no test broken in exchange (verified over repeated fresh containers).
_TEST_USER = "ecc"
_TEST_UID = "1500"

_NPM_RESYNC_SH = """
if ! git diff --quiet -- package.json 2>/dev/null; then
  timeout --kill-after=30 600 npm install --no-audit --no-fund || true
fi
""".strip()

# Dependency specs ADDED by a bundle's package.json, harvested from the patches so
# they can be pre-installed at BUILD time (where the network is always available).
#
# Why not rely on the eval-time re-sync alone: the correct anti-cheat posture is to
# run eval containers with `--network none` (without it, `git fetch <explicit-url>`
# restores the full upstream history and defeats Image._HARDENING_BLOCK entirely).
# Under network isolation an eval-time `npm install` cannot work, so any dependency
# the PR introduces must already be in node_modules. Warming them here makes the
# registry compatible with network-isolated evaluation.
#
# We parse only ADDED `"name": "version"` lines that sit INSIDE a *dependencies
# block. Section tracking is required: a naive scan also matches the top-level
# `"version": "1.10.0"` field and `engines`' `"node": ">=18"`, both of which are
# real npm package names and would install junk. The value must also look like a
# semver range, which excludes npm-script entries (values like
# "node scripts/ci/catalog.js --text") and "packageManager": "yarn@4.9.2+...".
# Nothing here reveals the fix — a dependency list is not the solution.
_DEP_SECTION_RE = re.compile(r'^\s*"(?:\w+)?[Dd]ependencies"\s*:\s*\{\s*$')
_SECTION_END_RE = re.compile(r"^\s*\}\s*,?\s*$")
_DEP_LINE_RE = re.compile(
    r'^\s*"(?P<name>@?[A-Za-z0-9][\w.-]*(?:/[\w.-]+)?)"\s*:\s*'
    r'"(?P<ver>[\^~><=]*\d[\w.\-+]*|\*|latest)"\s*,?\s*$'
)


def _added_dep_specs(*patches: str) -> list[str]:
    """Return `name@version` specs for deps added to package.json by the patches."""
    specs: dict[str, str] = {}
    for patch in patches:
        if not patch:
            continue
        in_pkg_json = False
        in_deps = False
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                in_pkg_json = line.endswith("/package.json")
                in_deps = False
                continue
            if not in_pkg_json:
                continue
            if line.startswith("@@"):
                # Hunk boundary: enclosing section is unknown again.
                in_deps = False
                continue
            if line[:1] not in ("+", "-", " "):
                continue
            added, body = line[:1] == "+", line[1:]
            if line[:1] == "-":  # removals never change the section we land in
                continue
            if _DEP_SECTION_RE.match(body):
                in_deps = True
                continue
            if _SECTION_END_RE.match(body):
                in_deps = False
                continue
            if in_deps and added:
                m = _DEP_LINE_RE.match(body)
                if m:
                    specs.setdefault(m.group("name"), m.group("ver"))
    return [f"{n}@{v}" for n, v in sorted(specs.items())]


class EverythingClaudeCodeImageBase(Image):
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
        return "node:20-bookworm"

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

        # SHARED base image: built ONCE and reused by every PR in this dataset
        # (image_tag is the constant "base", so all EverythingClaudeCodeImageBase
        # instances collapse to a single built image via Image.__eq__/__hash__).
        # It clones the repo at HEAD and keeps FULL git history — it must NOT check
        # out a per-PR BASE_COMMIT. The 9 dataset instances have 9 DIFFERENT base
        # shas, and each per-PR image (EverythingClaudeCodeImageDefault) builds FROM
        # this base and checks out its own BASE_COMMIT, so the base has to retain
        # every commit any sibling PR might reference.
        #
        # The leading `# syntax` directive makes DockerfileEnhancer.enhance() return
        # this Dockerfile unchanged (its first guard is
        # `if SYNTAX_DIRECTIVE in raw: return raw`). This is deliberate: it stops the
        # pipeline enhancer's _standardize_repo_fetch from rewriting the
        # `RUN git clone ... /home/{repo}` line into a block that checks out
        # ${BASE_COMMIT} and applies Image._HARDENING_BLOCK — which would harden the
        # SHARED base down to ONE commit and break every sibling PR's
        # `git checkout {base.sha}`. Because the enhancer is bypassed, the
        # ARG/ENV/LABEL infra is inlined below. Anti-cheat hardening is applied
        # per-PR in EverythingClaudeCodeImageDefault instead.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8
ENV CI=true
LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class EverythingClaudeCodeImageDefault(Image):
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
        return EverythingClaudeCodeImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Install npm deps if a package.json exists (early PRs in this dataset
# pre-date the package.json). `|| true` so optional deps can't block us.
if [ -f package.json ]; then
  timeout --kill-after=30 600 npm install --no-audit --no-fund || true
fi

# Pre-install dependencies the bundle's package.json ADDS, so the eval container
# never needs the network (it should be run with --network none; see registry
# comments). Empty for bundles that add no dependency.
if [ -f package.json ] && [ -n "{deps}" ]; then
  timeout --kill-after=30 600 npm install --no-audit --no-fund --no-save {deps} || true
fi

# The pre-install must not leave the worktree dirty, or the hardening block's
# HEAD/ref assertions run against a mutated tree and the base `run` stage would be
# contaminated with post-fix state.
git reset --hard
bash /home/check_git_changes.sh

# Unprivileged account the suite runs under (see _TEST_USER in the registry).
id -u {user} >/dev/null 2>&1 || useradd -m -u {uid} {user}
""".format(
                    pr=self.pr,
                    deps=" ".join(
                        _added_dep_specs(self.pr.test_patch, self.pr.fix_patch)
                    ),
                    user=_TEST_USER,
                    uid=_TEST_UID,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
# Run the unit test suite. We invoke `node tests/run-all.js` DIRECTLY
# rather than `npm test` because the npm test pipeline chains validators
# that check documentation consistency, which legitimately fail on PRs
# that bump version numbers or add files without doc updates. Those
# validator failures abort the chain before any unit tests run, masking
# the actual test signal we care about.
#
# Output format from this custom runner: `  ✓ <name>` and `  ✗ <name>`.
set -e

cd /home/{pr.repo}

# Drop to an unprivileged user so permission-dependent tests behave as designed
# (root bypasses chmod; see _TEST_USER in the registry). Patches are applied as
# root beforehand, so hand ownership over first. This runs in ALL THREE stages —
# run/test-run/fix-run all funnel through this script — so the environment stays
# identical across them and the f2p/p2p diff remains apples-to-apples.
# Falls back to running as root if the user or runuser is unavailable, so this can
# never be worse than the previous behaviour.
RUN_AS=""
if [ "$(id -u)" = "0" ] && id -u {user} >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
  chown -R {user}:{user} /home/{pr.repo} 2>/dev/null || true
  RUN_AS="runuser -u {user} -- env HOME=/home/{user} CI=true"
fi

if [ -f tests/run-all.js ]; then
  timeout --kill-after=30 600 $RUN_AS node tests/run-all.js || true
else
  echo "(no tests/run-all.js at this base.sha — pre-test era)"
fi
""".format(pr=self.pr, user=_TEST_USER),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/run_tests.sh
""".format(pr=self.pr),
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
export CI=true

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch
cd /home/{pr.repo}
{npm}
bash /home/run_tests.sh
""".format(pr=self.pr, npm=_NPM_RESYNC_SH),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

bash /home/robust_apply.sh /home/{pr.repo} /home/test.patch /home/fix.patch
cd /home/{pr.repo}
{npm}
bash /home/run_tests.sh
""".format(pr=self.pr, npm=_NPM_RESYNC_SH),
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

        # Per-PR anti-cheat hardening. This is the image the model is evaluated in,
        # so after prepare.sh has checked out BASE_COMMIT and warmed the npm cache we
        # strip every ref/remote and GC unreachable objects via
        # Image._HARDENING_BLOCK: the gold fix/merge commits and the `origin` remote
        # are removed, so a solution cannot recover the fix via `git log`,
        # `git show <future-sha>`, `git diff`, or `git fetch`. BASE_COMMIT is exported
        # as ENV (this image's dependency() is an Image, so build_dataset passes NO
        # --build-arg BASE_COMMIT — only str-dependency base images get those) which
        # makes the hardening block's ${BASE_COMMIT} resolve to THIS PR's base sha.
        # The block also self-audits in-build: it asserts HEAD == BASE_COMMIT, that no
        # refs/remotes/tags survive, and that `rev-list --all` == `rev-list HEAD`, so
        # a regression here fails the build instead of silently shipping history.
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


@Instance.register("affaan-m", "everything-claude-code")
class EverythingClaudeCode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EverythingClaudeCodeImageDefault(self.pr, self._config)

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

        # Strip ANSI escapes FIRST so colored CI output still parses.
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        # Custom runner's format (verified via docker smoke test):
        #   `  ✓ <test name>`     -> pass  (U+2713)
        #   `  ✗ <test name>`     -> fail  (U+2717)
        # Also accept ASCII fallbacks for robustness:
        #   `  ok <name>` / `  PASS <name>` / `  FAIL <name>`
        re_pass = [
            re.compile(r"^\s*[✓✔]\s+(.+?)\s*$"),  # ✓ ✔
            re.compile(r"^\s*ok\s+\d*\s*-?\s*(.+?)\s*$", re.IGNORECASE),
        ]
        re_fail = [
            re.compile(r"^\s*[✗✘❌✘]\s+(.+?)\s*$"),  # ✗ ✘ ❌
            re.compile(r"^\s*not\s+ok\s+\d*\s*-?\s*(.+?)\s*$", re.IGNORECASE),
        ]
        re_skip = [
            re.compile(r"^\s*[➖–○⚠]\s+(.+?)\s*$"),  # − – ○ ⚠
            re.compile(r"^\s*skip(?:ped)?\s+(.+?)\s*$", re.IGNORECASE),
        ]

        for line in clean.splitlines():
            for rx in re_pass:
                m = rx.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    break
            else:
                for rx in re_fail:
                    m = rx.match(line)
                    if m:
                        failed_tests.add(m.group(1).strip())
                        break
                else:
                    for rx in re_skip:
                        m = rx.match(line)
                        if m:
                            skipped_tests.add(m.group(1).strip())
                            break

        # Enforce disjoint sets: passed > failed > skipped.
        failed_tests -= passed_tests
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
