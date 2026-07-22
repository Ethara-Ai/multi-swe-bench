import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Emitted by both images so DockerfileEnhancer.enhance() early-returns
# ("if SYNTAX_DIRECTIVE in raw: return raw") and leaves the generated content
# alone. Without it the enhancer rewrites the base image's plain `git clone`
# into clone + "git checkout ${BASE_COMMIT}" + the hardening block -- which is
# wrong for a base image: the base is shared by every PR and must stay at the
# default branch, unpinned, so its dependency cache is reusable.
_SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"


def _infra(org: str, repo: str, with_base_commit: bool) -> str:
    """The non-proxy infrastructure block (ARGs, env, labels).

    Mirrors DockerfileEnhancer._infrastructure_block minus the proxy ARGs, proxy
    ENV vars, CA-cert symlinks and MITM secret mount. `with_base_commit` is False
    for the base image, which is deliberately not pinned to any commit.
    """
    github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo
    repo_url = f"https://github.com/{org}/{github_repo}.git"

    args = f'ARG TARGETARCH\nARG REPO_URL="{repo_url}"'
    if with_base_commit:
        args += "\nARG BASE_COMMIT"

    env = "ENV DEBIAN_FRONTEND=noninteractive\nENV TZ=UTC\nENV LANG=C.UTF-8"

    label = (
        f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
        f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
        f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
        f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
    )
    return "\n\n".join([args, env, label])


class FabricImageBase(Image):
    """Shared dependency layer: clones Fabric at the default branch and warms the
    Go module cache from go.mod/go.sum.

    Deliberately carries NO base commit. It is built once and reused by every PR
    image, so pinning it to one PR's SHA would make it wrong for all the others.
    The per-PR checkout happens in FabricImageDefault.
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
        return "golang:1.25-bookworm"

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

        repo = self.pr.repo

        # `git clone "${REPO_URL}"` (quoted, exactly this form) is also what
        # _standardize_repo_fetch's negative lookahead skips, so this line stays
        # a plain clone even if the enhancer ever does run over it.
        return f"""{_SYNTAX_DIRECTIVE}

FROM {image_name}

{_infra(self.pr.org, repo, with_base_commit=False)}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

# Warm the module + build cache from the default branch. Individual PRs resolve
# their own deps on top of this in prepare.sh; this only has to be a good cache
# seed, so failures here are non-fatal.
RUN go mod download || true
RUN go build ./... || true

CMD ["/bin/bash"]
"""


class FabricImageDefault(Image):
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
        # Chain onto the shared base so the apt layer, the clone and the warmed
        # Go module cache are built once and reused by all 11 PR images.
        return FabricImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha

        # Split the COPY steps around the expensive prepare.sh layer: the patches
        # and prepare.sh are build inputs, while the eval scripts are only read at
        # `docker run` time. Copying the eval scripts last means editing one of
        # them invalidates only the trailing cheap layers, not the checkout +
        # `go mod download` above it.
        build_inputs = ("fix.patch", "test.patch", "prepare.sh")
        pre_copy = "".join(
            f"COPY {f.name} /home/\n" for f in self.files() if f.name in build_inputs
        )
        post_copy = "".join(
            f"COPY {f.name} /home/\n"
            for f in self.files()
            if f.name not in build_inputs
        )

        # build_dataset.py only passes the BASE_COMMIT build-arg when
        # dependency() is a string, so in this two-stage layout the SHA is
        # interpolated here as a literal instead of read from an ARG.
        #
        # The base image carries Fabric's FULL history (it must, so any PR's SHA
        # is checkout-able). That history is inherited through FROM, so once this
        # image is pinned to its base commit the future commits -- including the
        # one holding the fix and the gold test -- have to be destroyed here, or
        # an agent could recover them with `git log` / `git show`. The enhancer
        # does not do it for us: enhance() early-returns for a chained Image
        # dependency, so the hardening is emitted explicitly below.
        return f"""{_SYNTAX_DIRECTIVE}

FROM {image.image_full_name()}

{_infra(self.pr.org, repo, with_base_commit=False)}

{pre_copy}
WORKDIR /home/{repo}

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{post_copy}
CMD ["/bin/bash"]
"""

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
                "prepare.sh",
                """#!/bin/bash
# Runs against the base image's full clone. Pins the tree to this PR's base
# commit, then resolves the deps that commit needs on top of the module cache
# the base image already warmed. The hardening RUN that strips the remaining
# history happens after this script, in the Dockerfile.
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

go mod download || true
go build ./... || true
go test -count=1 -run '^$' ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# The gold test patch MUST apply cleanly: grading is meaningless if the tests
# that decide pass/fail are silently missing, so no --reject / || true here.
git apply --whitespace=nowarn /home/test.patch

# The fix patch may carry binary blobs (Fabric ships cmd/generate_changelog/
# changelog.db) that arrive as a "Binary files ... differ" summary with an
# abbreviated index line and no payload -- git can never apply those, and they
# are build artifacts rather than code under test. Drop exactly those paths and
# apply everything else strictly, so a real source hunk failing is still fatal.
_ex=$(grep -B2 '^Binary files' /home/fix.patch 2>/dev/null \\
        | grep '^diff --git' \\
        | sed -e 's|.* b/||' -e 's|^|--exclude=|' \\
        | tr '\\n' ' ' || true)
git apply --whitespace=nowarn $_ex /home/fix.patch

go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]


@Instance.register("danielmiessler", "Fabric")
class Fabric(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FabricImageDefault(self.pr, self._config)

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
                    if test_name in failed_tests:
                        continue
                    if base_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if test_name in passed_tests:
                        continue
                    if base_name in passed_tests:
                        continue
                    if test_name not in failed_tests and base_name not in failed_tests:
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# number_interval is the hyphen-joined prs_in_bundle of a bundle -- an explicit
# membership list, NOT a range: "1802-1823" is the two PRs 1802 and 1823, and
# must never be read as the span 1802..1823 (which would wrongly pull in the 20
# PRs between them). Instance.create() routes on f"{org}/{number_interval}"
# whenever number_interval is set, so every interval a PR can carry must be
# registered to the same Fabric config (in addition to "danielmiessler/Fabric"),
# else create() raises "Instance ... is not registered" before any image is
# built. Data-derived from the 11 delivered bundles in
# danielmiessler__Fabric_lht_final.jsonl -- regenerate if the delivered set
# changes.
_BUNDLE_NIS = [
    "1759-1762",
    "1802-1823",
    "1914-1915",
    "1925-1926",
    "1947-1948-1949",
    "1950-1951-1952",
    "1964-1967",
    "1975-1978",
    "2001-2002-2003",
    "2044-2047",
    "2079-2086",
]
for _ni in _BUNDLE_NIS:
    Instance.register("danielmiessler", _ni)(Fabric)
