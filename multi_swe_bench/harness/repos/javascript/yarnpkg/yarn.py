import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files


def _get_test_command(pr_number: int) -> str:
    """PR 2857: simple npx jest. PR 3929+: node with 4GB heap to avoid OOM."""
    if pr_number <= 2857:
        return "npx jest --coverage --verbose"
    return "node --max_old_space_size=4096 node_modules/jest/bin/jest.js --coverage --verbose"


def _target_test_files(test_patch: str) -> str:
    """Space-joined __tests__/**/*.js files touched by this PR's test.patch.

    Yarn's full suite (~70 files) makes live calls against today's npm
    registry; many of those now fail against Node 10 (packages that moved to
    requiring Node>=12, etc.), which crashes jest's worker pool
    (ProcessTerminatedError) and aborts the whole run before it ever reaches
    the file(s) this PR is actually graded on -- verified: for PR 4211 the
    crash happens two suites in, before __tests__/commands/info.js (the file
    test.patch touches) ever runs. Scoping every stage (run/test/fix) to just
    the modified test file(s) avoids that unrelated flakiness so a genuine
    fail->pass isn't drowned out. Fixture/mock/snapshot paths under
    __tests__/ are excluded -- they aren't runnable jest suites. Empty
    string (no targetable spec) falls back to the whole suite.
    """
    files = [
        f
        for f in get_modified_files(test_patch or "")
        if f.startswith("__tests__/")
        and f.endswith(".js")
        and "/fixtures/" not in f
        and "/__mocks__/" not in f
        and "/__snapshots__/" not in f
    ]
    return " ".join(files)


class YarnImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:10-buster"

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

        # `# syntax=` makes DockerfileEnhancer return this Dockerfile verbatim
        # instead of injecting its BASE_COMMIT-driven checkout+hardening pass.
        # This image is the SHARED, once-built ":base" tag reused by every PR:
        # it clones the repo UNPINNED (whatever HEAD currently is, no
        # BASE_COMMIT/checkout here) and warms the common npm dependencies
        # once. Each PR's own image (YarnImageDefault) checks out its own
        # BASE_COMMIT on top of this shared clone and re-runs npm install to
        # reconcile with that commit's package.json. The base must never be
        # hardened/pinned to any one PR's commit -- that would prune every
        # other PR's history out of the shared clone. Mirrors p5.js's
        # ImageBase / BufImageBase (buf.py), which hit the identical problem.
        return f"""\
# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

RUN npm install || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class YarnImageDefault(Image):
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
        return YarnImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_command = _get_test_command(self.pr.number)
        targets = _target_test_files(self.pr.test_patch)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

# -uno: ignore untracked files (node_modules/, package-lock.json etc. warmed
# by the shared base image's `npm install`) -- only tracked-file changes
# indicate a bad checkout.
if [[ -n $(git status --porcelain -uno) ]]; then
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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
npm install || true
npx gulp build
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
timeout -k 30 300 {test_command} {targets} || true
""".format(repo=self.pr.repo, test_command=test_command, targets=targets),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
npx gulp build
timeout -k 30 300 {test_command} {targets} || true
""".format(repo=self.pr.repo, test_command=test_command, targets=targets),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
git apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
git apply --whitespace=nowarn --allow-binary-replacement /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true
npx gulp build
timeout -k 30 300 {test_command} {targets} || true
""".format(repo=self.pr.repo, test_command=test_command, targets=targets),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        name = dep.image_name()
        tag = dep.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # dependency() returns an Image (not a string), so DockerfileEnhancer
        # returns this Dockerfile verbatim -- BASE_COMMIT is not auto-injected
        # and must be declared here. prepare.sh installs deps + builds (network
        # is available at build time, before the hardening strip); the strip
        # runs after, mirroring ValidatorJsImageDefault/p5.js ImageDefault, so
        # the shipped image carries no future commits/branches/tags/reflog for
        # an evaluated agent to read.
        header = f"""\
FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("yarnpkg", "yarn")
class Yarn(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return YarnImageDefault(self.pr, self._config)

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
        """Parse Jest --verbose suite-level output: PASS/FAIL path/to/file.js"""
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+\.?\d*\s*\w+\))?\s*$")
        re_fail = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+\.?\d*\s*\w+\))?\s*$")

        for line in clean_log.splitlines():
            pass_match = re_pass.match(line)
            if pass_match:
                test = pass_match.group(1).strip()
                passed_tests.add(test)
                continue

            fail_match = re_fail.match(line)
            if fail_match:
                test = fail_match.group(1).strip()
                failed_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file changed).
#
# The delivered JSONL carries no `number_interval`; it carries exact bundle
# membership in `prs_in_bundle`. Dataset.build() copies number_interval
# straight off the loaded PullRequest into the resolved output jsonl, so
# filling it at load time is what makes it appear downstream. The required
# value is the EXACT PRs joined with '-', never a range:
#
#     prs_in_bundle: [146, 147, 150, 155, 157]
#     number_interval: "146-147-150-155-157"      (NOT "146-157")
#
# A range would claim every PR in between as part of the bundle, which is
# wrong -- yarn's bundles are sparse (e.g. anchor 3929 bundles
# 3929-4374-...-5216, skipping thousands of intervening PRs). Two small,
# idempotent, yarnpkg/yarn-scoped shims are installed at import time:
#
#   1. PullRequest.from_json -- the only loader path the harness actually
#      uses (build_dataset.py / gen_report.py read jsonl lines through this).
#      For yarnpkg/yarn records whose number_interval is empty, fill it from
#      the raw line's prs_in_bundle. Only empty values are filled, so an
#      explicitly-set number_interval is never overwritten, and other repos
#      are untouched.
#   2. Instance.create -- once number_interval is non-empty, routing looks up
#      `yarnpkg/<number_interval>`, which is not a registered key (yarn
#      registers under the plain "yarnpkg/yarn"). On the resulting
#      ValueError, fall back to "yarnpkg/yarn" so the build still routes.
# ---------------------------------------------------------------------------

if not getattr(PullRequest, "_yarnpkg_ni_shim", False):
    _yarn_orig_from_json = PullRequest.from_json.__func__

    def _yarn_from_json(cls, json_str):
        pr = _yarn_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "yarnpkg"
                and getattr(pr, "repo", "") == "yarn"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_yarn_from_json)
    PullRequest._yarnpkg_ni_shim = True

if not getattr(Instance, "_yarnpkg_route_shim", False):
    _yarn_orig_create = Instance.create.__func__

    def _yarn_create(cls, pr, config, *args, **kwargs):
        try:
            return _yarn_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if (
                getattr(pr, "org", "") == "yarnpkg"
                and getattr(pr, "repo", "") == "yarn"
            ):
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_yarn_create)
    Instance._yarnpkg_route_shim = True
