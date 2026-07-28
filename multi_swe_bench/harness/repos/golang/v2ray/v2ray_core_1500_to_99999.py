"""v2ray-core harness for the go-modules era (PRs 1500+).

These bases carry a working go.mod, so the module graph is taken from the tree
as-is -- no pinned manifest, no ext stub, unlike v2ray_core_0_to_1499.py.

Test command: go test -v -count=1 -timeout 15m -skip <hanging> -tags json ./...
"""

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

# The number_interval / Dataset.build monkeypatches that make these records
# routable live in v2ray_core_0_to_1499.py -- __init__.py imports that module
# first and its org filter covers both eras. Importing the constants from there
# also keeps patch-application policy identical across the two eras, so the
# reward buckets are not skewed by an era-dependent flag.
from multi_swe_bench.harness.repos.golang.v2ray.v2ray_core_0_to_1499 import (  # noqa: E402
    GO_TEST_SKIP,
    PATCH_EXCLUDES,
    V2RAY_CORE_0_TO_1499,
)


class V2rayCore1500To99999ImageBase(Image):
    """Toolchain + full-history checkout, shared by every PR in this era.

    ``image_tag()`` is the constant ``"base"``, so ONE image serves all 6 PRs in
    this era while the records carry 6 different ``base.sha`` values. That is why
    this Dockerfile declares its own ``# syntax`` directive: it makes
    ``DockerfileEnhancer.enhance()`` return the content verbatim, which is the
    only way to stop the enhancer's ``_standardize_repo_fetch`` from rewriting
    the clone below into ``git clone`` + ``git checkout ${BASE_COMMIT}`` +
    ``Image._HARDENING_BLOCK``.

    That rewrite is what the harness now does by default, and on a shared base
    tag it is fatal: the hardening block detaches at ``${BASE_COMMIT}``, deletes
    every ref and ``gc --prune``s the repository down to a single commit's
    history. Since the pipeline only builds this tag ONCE (images are deduped by
    full name, and BASE_COMMIT is whichever PR was scheduled first), the other 5
    PRs would then fail ``git checkout <their sha>`` with "reference is not a
    tree" -- and could not recover, because the same block removes ``origin``.

    So the base keeps FULL history (every era member's base.sha stays reachable)
    and takes only the hardening that is safe to share: the network remote is
    dropped, so no later layer -- and no agent -- can re-fetch upstream history.
    The strict per-PR hardening runs one tier up, in
    V2rayCore1500To99999ImageDefault, where pinning to a single base.sha is
    correct.

    Opting out of the enhancer means the ARG/ENV/LABEL block it would have
    injected is no longer free, so the parts still wanted are spelled out inline.
    """

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
        return "golang:1.22-bookworm"

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

        # Validated before interpolation into the clone URL / WORKDIR paths.
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)

        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{fetch}

# Drop the network remote from the shared base. Full history is deliberately
# retained here (see the class docstring); the per-PR image prunes it.
WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class V2rayCore1500To99999ImageDefault(Image):
    """Per-PR grading image -- this is the tier that carries the hardening.

    ``Image._HARDENING_BLOCK`` runs BEFORE ``prepare.sh`` so that it sees a
    pristine worktree (prepare.sh's ``go mod download`` can touch go.sum), and
    so prepare.sh operates on an already-pinned, already-pruned checkout. After
    it, the PR's own fix commit -- and everything merged after it -- is no longer
    readable out of git inside the image.
    """

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
        return V2rayCore1500To99999ImageBase(self.pr, self.config)

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
set -eo pipefail

cd /home/{pr.repo}

# HEAD is already detached at {pr.base.sha} and the history has already been
# pruned to it by the hardening block in the Dockerfile, which runs before this
# script. This is a cheap re-assertion of that pin; there is no `git fetch
# origin` fallback because the base image has no remote (verified: all 22 base
# SHAs in this dataset -- including 2208, 2313, 2679 and 2725, which the old
# comment flagged as off-branch -- are reachable from a plain clone, so the
# fallback was dead code even before the remote was dropped).
git reset --hard
bash /home/check_git_changes.sh

go mod download || true
go test -v -count=1 -timeout 15m {skip} -tags json ./... || true

