import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FluentIterableImageBase(Image):
    """Repo-level base: node + npm install of the mocha/ts-node/chai toolchain.

    fluent-iterable is a small TS library; `npm test` = `mocha`, driven by `.mocharc.json`
    which `extends node_modules/@codibre/confs/mocharc.json` (that shared config registers
    ts-node/register). Tests are `test/*.spec.ts` importing from `../src` with chai."""

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
# installs @codibre/confs (mocharc/tsconfig), ts-node, mocha, chai, stream-mock, reflect-metadata
RUN npm install --force || npm install --force || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class FluentIterableImageDefault(Image):
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
        return FluentIterableImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Scope mocha to the 2 touched spec files. TS_NODE_TRANSPILE_ONLY: at the TEST stage
        # sort-by.spec.ts imports `sortBy`/`desc` which don't exist yet — transpile-only lets
        # ts-node LOAD the file (no type-check abort) so the specs RUN and FAIL individually
        # (n2p/f2p), instead of a compile error that yields 0 tests. --require ts-node/register
        # + reflect-metadata belt-and-suspenders in case the .mocharc require doesn't apply to
        # explicit files. --reporter json → stable fullTitle identities for parse_log.
        test_cmd = (
            "TS_NODE_TRANSPILE_ONLY=true TS_NODE_PROJECT=tsconfig.test.json "
            "npx mocha --reporter json "
            "--require ts-node/register --require reflect-metadata "
            "test/order-assuring.spec.ts test/sort-by.spec.ts 2>&1 || true"
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


@Instance.register("codibre", "fluent-iterable")
class FLUENT_ITERABLE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FluentIterableImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # mocha --reporter json emits one or more top-level {..} JSON objects with
        # passes/failures/pending arrays; identity = fullTitle.
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        depth = 0
        start = None
        blocks = []
        for i, ch in enumerate(clean):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(clean[start : i + 1])
                    start = None
        for block in blocks:
            try:
                data = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for t in data.get("passes", []):
                title = t.get("fullTitle", t.get("title", ""))
                if title:
                    passed.add(title)
            for t in data.get("failures", []):
                title = t.get("fullTitle", t.get("title", ""))
                if title:
                    failed.add(title)
            for t in data.get("pending", []):
                title = t.get("fullTitle", t.get("title", ""))
                if title:
                    skipped.add(title)
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
