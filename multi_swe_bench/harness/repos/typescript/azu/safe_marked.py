"""azu/safe-marked — TypeScript, yarn 1 + mocha (ts loader).

Small library: `src/*.ts`, one suite at `test/index.test.ts`, run by mocha via
`.mocharc.json`. Package manager is yarn 1 (`packageManager: yarn@1.22.21`,
`yarn.lock`); CI matrix is node 16/18, so the image pins node 18.

IMPORTANT — the fix patch is a dependency upgrade, not just a code change:

    marked      ^4.3.0  -> ^12.0.0        jsdom     ^22.1.0 -> ^24.0.0
    typescript  ^4.9.4  -> ^5.3.3         prettier  ^2.8.3  -> ^3.2.5
    ts-node     -> REMOVED, replaced by tsimp
    .mocharc.json  "loader": "ts-node/esm"  ->  "import": "tsimp"

So `fix-run.sh` MUST reinstall after applying the patches: the base install has
no `tsimp`, and mocha would abort loading it before running a single test. The
gold and test-patch stages keep the base install (the test patch only touches
`test/index.test.ts` and a CI workflow, neither of which moves a dependency).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "azu"
REPO = "safe-marked"

# mocha's spec/loader come from .mocharc.json, which the fix patch rewrites --
# so the command stays bare and the config file decides how TS is loaded.
MOCHA = "yarn mocha --reporter spec"

# yarn 1 has no `--frozen-lockfile` equivalent that tolerates a patched lockfile
# cleanly, so the reinstall after the fix patch is a plain install.
YARN_INSTALL = "yarn install --non-interactive"


class SafeMarkedImageBase(Image):
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return "base-node18"

    def workdir(self) -> str:
        return "base-node18"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}

ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=Etc/UTC \\
    CI=true \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}"

# bullseye's security suite Release file is expired, which makes `apt-get
# update` fail outright. Drop those lines; the remaining suites are sufficient.
RUN set -eux; \\
    sed -i '/-security/d' /etc/apt/sources.list; \\
    ! grep -q '\\-security' /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN set -eux; \\
    git config --global core.compression 0; \\
    git config --global http.postBuffer 1048576000; \\
    for i in 1 2 3; do \\
        git clone "${{REPO_URL}}" /home/{REPO} && break; \\
        rm -rf /home/{REPO}; \\
    done; \\
    git -C /home/{REPO} rev-parse --verify HEAD

WORKDIR /home/{REPO}

CMD ["/bin/bash"]
"""


class SafeMarkedImageDefault(Image):
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
        return SafeMarkedImageBase(self.pr, self._config)

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
# Base commits can be unreachable from any ref (force-pushed base branch); the
# image clone then lacks them. GitHub still serves them by explicit SHA.
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch --no-tags origin {sha}
git cat-file -e {sha}^{{commit}}
git checkout --detach {sha}

{install}
test -x node_modules/.bin/mocha
""".format(repo=REPO, sha=self.pr.base.sha, install=YARN_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
{mocha}
""".format(repo=REPO, mocha=MOCHA),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{mocha}
""".format(repo=REPO, mocha=MOCHA),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# The fix patch rewrites package.json/yarn.lock and swaps the mocha TS loader
# (ts-node -> tsimp). Without reinstalling, mocha aborts on the missing loader
# before running any test, and every f2p candidate is lost.
{install}
{mocha}
""".format(repo=REPO, install=YARN_INSTALL, mocha=MOCHA),
            ),
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
class SafeMarked(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SafeMarkedImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # mocha spec reporter: nested describes indented, leaves marked by a
        # tick / a numbered failure / a dash for pending. Track the describe
        # stack by indentation so ids stay unique across suites.
        # mocha's spec reporter emits U+2714 HEAVY CHECK MARK, not U+2713.
        # Matching only the latter scored a genuinely-passing 4-test run as 0.
        tick = re.compile(r"^(\s+)[\u2713\u2714\u221a]\s+(.*?)(?:\s+\(\d+m?s\))?$")
        fail = re.compile(r"^(\s+)(?:\d+\)|[\u2715\u2716\u2717\u2718\u00d7])\s+(.*?)$")
        pend = re.compile(r"^(\s+)-\s+(.*?)$")
        head = re.compile(r"^(\s+)([^\s\u2713\u2714\u221a\u2715\u2716\u2717\u2718\u00d7\-\d].*?)$")

        stack: list[tuple[int, str]] = []

        def full(indent: int, name: str) -> str:
            parts = [n for i, n in stack if i < indent]
            return "mocha::" + " > ".join(parts + [name])

        for line in log.splitlines():
            if not line.strip():
                continue
            for pat, bucket in ((tick, passed_tests), (fail, failed_tests), (pend, skipped_tests)):
                m = pat.match(line)
                if m:
                    bucket.add(full(len(m.group(1)), m.group(2).strip()))
                    break
            else:
                m = head.match(line)
                if m:
                    indent = len(m.group(1))
                    stack = [(i, n) for i, n in stack if i < indent]
                    stack.append((indent, m.group(2).strip()))

        # A stage can die before mocha runs anything -- here the test patch
        # imports marked-gfm-heading-id / marked-mangle, which only exist after
        # the fix patch adds them, so module resolution fails and mocha prints
        # its usage banner instead of results. Recording nothing would make the
        # stage indistinguishable from a clean pass; record a suite-level
        # failure so the stage is visibly red.
        if not (passed_tests or failed_tests or skipped_tests):
            if re.search(r"ERROR:|Cannot find (module|package)|ERR_MODULE_NOT_FOUND|"
                         r"error Command failed|Run tests with Mocha", log):
                failed_tests.add("mocha::<suite failed to load>")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
