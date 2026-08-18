import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CamelcaseKeysImageBase(Image):
    """Repo-level base: node + npm install + type-fest (fix.patch's new runtime type dep).

    camelcase-keys #72 is a TYPE-definition PR: it strengthens `index.d.ts` (using type-fest)
    and the type test `index.test-d.ts`. The graded test is `tsd` (the package's `npm test` is
    `xo && ava && tsd`). type-fest is added by fix.patch's package.json but the run scripts only
    `git apply` (no re-install), so bake it into the base or the fix-stage `index.d.ts` import
    of type-fest can't resolve and tsd would error even after the fix."""

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
        return "node:16-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN npm install --force || npm install --force || true
# fix.patch adds `type-fest` to dependencies and imports it from index.d.ts; make it resolvable
# at the fix stage (run scripts don't re-install). Also ensure tsd is present.
# The 2026 lockfile-less install pulls too-new transitive @types that tsd's whole-project
# type-check trips over (this repo has NO committed lockfile). Pin the era-appropriate versions
# so tsd grades the actual index.test-d.ts vs index.d.ts, not toolchain noise:
#   - @types/node@14   : tsd@0.14's TS can't parse a modern @types/node (Http2 event-map syntax)
#   - minimatch@3 + @types/minimatch@3 : @types/glob@7 (via ava->del/globby) references
#     minimatch's IOptions/IMinimatch, removed in minimatch>=5; the v3 pair restores them.
# With these, baseline tsd passes; test.patch's stricter assertions vs the old index.d.ts fail;
# fix.patch's updated index.d.ts (+ type-fest) pass -> f2p.
# type-fest MUST be pinned to 1.2.2 exactly: index.d.ts's CamelCase-based conditional type only
# resolves Record<string,string> correctly under type-fest 1.2.x — caret 1.2.1 resolves to 1.4.0
# where 2 Record-input assertions infer the empty object type and tsd fails even at the fix stage.
RUN npm install --no-save --force type-fest@1.2.2 tsd@^0.14.0 \\
    @types/node@14 minimatch@3 @types/minimatch@3 || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class CamelcaseKeysImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
        return CamelcaseKeysImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # `tsd` type-checks index.test-d.ts against index.d.ts. It has no named tests: it exits
        # 0 (silent) on success or non-zero with error lines on a type mismatch. Emit the exit
        # code as a marker and synthesize a single test "tsd:index.test-d.ts". run(baseline) +
        # fix pass; test(modified assertions vs old index.d.ts) fails -> f2p.
        test_cmd = (
            "npx --no-install tsd 2>&1 || npx tsd 2>&1; echo \"TSD_EXIT=$?\""
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sindresorhus", "camelcase-keys")
class CAMELCASE_KEYS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CamelcaseKeysImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # tsd is a single pass/fail type-check. Identify it by the emitted TSD_EXIT marker.
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        name = "tsd:index.test-d.ts"
        m = re.search(r"TSD_EXIT=(\d+)", clean)
        if m:
            if m.group(1) == "0":
                passed.add(name)
            else:
                failed.add(name)
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=0,
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=set(),
        )