""".format(pr=self.pr, skip=GO_TEST_SKIP),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
go test -v -count=1 -timeout 15m {skip} -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# STRICT: no `|| true`, no --reject, no .rej deletion. A test patch that does
# not apply must fail this stage loudly rather than silently grade the unpatched
# tree. Verified: all 22 test patches in this dataset apply cleanly at their
# base sha with these excludes.
git apply --whitespace=nowarn {excludes} /home/test.patch

go test -v -count=1 -timeout 15m {skip} -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP, excludes=PATCH_EXCLUDES),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# STRICT, same rationale as test-run.sh. Verified: all 22 fix patches apply
# cleanly on top of their test patch with these excludes.
git apply --whitespace=nowarn {excludes} /home/test.patch
git apply --whitespace=nowarn {excludes} /home/fix.patch

go test -v -count=1 -timeout 15m {skip} -tags json ./...

""".format(pr=self.pr, skip=GO_TEST_SKIP, excludes=PATCH_EXCLUDES),
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

        prepare_commands = "RUN bash /home/prepare.sh"

        # This image's dependency() is an Image, so DockerfileEnhancer returns
        # the content verbatim and injects nothing -- the hardening has to be
        # emitted here explicitly. ${BASE_COMMIT} is substituted with the literal
        # sha because the pipeline only passes REPO_URL/BASE_COMMIT build args to
        # string-dependency (base) images. Concatenating the block through
        # .replace rather than an f-string keeps its %(refname) tokens literal.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

{hardening}

WORKDIR /home/

{prepare_commands}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("v2ray", "v2ray-core_1500_to_99999")
class V2RAY_CORE_1500_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return V2rayCore1500To99999ImageDefault(self.pr, self._config)

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
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# instance.py routes on f"{org}/{number_interval}" whenever number_interval is
# set, so every dash-joined bundle value a record in THIS era can carry must
# resolve to a class. See the matching table in v2ray_core_0_to_1499.py.
#
# Explicit dash-joined member lists, never ranges -- the bundles are sparse.
# These are the 6 bundles whose base commit carries a go.mod.
_BUNDLE_NIS_V2RAY_MODULE = [
    "2002-2005-2037-2038-2039-2041-2043-2055-2056-2059-2086-2094",  # pr-2002 (12 PRs)
    "2091-2109-2154-2157-2161-2170-2176-2212-2228-2246-2272-2281-2287-2300-2305-2327-2338-2341-2350",  # pr-2091 (19 PRs)
    "2208-2416-2437-2543-2577-2606-2625",  # pr-2208 (7 PRs)
    "2313-2319-2322-2365-2366-2368",  # pr-2313 (6 PRs)
    "2679-2682-2714",  # pr-2679 (3 PRs)
    "2725-2728-2730-2740",  # pr-2725 (4 PRs)
]

for _ni in _BUNDLE_NIS_V2RAY_MODULE:
    Instance.register("v2ray", _ni)(V2RAY_CORE_1500_TO_99999)


# === bare-key fallback: "v2ray/v2ray-core" ===
# Instance.create falls back to f"{org}/{repo}" whenever BOTH number_interval and
# tag are empty. Registries with a single era (restic, 3x-ui) just register their
# repo name; v2ray-core cannot, because the era split means one bare key would be
# ambiguous. Registering a dispatcher instead resolves that by PR number, which IS
# the era rule.
#
# This is not a belt-and-braces nicety -- gen_report needs it. Its
# collect_report_tasks() reads number_interval out of self._dataset /
# self._raw_dataset, but in the build_dataset-driven path it runs BEFORE either is
# loaded ("Collecting report tasks..." precedes "Loading raw dataset..." in the
# log), so hasattr() is False and every ReportTask is built with
# number_interval="". Without this key, images build and instances run fine and
# then EVERY report fails with "Instance 'v2ray/v2ray-core' is not registered" --
# i.e. a fully successful run scores nothing.
#
# Registered here rather than in the 0_to_1499 file because both era classes must
# already exist, and __init__.py imports this module second.
def _v2ray_core_dispatch(pr: PullRequest, config: Config, *args, **kwargs):
    """Route a v2ray-core record to its era class by PR number.

    Mirrors the split between the two registry modules: bases below 1500 predate
    go modules and need the pinned manifest + ext stub, 1500+ carry a usable
    go.mod. Used whenever the routing key degrades to the bare repo name.
    """
    if pr.number < 1500:
        return V2RAY_CORE_0_TO_1499(pr, config, *args, **kwargs)
    return V2RAY_CORE_1500_TO_99999(pr, config, *args, **kwargs)


Instance.register("v2ray", "v2ray-core")(_v2ray_core_dispatch)
