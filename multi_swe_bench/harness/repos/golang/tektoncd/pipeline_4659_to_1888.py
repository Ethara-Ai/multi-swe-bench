"""Tekton Pipeline harness for the 2020-01 .. 2022-03 era (PRs 1888 .. 4659).

Dataset: output-tektoncd__pipeline/tektoncd__pipeline_raw_dataset.jsonl (5 PRs).

    PR    base sha    date      go.mod
    4659  9a2deb3e    2022-03   go 1.16
    4440  1401ab77    2021-12   go 1.13
    2835  75750000    2020-06   go 1.13
    1987  bf99afce    2020-01   go 1.13
    1888  e5b2530c    2020-01   go 1.13

ONE shared base image (rule 4, row 3 -> confirmed by the user as a single era)
on golang:1.16. golang:1.16 is safe for the go-1.13 trees because Go only runs
the strict vendor/modules.txt consistency check when go.mod declares go >= 1.14;
below that the vendor directory is consumed as-is. Verified by probe on
2026-09-04: `go build -mod=vendor ./...` at e5b2530c under golang:1.16 exits 0.

Layout follows RULE 9:

    base Dockerfile  toolchain, proxy/CA, ENV, LABELs, WORKDIR /home/, git clone,
                     CMD ["/bin/bash"].  NOTHING after the clone -- no checkout,
                     no pin, no gc, no scrub, no asserts.  Full history is kept
                     on purpose, so one shared base serves every PR in the era.
    PR Dockerfile    FROM <base>, the COPY lines, ARG BASE_COMMIT="<sha>",
                     RUN bash /home/prepare.sh, then the FULL hardening block.
    prepare.sh       checkout + build/cache warm-up. No stripping, no hardening.

The base declares a string dependency(), so DockerfileEnhancer would normally
rewrite its `git clone` into a pinned checkout plus the hardening block (see
image.py:_standardize_repo_fetch / _inject_final_sanitize) -- which is exactly
what rule 9 forbids in the base. Emitting the BuildKit syntax directive makes
DockerfileEnhancer.enhance() return the Dockerfile verbatim (image.py:315), so
the infrastructure block is assembled here from the enhancer's own constants.
That keeps it byte-identical to what every other image gets while leaving the
clone unpinned.
"""

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.16"
_ERA = "4659_to_1888"
_BASE_TAG = f"base-{_ERA}"

# Stripped off the `Package` field of `go test -json` events so a test id reads
# `go::pkg/pod::TestOrderContainers` rather than the full module path.
_REPO_PREFIX = "github.com/tektoncd/pipeline/"

# Wall-clock races, dropped from every stage.
#
# Each of these asserts that something happened "in time" and each one has now
# been observed BOTH passing and failing on this dataset, on different PRs and
# different stages, purely with machine load. None is related to any fix patch
# here. Their failure messages say so plainly:
#
#   TestSetTaskRunTimer            "timer did not execute task run callback
#                                   func within expected time"
#   TestRealRunnerTimeout          "step didn't timeout"
#   TestRealWaiterWaitWithContent  "expected Wait() to have detected a non-zero
#                                   file size by now"
#   TestSendCloudEventWithRetries  "timed out waiting for 1 more events"
#
# Left in, a rotation is not merely noise -- it invalidates the whole instance.
# report.py rule 2 fails a PR the moment any test goes PASS in the test stage
# and FAIL in the fix stage, and on the 2026-09-04 multi-arch run
# TestSetTaskRunTimer did exactly that to PR 2835 (it had failed PR 1888's run
# stage on the single-arch run four hours earlier). That is a rotating suite, so
# it gets EXCLUDED, not tuned with a bigger timeout.
#
# Removal happens in parse_log, so all three stages drop them identically and no
# stage can disagree with another. Entries are "<pkg>::<TestName>" and cover
# that test's subtests too. Two package spellings for the cloudevent retry test
# because it moved from pkg/reconciler/taskrun/resources/cloudevent to
# pkg/reconciler/events/cloudevent partway through this era.
#
# NOT excluded, deliberately: TestSendCloudEvent/..._invalid_sink_URI. That one
# is deterministic -- a Go 1.14+ net/http error-string change, failing in run,
# test AND fix alike -- so it can never trip rule 2, and hiding it would hide a
# real difference between golang:1.16 and the era's own golang:1.13.
_FLAKY_TESTS = frozenset(
    {
        "pkg/reconciler::TestSetTaskRunTimer",
        "cmd/entrypoint::TestRealRunnerTimeout",
        "cmd/entrypoint::TestRealWaiterWaitWithContent",
        "pkg/reconciler/events/cloudevent::TestSendCloudEventWithRetries",
        "pkg/reconciler/taskrun/resources/cloudevent::TestSendCloudEventWithRetries",
    }
)


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
#   -p 4       cap how many package binaries run at once. Go defaults this to
#              GOMAXPROCS, which is 12 here against only 7 GB of RAM, and every
#              tekton reconciler package stands up its own fake Kubernetes
#              clients and informers. Under that memory pressure the wall-clock
#              assertions inside the reconciler tests start missing their
#              deadlines non-deterministically -- on the 2026-09-04 runs
#              TestReconcile_InvalidPipelineRuns failed on a DIFFERENT subtest in
#              the run stage than in the test stage of the same PR. Those tests
#              cannot simply be excluded like the ones in _FLAKY_TESTS, because
#              pkg/reconciler/taskrun::TestReconcile is a genuine f2p for PR
#              2835; dropping it would delete real signal to hide noise. Capping
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

{label_block}

{enh._CERT_SYMLINKS}

{enh._MITM_MOUNT}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

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

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the Go build cache so the three test stages do not each pay for a full
# compile of the tree. Vendored, so no network is touched.
go build -mod=vendor ./... || true
go test -run ZZZ_NO_SUCH_TEST -count=1 -mod=vendor ./... || true

git reset --hard
bash /home/check_git_changes.sh
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

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


def _parse_go_test_log(test_log: str) -> TestResult:
    """Parse `go test -json` output into `go::<pkg path>::<TestName>` ids.

    The tool name leads the id on purpose. report.py's matchers take
    `test_name.split("::", 1)[0]` and compare it against the patch's file paths
    (report.py:388); a leading `go` can never collide with a real file, so a fix
    patch that creates a file cannot mis-flag a test as fix-authored.

    Subtests keep their `Parent/child` shape, which is what
    `_candidate_identifiers` expects for Go (report.py:538).
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


@Instance.register("tektoncd", f"pipeline_{_ERA}")
class Pipeline4659To1888(_PipelineInstanceBase):
    pass
