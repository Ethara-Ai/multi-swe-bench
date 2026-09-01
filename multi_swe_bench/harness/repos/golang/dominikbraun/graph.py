import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GraphImageBase(Image):
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
        # go.mod at every base commit in this dataset (a6d22040 .. e6712213)
        # declares `module github.com/dominikbraun/graph` / `go 1.18` with no
        # require block, so the module is dependency-free and one toolchain
        # serves PRs 22..127 (2022-07 .. 2023-05). Pinned to the era rather
        # than golang:latest because the package uses generics and nothing
        # newer is needed.
        return "golang:1.20"

    def image_tag(self) -> str:
        # ONE base image shared by all five PRs. Images are deduplicated by
        # image_full_name(), so this is built exactly once and its
        # ${BASE_COMMIT} is whichever PR reached the builder first - which is
        # non-deterministic and, on its own, unsafe.
        #
        # It is made safe by prepare.sh, which re-pins to this PR's own sha AND
        # re-runs the full history scrub afterwards. Both halves are required.
        # An earlier revision kept the shared base but skipped the re-scrub, and
        # the result was a dataset-integrity failure: with the base baked at
        # e6712213 (PR 127's base, 2023-05-18), prepare.sh walked HEAD backwards
        # to each older commit without pruning, leaving every commit in between
        # in the pack as an unreachable but fully readable object. Measured
        # inside pr-22: `git show 3ebaa195` printed PR 22's own merge commit,
        # "Add edge properties and support for edge attributes (#22)", diffstat
        # and all - the exact gold patch the instance exists to test. All four
        # integrity asserts still passed, because they only measure what is
        # REACHABLE. Four of the five instances shipped their own answer.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Written out in full, including the syntax directive, so that
        # DockerfileEnhancer.enhance() returns this text verbatim
        # (image.py:317) instead of synthesising the infrastructure itself.
        # Every line below is owned by this config.
        #
        # There is deliberately NO `ENV BASE_COMMIT=...` here. An earlier
        # revision hardcoded it to e6712213 so that one shared base could serve
        # all five PRs; because ENV wins over ARG, that silently discarded the
        # `--build-arg BASE_COMMIT=<pr.base.sha>` the harness passes
        # (build_dataset.py:629) and pinned every instance to PR 127's commit.
        # See image_tag() for the leak that produced. ${BASE_COMMIT} below now
        # resolves to the ARG on line 3, i.e. this PR's own base sha.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt




WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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


CMD ["/bin/bash"]
"""


class GraphImageDefault(Image):
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
        return GraphImageBase(self.pr, self.config)

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

# Re-pin to THIS PR's base commit.
#
# The base image is shared by all five PRs, so it is built once and its
# ${{BASE_COMMIT}} is whichever PR reached the builder first. Its scrub then
# deleted every ref and ran `gc --prune=now`, so depending on which PR won that
# race the commit needed here may have been pruned out of the image entirely.
# Verify it is present COMPLETE (`^{{tree}}` resolves only if the tree object is
# really there, which a bare `cat-file -e <sha>` would not prove) and clone
# fresh otherwise. This runs at BUILD time, where the network is up (R16).
#
# `cd /home` first is load-bearing: the base image ends on `WORKDIR /home/graph`,
# so this script starts INSIDE the directory the rm below deletes. Removing the
# shell's own cwd makes every later git call abort with
# "fatal: Unable to read current working directory".
cd /home
if ! git -C /home/{pr.repo} rev-parse -q --verify "{pr.base.sha}^{{tree}}" > /dev/null 2>&1; then
    rm -rf /home/{pr.repo}
    git clone --quiet https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}
fi
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Re-run the history scrub AT THIS PR'S COMMIT. This is the half that makes a
# shared base safe, and omitting it is a dataset-integrity failure rather than
# an untidiness: the base's own scrub ran at a different commit, so every commit
# between the two survives in the pack as an unreachable but readable object.
# `git log` hides them; `git show <sha>` and `git cat-file --batch-all-objects`
# hand over the finished fix. Measured before this block existed, inside pr-22:
#     rev-list HEAD                =  79 commits   <- all four asserts PASSED
#     cat-file --batch-all-objects = 219 commits
#     git show 3ebaa195            -> "Add edge properties and support for edge
#                                      attributes (#22)" - PR 22's own gold patch
# Same sequence the harness's enhancer uses, ending in the same four assertions,
# so `pr-{pr.number}` finishes in exactly the state a per-PR pinned base would
# have produced.
git checkout --detach {pr.base.sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
bash /home/check_git_changes.sh

go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

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
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("dominikbraun", "graph")
class Graph(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GraphImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # `go test -v` marks every individual result with a `--- <STATUS>: <name>`
        # line, indented for subtests:
        #     --- PASS: TestDirected_AddEdge (0.00s)
        #         --- FAIL: TestDirected_AddEdge/edge_already_exists (0.00s)
        #     --- SKIP: TestUndirected_Clone (0.00s)
        # The leading `---` is what makes a line a per-test result, so all three
        # patterns anchor on it after stripping.
        #
        # A bare `FAIL:?\s?(.+?)\s` pattern is deliberately NOT used. Go closes
        # every failing package with a summary line
        #     FAIL	github.com/dominikbraun/graph	0.030s
        # which that pattern matches, capturing the PACKAGE PATH as if it were a
        # test. That violates section 5.1 rule 5 ("never turn a summary line into
        # a test name"), inflates failed_count, and produces a name that
        # report._test_name_matches_files can never resolve to a file (R20).
        # Nothing is lost by dropping it: a package that fails to compile emits
        # no `--- FAIL:` lines at all, so its tests are absent from that stage
        # and grade NONE -> PASS (n2p), which Report.check() accepts and
        # section 14.4 documents as normal for compiled languages.
        re_pass_tests = [re.compile(r"^--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"^--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"^--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            # Subtests are kept whole (`TestX/case`, not `TestX`). Collapsing
            # them would merge distinct subtest identities into their parents and
            # discard results; keeping them also guarantees a passing and a
            # failing sibling can never land on one name.
            return test_name

        for line in clean_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    passed_tests.add(get_base_name(pass_match.group(1)))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    failed_tests.add(get_base_name(fail_match.group(1)))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    # Recorded unconditionally. The previous revision only added
                    # a name here when it was ALREADY in failed_tests, and the
                    # `skipped_tests -= failed_tests` line below then removed it
                    # again - so skipped_tests could never be non-empty and every
                    # `--- SKIP:` result was silently dropped.
                    skipped_tests.add(get_base_name(skip_match.group(1)))

        # TestResult.__post_init__ (harness/test_result.py:56-101) requires the
        # three sets to be pairwise disjoint and every *_count to equal
        # len(*_tests). Failure wins, then skip yields to pass.
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
