"""Swatinem/rollup-plugin-dts — TypeScript, custom test harness.

package.json: engines.node >=16, npm (package-lock.json), and

    pretest: npm run build      (tsc && rollup --config .build/rollup.config.js)
    test:    c8 node .build/tests/index.js

so the suite runs COMPILED output under `.build/`. `npm test` therefore rebuilds
first -- the graded stages must invoke `npm test`, not the compiled entry point
directly, or a patch to `src/**` or `tests/**` would never reach the run.

The harness is bespoke (tests/utils.ts `Harness`), not mocha/jest. It prints one
line per case:

    <name>...  ok
    <name>...  failed

and then a "Failures:" block listing `- <name>`. parse_log reads the per-case
lines; the trailing block is redundant and deliberately ignored.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "Swatinem"
REPO = "rollup-plugin-dts"

NPM_TEST = "npm test"


class DtsImageBase(Image):
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
        # engines.node >=16; 20 is the closest LTS that still satisfies the
        # rollup/typescript versions pinned in package-lock.json.
        return "node:20-bullseye"

    def image_tag(self) -> str:
        return "base-node20"

    def workdir(self) -> str:
        return "base-node20"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}

ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 TZ=Etc/UTC CI=true \\
    NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}"

RUN set -eux; \\
    sed -i '/-security/d' /etc/apt/sources.list; \\
    ! grep -q '\\-security' /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN set -eux; \\
    git config --global core.compression 0; \\
    git config --global http.postBuffer 1048576000; \\
    for i in 1 2 3; do git clone "${{REPO_URL}}" /home/{REPO} && break; rm -rf /home/{REPO}; done; \\
    git -C /home/{REPO} rev-parse --verify HEAD

WORKDIR /home/{REPO}

CMD ["/bin/bash"]
"""


class DtsImageDefault(Image):
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
        return DtsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git reset --hard
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch --no-tags origin {sha}
git cat-file -e {sha}^{{commit}}
git checkout --detach {sha}

npm ci --no-audit --no-fund
""".format(repo=REPO, sha=self.pr.base.sha),
            ),
            File(".", "run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{repo}
{test}
""".format(repo=REPO, test=NPM_TEST)),
            File(".", "test-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test}
""".format(repo=REPO, test=NPM_TEST)),
            File(".", "fix-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The fix patch can move package.json/package-lock.json; reinstall so the build
# that `pretest` runs sees the dependency set the fixed code expects.
if git diff --name-only HEAD | grep -qE '^(package|package-lock)\\.json$'; then
    npm ci --no-audit --no-fund
fi
{test}
""".format(repo=REPO, test=NPM_TEST)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copies.rstrip()}

RUN bash /home/prepare.sh
"""


@Instance.register(ORG, REPO)
class RollupPluginDts(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DtsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed, failed, skipped = set(), set(), set()
        log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Harness writes "<name>... " then logs " ok" / " failed" on the same
        # line. Anchored on the trailing verdict so stray "..." in diff output
        # cannot be mistaken for a case.
        case = re.compile(r"^(.*?)\.\.\.\s+(ok|failed)\s*$")
        for line in log.splitlines():
            m = case.match(line.rstrip())
            if not m:
                continue
            name, verdict = m.group(1).strip(), m.group(2)
            (passed if verdict == "ok" else failed).add(f"dts::{name}")

        # A build failure (tsc/rollup in `pretest`) means no case ever ran.
        if not (passed or failed or skipped) and re.search(
            r"npm ERR!|error TS\d+|Cannot find module|rollup .*error", log
        ):
            failed.add("dts::<build or suite failed>")

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
