"""Tekton Pipeline harness for the 2023-05 .. 2024-12 era (PRs 6673 .. 8878).

Dataset: output/tektoncd__pipeline_raw_dataset.jsonl (10 PRs).

    PR    base sha    title
    6673  cec2422542  [TEP-0091] use VerificationResult in verify
    6801  b6f84fd68e  Refactor failure logic in pipelinerun resolution
    7565  ec051b22a7  Add granular termination reason in container termination
    7666  73936e072f  Allow `imagePullBackOff` for the specified duration
    7677  c58fa318b8  [release-v0.53.x] wait for a given duration in case of ipbo
    8574  990917dffc  Add configuration for custom bundle resolver backoff
    8710  38f9595e22  Refactor sidecar validation to implement apis.Validatable
    8777  d438f79aae  Move Step ResultRef/ArtifactsRef tests to container_valid
    8873  a4b9245629  Nr. 1 - [TEP-0056]: Pipelines-in-Pipelines task resolution
    8878  d8f7b93fb9  Nr. 2 - [TEP-0056]: Update state methods for child PRuns

ONE shared base image on golang:1.24-bookworm. The later PRs in this range
(8710, 8777, 8873, 8878) declare `go 1.23` / `go 1.24` in go.mod, so a 1.22
base fails immediately with `go: go.mod requires go >= 1.23` and no tests
are captured. 1.24 is the conservative upper bound that satisfies every PR
here; combined with GOTOOLCHAIN=auto (set in the base Dockerfile below), any
fix patch that bumps go.mod beyond 1.24 will auto-fetch the required
toolchain. Older PRs still build cleanly against 1.24 via `-mod=vendor`.

Layout follows the same rules as pipeline_4659_to_1888.py:

    base Dockerfile  toolchain, proxy/CA, ENV, LABELs, WORKDIR /home/, git clone,
                     CMD ["/bin/bash"].  NOTHING after the clone -- no checkout,
                     no pin, no gc, no scrub, no asserts.  Full history is kept
                     on purpose, so one shared base serves every PR in the era.
    PR Dockerfile    FROM <base>, WORKDIR /home/<repo>, RUN git reset --hard,
                     RUN git checkout <sha>, the COPY lines,
                     ARG BASE_COMMIT="<sha>", the FULL hardening block,
                     RUN bash /home/prepare.sh.
    prepare.sh       check_git_changes + build/cache warm-up. No checkout,
                     no stripping, no hardening -- all of that lives in the
                     PR Dockerfile now.

The base declares a string dependency(), so DockerfileEnhancer would normally
rewrite its `git clone` into a pinned checkout plus the hardening block. Emitting
the BuildKit syntax directive makes DockerfileEnhancer.enhance() return the
Dockerfile verbatim, so the infrastructure block is assembled here from the
enhancer's own constants -- byte-identical to what every other image gets while
leaving the clone unpinned.
"""

import json as _json
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.24-bookworm"
_ERA = "6673_to_8878"
_BASE_TAG = f"base-{_ERA}"

# Stripped off the `Package` field of `go test -json` events so a test id reads
# `go::pkg/pod::TestOrderContainers` rather than the full module path.
_REPO_PREFIX = "github.com/tektoncd/pipeline/"

# Wall-clock races or otherwise flaky tests observed to rotate between
# PASS/FAIL across stages on the same PR. Populate empirically after first
# multi-arch run; each entry is "<pkg>::<TestName>" and covers subtests too.
_FLAKY_TESTS: frozenset[str] = frozenset()


def _is_flaky(pkg: str, test: str) -> bool:
    # Subtests arrive as "Parent/child"; the entry names the parent only.
    return f"{pkg}::{test.split('/', 1)[0]}" in _FLAKY_TESTS


# ONE constant, used by run.sh / test-run.sh / fix-run.sh, so the three stages
# can never drift apart.
#
#   -json      machine-readable. Scraping `--- PASS:` lines loses the package,
#              and tekton has colliding leaf test names across packages.
#   -mod=vendor the tree is vendored at every sha in this era; this keeps the
#              build fully offline and off proxy.golang.org.
#   -timeout   the per-package default is 10m; taskrun_test.go is the slow one.
#   -p 4       cap how many package binaries run at once. Every tekton
#              reconciler package stands up its own fake Kubernetes clients and
#              informers, and under memory pressure the wall-clock assertions
#              start missing deadlines non-deterministically. Capping
#              concurrency attacks the cause instead of the symptom.
#   2>&1       compile errors arrive on stderr as plain text. Keeping them in
#              the log makes a broken stage visible; parse_log skips any line
#              that does not start with `{`.
#   || true    a failing package must not abort the script -- the FAIL events
#              are the f2p signal and still have to reach parse_log.
_TEST_CMD = "go test -json -count=1 -timeout=20m -p 4 -mod=vendor ./... 2>&1 || true"

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

# --really-refresh re-reads every file instead of trusting the stat cache. A
# plain `git status --porcelain` can report a clean tree purely because the
# cached stat data still matches, which is how a dirty worktree ships.
git update-index -q --really-refresh || true

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        enh = DockerfileEnhancer

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

ENV GOTOOLCHAIN=auto

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

{code}

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

    def dependency(self) -> Image | None:
        return _ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh

# Warm the Go build cache so the three test stages do not each pay for a full
# compile of the tree. Vendored, so no network is touched.
go build -mod=vendor ./... || true
go test -run ZZZ_NO_SUCH_TEST -count=1 -mod=vendor ./... || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
{test_cmd}
""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # This layer has an Image dependency(), so build_dataset.py passes it NO
        # build args (build_dataset.py:625-629 restricts REPO_URL/BASE_COMMIT to
        # string-dependency images). The hardening block below dereferences
        # ${BASE_COMMIT}, so the sha has to be baked in here as an ARG default.
        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

{Image._HARDENING_BLOCK}
RUN bash /home/prepare.sh

{self.clear_env}
"""


def _parse_go_test_log(test_log: str) -> TestResult:
    """Parse `go test -json` output into `go::<pkg path>::<TestName>` ids.

    The tool name leads the id on purpose. report.py's matchers take
    `test_name.split("::", 1)[0]` and compare it against the patch's file paths;
    a leading `go` can never collide with a real file, so a fix patch that
    creates a file cannot mis-flag a test as fix-authored.

    Subtests keep their `Parent/child` shape, which is what
    `_candidate_identifiers` expects for Go.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in test_log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = _json.loads(raw)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue

        test = ev.get("Test")
        action = ev.get("Action")
        if not test or action not in ("pass", "fail", "skip"):
            continue

        pkg = ev.get("Package") or ""
        if pkg.startswith(_REPO_PREFIX):
            pkg = pkg[len(_REPO_PREFIX) :]
        elif pkg == _REPO_PREFIX.rstrip("/"):
            pkg = "."

        if _is_flaky(pkg, test):
            continue

        name = f"go::{pkg}::{test}"

        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # TestResult requires the three sets to be disjoint. A parent test whose
    # subtest failed emits `fail` for the parent too, so fail always wins.
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


class _PipelineInstanceBase(Instance):
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


@Instance.register("tektoncd", "pipeline")
@Instance.register("tektoncd", f"pipeline_{_ERA}")
class Pipeline6673To8878(_PipelineInstanceBase):
    pass
