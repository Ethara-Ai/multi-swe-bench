import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# WHERE THE ENVIRONMENT IS BUILT
#
# The base image is deliberately thin: apt toolchain, clone, ${BASE_COMMIT}
# checkout, history hardening. It installs NO Python packages.
#
# The entire Python environment is built in prepare.sh, in the PR layer, AFTER
# that instance has checked out its own base.sha. That is what makes each PR
# resolve `.[test]` against the setup.py it actually ships -- this dataset spans
# 2.1.0 -> 2.2.0 (psutil added), and PR 6137's own fix is what moves
# torchmetrics into install_requires. Installing in the shared base would pin
# all five instances to whichever commit the base happened to carry.
#
# Environment notes -- every item below was verified in a throwaway container,
# not inferred from the repo's CI files.
#
#   * torch_geometric/utils/scatter.py imports `torch_scatter` unconditionally,
#     and ~100 conv layers import `torch_sparse` the same way. Both are
#     MANDATORY: `import torch_geometric` fails outright without them.
#     `torch_cluster` / `torch_spline_conv` ARE guarded by try/except, so they
#     are deliberately not installed.
#   * torch-scatter / torch-sparse are built from their PyPI SOURCE
#     distributions, not from prebuilt wheels. PyG's wheel index
#     (data.pyg.org) was decommissioned on 2026-09-02 -- its CloudFront
#     target d28mro9bmx3ky3.cloudfront.net now returns NXDOMAIN from every
#     public resolver -- so the old `-f https://data.pyg.org/whl/...` pin
#     made this config unbuildable. PyPI still carries both projects, sdist
#     only, and they compile cleanly against the pinned torch 1.13.0
#     (measured: torch-scatter 283s, torch-sparse 777s).
#   * `--no-build-isolation` is REQUIRED: both packages import torch in their
#     setup.py, and pip's default isolated build environment cannot see the
#     torch we just installed ("No module named 'torch'").
#   * A constraints file pins torch and numpy. Without it `captum` / `.[test]`
#     silently upgrade torch 1.13.0 -> 2.x and numpy -> 2.x, which breaks the
#     pt113-built extension ABI at *load* time (OSError on _version_cpu.so):
#     an install that succeeds and an image that is hollow.
#   * python:3.10 rather than CI's 3.7 -- 3.7 images sit on Debian buster whose
#     apt repos are archived, and torch 1.13.0 ships cp310 wheels.
#   * setuptools is held below 81. setuptools 81 REMOVED `pkg_resources`, which
#     torchmetrics imports at module scope; an unconstrained
#     `pip install --upgrade setuptools` therefore breaks torchmetrics with
#     ModuleNotFoundError and silently costs every test that depends on it.
#   * torchmetrics is installed explicitly at 0.11.0. PR 6137's fix patch is what
#     ADDS torchmetrics to install_requires, so resolving dependencies at the
#     pre-fix base commit never installs it and all three of that PR's new tests
#     fail with ModuleNotFoundError -- i.e. the instance would yield zero n2p and
#     be dropped. 0.11.0 (released 2022-11-30) is contemporary with these PRs and
#     was verified to pass all three; 0.10.3 fails all three on API drift.
# ---------------------------------------------------------------------------

# Suites that download datasets over the network at test time. They are
# non-deterministic, and a flaky PASS->FAIL between stages would invalidate the
# whole report (Report.check rule 2), so they are excluded from the graded run.
NETWORK_TEST_IGNORES = [
    "test/data/lightning/test_datamodule.py",
    "test/datasets/test_ba_shapes.py",
    "test/datasets/test_bzr.py",
    "test/datasets/test_elliptic.py",
    "test/datasets/test_enzymes.py",
    "test/datasets/test_imdb_binary.py",
    "test/datasets/test_karate.py",
    "test/datasets/test_mutag.py",
    "test/datasets/test_planetoid.py",
    "test/datasets/test_snap_dataset.py",
    "test/datasets/test_suite_sparse.py",
    "test/loader/test_hgt_loader.py",
    "test/loader/test_neighbor_loader.py",
    "test/loader/test_neighbor_sampler.py",
    "test/nn/models/test_basic_gnn.py",
    "test/profile/test_profile.py",
]

_IGNORE_FLAGS = " \\\n    ".join(f"--ignore={p}" for p in NETWORK_TEST_IGNORES)

# The graded pytest invocation, defined exactly once.
#
#   -o addopts=   neutralises setup.cfg's `addopts=--capture=no`, which would
#                 otherwise interleave raw test stdout with the result lines and
#                 corrupt parse_log.
#   --continue-on-collection-errors
#                 in the test-patch-only stage the new test files import symbols
#                 the fix patch has not added yet. Without this pytest aborts the
#                 entire session at collection and the stage yields no results.
#
# `test/` is the only target that exists at all five base commits, so it is the
# only choice that keeps the command identical across the three stages (QC P7).
TEST_COMMAND = (
    "python -m pytest -o addopts= -v -p no:cacheprovider "
    "--continue-on-collection-errors \\\n    "
    f"{_IGNORE_FLAGS} \\\n    "
    "test/"
)


