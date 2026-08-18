import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MsdnDelocalizerImageBase(Image):
    """Repo-level base: node + npm install + a baked vitest.

    msdn-delocalizer #40 migrates the whole stack (mocha 2.x -> vitest, browserify -> WXT).
    The added test `test/url-utils.spec.ts` imports from `vitest` and from the MOVED module
    `../src/utils/url-utils` (fix.patch creates src/utils/url-utils.ts from the base
    src/url-utils.ts). Bake vitest so the spec can run in every stage; the run scripts strip
    the wxt/vite config files first so vitest uses its defaults (no WXT install needed for a
    plain relative-import spec). test stage: the moved module is absent -> vitest fails; fix
    stage: it exists -> passes -> n2p."""

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
        return "node:20-bullseye"

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
RUN npm install --force || true
# vitest transpiles via esbuild (base pins ancient typescript 3.8, which vitest ignores). Bake
# it so the spec runs regardless of the base's mocha-2 toolchain.
RUN npm install --no-save --force vitest@^1.6.0 || npm install --no-save --force vitest || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class MsdnDelocalizerImageDefault(Image):
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
        return MsdnDelocalizerImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Strip the wxt/vite config files (fix.patch adds wxt.config.ts which imports `wxt` —
        # loading it would need the full WXT install). The spec uses only a relative import, so
        # plain vitest with defaults resolves it. `--root .` keeps cwd as the project root.
        test_cmd = (
            "rm -f wxt.config.ts wxt.config.js web-ext.config.ts "
            "vite.config.ts vite.config.js vitest.config.ts vitest.config.js 2>/dev/null; "
            "npx --no-install vitest run test/url-utils.spec.ts --reporter=verbose 2>&1 "
            "|| npx vitest run test/url-utils.spec.ts --reporter=verbose 2>&1 || true"
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
git apply --whitespace=nowarn --exclude='yarn.lock' --exclude='package-lock.json' /home/test.patch
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
# exclude the lockfiles from fix.patch: its yarn.lock hunk does not apply on the base (which
# has package-lock.json), and git apply is all-or-nothing per file set — excluding it lets the
# real change (src/utils/url-utils.ts + wxt migration) land so vitest can resolve the module.
git apply --whitespace=nowarn --exclude='yarn.lock' --exclude='package-lock.json' /home/test.patch /home/fix.patch
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


@Instance.register("ForNeVeR", "msdn-delocalizer")
class MSDN_DELOCALIZER(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MsdnDelocalizerImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # vitest --reporter=verbose: `✓/×/↓ <file> > <suite> > <name>  <ms>`. Identity = the
        # full `suite > name` chain (fall back to the whole label).
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        def ident(rest: str) -> str:
            rest = re.sub(r"\s+\d+\s*ms\s*$", "", rest).strip()
            rest = re.sub(r"\s+\(\d+\s*ms\)\s*$", "", rest).strip()
            # drop a leading "<file> > " segment if present so identity is suite>name
            parts = rest.split(" > ")
            if len(parts) > 1 and re.search(r"\.(?:test|spec)\.[jt]sx?$", parts[0]):
                rest = " > ".join(parts[1:])
            return rest

        for raw in clean.split("\n"):
            line = raw.rstrip()
            m = re.match(r"^\s*[✓√]\s+(.+)$", line)
            if m:
                passed.add(ident(m.group(1)))
                continue
            m = re.match(r"^\s*[×✗✘]\s+(.+)$", line)
            if m:
                failed.add(ident(m.group(1)))
                continue
            m = re.match(r"^\s*[↓⊝]\s+(.+)$", line)
            if m:
                skipped.add(ident(m.group(1)))
        passed -= failed
        passed -= skipped
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