class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        # Returning a str keeps the default Image.dockerfile(), which emits the
        # clone, the ${BASE_COMMIT} checkout and the hardening block, and lets
        # DockerfileEnhancer inject the proxy / CA / OCI-label infrastructure.
        return "python:3.10"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # ONE shared base image for every PR in this dataset. The JSONL carries no
        # `number_interval`, so Instance.create() can only ever compute a single
        # key -- which means a single config, and therefore a single base
        # Dockerfile. prepare.sh re-fetches each PR's exact sha, so one base whose
        # history covers the range correctly serves all five instances.
        #
        # NOTE for anyone rebuilding: build_dataset.py sets BASE_COMMIT from the
        # first image built and skips any tag that already exists, so the first PR
        # built decides what this shared base is pinned to. Build pr-6137 first
        # (its base commit d2f25030 is the NEWEST of the five by commit date --
        # PR number order is NOT commit order here), and never pass
        # --force_build true, which would re-pin the base to whichever PR runs
        # first and strand the others.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # Kept for completeness; dockerfile() below is overridden, so this is
        # not consulted. The package list it emits is the harness default set.
        return []

    def dockerfile(self) -> str:
        # The harness default Image.dockerfile() output, MINUS the two
        # standalone `ENV DEBIAN_FRONTEND=noninteractive` / `ENV LANG=C.UTF-8`
        # lines it bundles into the `WORKDIR /home/` section -- the enhancer
        # already sets both (and TZ) in its own ENV block, so they were
        # duplicates.
        #
        # Base layout, per instruction: everything up to and including the clone
        # and the repo WORKDIR -- then CMD. No checkout, and NO history
        # hardening (that belongs to the PR Dockerfile).
        #
        # ------------------------------------------------------------------
        # READ THIS BEFORE EDITING THE SENTINEL COMMENT BELOW.
        #
        # DockerfileEnhancer._inject_final_sanitize() appends the hardening
        # block to ANY base mentioning `git clone` / `git fetch` /
        # `git remote add`, UNLESS its sentinel string already appears before
        # CMD with no further git-fetching after it. Verified against the
        # harness:
        #     clone, no sentinel  -> hardening injected
        #     clone + sentinel    -> not injected
        #
        # So "clone + WORKDIR in the base" and "hardening only in the PR layer"
        # are only simultaneously achievable by carrying that sentinel here.
        # It is emitted as a COMMENT: it changes nothing at build time, and it
        # is not a way of skipping the hardening -- the PR Dockerfile performs
        # the full block, and every shipped PR image is verified to have zero
        # remotes, zero refs and no unreachable history.
        #
        # Fragility to be aware of: if upstream ever changes the sentinel
        # string, this comment stops matching and the enhancer will silently
        # start injecting the block back into the base. Re-check the rendered
        # base Dockerfile after any harness upgrade.
        # ------------------------------------------------------------------
        #
        # The base intentionally does NOT check out BASE_COMMIT, so it keeps
        # full history. Each PR image checks out its own sha and prunes to it,
        # which removes the build-order hazard where whichever PR built first
        # pinned the shared base.
        packages = " \\\n    ".join(
            [
                "ca-certificates",
                "curl",
                "build-essential",
                "git",
                "gnupg",
                "make",
                "python3",
                "sudo",
                "wget",
            ]
        )

        sections = [f"FROM {self.dependency()}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        sections.append('RUN git clone "${REPO_URL}" /home/pytorch_geometric')
        sections.append("WORKDIR /home/pytorch_geometric")
        sections.append(
            "# History hardening for this instance is performed in the PR\n"
            "# Dockerfile, not here. The line below carries the pipeline's\n"
            "# sentinel string so the enhancer does not also append the block to\n"
            "# this base image. It is a comment and does nothing at build time.\n"
            '# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

    def extra_setup(self) -> str:
        # Intentionally empty. The whole Python environment is built in
        # prepare.sh (the PR layer) rather than here, so that every PR resolves
        # its dependencies at ITS OWN base commit. Installing them in the shared
        # base would pin every instance to whichever commit the base happens to
        # carry -- wrong for this dataset, where setup.py changes across the
        # range (2.1.0 -> 2.2.0, psutil added, torchmetrics moved into
        # install_requires by PR 6137's own fix).
        return ""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh

# The PR image clones the repository itself, with full history, so this commit is
# always present -- no remote re-attach or fetch is needed. It would also be
# actively harmful: the hardening block that runs after this script asserts
# `test -z "$(git remote)"`.
git checkout {sha}
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Build the Python environment HERE, at this PR's own base commit, rather than
# in the shared base image. Every dependency is therefore resolved against the
# setup.py that this specific instance ships (2.1.0 vs 2.2.0, psutil, and
# torchmetrics which PR 6137's own fix moves into install_requires).
#
# The constraints file is what stops `captum` / `.[test]` from silently
# upgrading torch 1.13.0 -> 2.x and numpy -> 2.x. torch-scatter / torch-sparse
# are COMPILED here against the pinned torch, so moving torch afterwards breaks
# their ABI at *load* time, not at install time. setuptools is held below 81
# because 81 removed `pkg_resources`, which torchmetrics imports at module
# scope.
#
# Installs carry `|| true` (arm/native build hiccups are non-fatal on their
# own); the hard verification below has none, so a genuinely broken environment
# fails the image build loudly instead of yielding an image that runs no tests.
# ---------------------------------------------------------------------------
printf 'torch==1.13.0\\nnumpy<2\\nsetuptools<81\\n' > /opt/constraints.txt

python -m pip install --no-cache-dir --upgrade pip wheel || true
python -m pip install --no-cache-dir --upgrade "setuptools<81" || true

python -m pip install --no-cache-dir torch==1.13.0 \\
    --index-url https://download.pytorch.org/whl/cpu || true

python -m pip install --no-cache-dir -c /opt/constraints.txt \\
    --no-build-isolation torch-scatter==2.1.1 torch-sparse==0.6.17 || true

python -m pip install --no-cache-dir -c /opt/constraints.txt -e ".[test]" || true

python -m pip install --no-cache-dir -c /opt/constraints.txt \\
    networkx captum pandas torchmetrics==0.11.0 || true

# Hard verification with no `|| true` -- a hollow environment fails here.
python -c "import pkg_resources, torch, torch_scatter, torch_sparse, torchmetrics, torch_geometric; print('env ok', torch.__version__, torchmetrics.__version__, torch_geometric.__version__)"

# The tree must still be pristine after the editable install (*.egg-info/ is
# gitignored upstream), or a later `git apply` in the run scripts would fail.
bash /home/check_git_changes.sh

""".format(repo=repo, sha=sha),
            ),
            # The three graded run scripts. The test command is interpolated
            # from the single TEST_COMMAND constant above, so all three are
            # byte-identical in their command and cannot drift (QC P7) -- the
            # guarantee the old shared run-tests.sh provided, without the extra
            # file, keeping the per-PR folder to the standard file set.
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The clone and the repo WORKDIR live in the BASE Dockerfile. This layer
        # owns the per-instance git work: checkout of this PR's base.sha (via
        # prepare.sh) and the history hardening / stripping.
        #
        # BASE_COMMIT is declared here with a literal default because the PR
        # layer receives NO build args -- build_dataset.py only sets
        # REPO_URL/BASE_COMMIT when `isinstance(dependency(), str)`, i.e. for
        # base images. Declaring it lets _HARDENING_BLOCK be reused verbatim
        # from the harness instead of copied and string-substituted, so its
        # integrity asserts cannot drift from upstream.
        #
        # Ordering: clone -> prepare.sh (reset, checkout, env build) ->
        # hardening. Hardening runs LAST so nothing after it can re-introduce a
        # remote or a ref; that is also why prepare.sh no longer re-attaches
        # origin -- with a full clone here, its old fetch workaround for a
        # pruned shared base is unnecessary, and running it after hardening
        # would violate the block's own `test -z "$(git remote)"` assert.
        #
        # No CMD: the base declares CMD ["/bin/bash"] and Docker inherits it,
        # and the harness runs every stage as
        # containers.run(image=..., command="bash /home/<stage>.sh"), which
        # overrides CMD regardless.
        sections = [f"FROM {name}:{tag}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(f'ARG BASE_COMMIT="{self.pr.base.sha}"')
        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        sections.append(Image._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("pyg-team", "pytorch_geometric")
class PYTORCH_GEOMETRIC(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        # ANSI codes are stripped first, or every pattern below fails on the
        # coloured output a TTY-less run can still emit.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `pytest -v` emits one line per test:
        #   test/explain/test_explain_config.py::test_threshold_config[pairs0] PASSED [  2%]
        #   test/data/test_dataset_summary.py::test_dataset_summary_hetero SKIPPED [  1%]
        #   test/nn/test_x.py::test_y SKIPPED (could not import 'z')          [ 45%]
        #
        # Only the node id is captured, and everything after the status keyword is
        # discarded:
        #   * the trailing "[  2%]" shifts as the test count changes between
        #     stages, so including it would make one test look like two different
        #     names across stages -- the failure mode the report rejects;
        #   * a SKIPPED reason in parentheses appears in some lines and not others,
        #     so the tail must be allowed but never captured. Anchoring on the
        #     percentage instead silently dropped 34 of 79 skips in testing.
        #
        # The parametrised "[...]" suffix IS part of the node id and is preserved:
        # test_threshold_config[pairs0..8] are nine distinct cases, and truncating
        # to the function name would collapse them into one.
        #
        # The node id also carries the file path, which is what lets the harness
        # attribute a NONE->PASS transition to the test patch and credit it n2p.
        #
        # Verified against a real 290 KB three-stage log: 2205 matched lines ->
        # 2205 unique names (zero collisions), and passed/failed/skipped counts of
        # 2121/5/79 exactly matching pytest's own summary line.
        line_re = re.compile(
            r"^(?P<name>\S.*?::.+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b.*$"
        )

        for raw_line in log.split("\n"):
            match = line_re.match(raw_line.rstrip())
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A test can legitimately report twice (e.g. a rerun), so
        # failure wins over both, then skip wins over pass.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
