import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AxeCoreNpmImageBase(Image):
    """Repo-level base: node + matched chromium/chromium-driver + lerna bootstrap + build.
    The webdriverio package's test spawns npm `chromedriver` and drives Chrome; we point that
    npm binary at the apt-matched system chromedriver so versions line up, and run with CI=1
    (the test uses `isCI ? ['--headless','--no-sandbox'] : []`)."""

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
        # @wdio/sync 6 uses a fiber/coroutine native lib that crashes on node>=16
        # (`coroutine.cc ... Assertion 'thread_id_key' failed`). Fibers work only on node<=14.
        return "node:14-bullseye"

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
# chromium + the apt-matched chromium-driver (same version, avoids the npm chromedriver@90
# vs installed-Chrome mismatch). build-essential for any native compiles.
RUN apt-get update && apt-get install -y \\
    git chromium chromium-driver build-essential python3 pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN ln -sf /usr/bin/chromium /usr/local/bin/google-chrome

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
# lerna bootstrap doesn't reliably populate package node_modules here (empty → ts-node/dist
# missing). Install the webdriverio package DIRECTLY (its runtime dep is npm `axe-core`, so no
# sibling monorepo build is required), then build its dist. Skip the pinned chromedriver
# download; we override the binary with the apt system one below.
# The committed package-lock.json pins deps to Deque's PRIVATE registry
# (https://agora.dequecloud.com) → npm 401 "Incorrect or missing password". Drop all
# lockfiles and install from the public registry.
RUN find . -name package-lock.json -not -path '*/node_modules/*' -delete || true
RUN CHROMEDRIVER_SKIP_DOWNLOAD=true npm install --force --no-package-lock --registry=https://registry.npmjs.org/ || true
RUN cd packages/webdriverio && CHROMEDRIVER_SKIP_DOWNLOAD=true npm install --force --no-package-lock --registry=https://registry.npmjs.org/ || true
# pinned ts-node@9 crashes on the resolved typescript@4.9 (`resolveTypeReferenceDirective`
# non-string). Upgrade ts-node to 10 (supports TS 4.x); keep typescript so the build works.
RUN cd packages/webdriverio && npm install --no-save --no-package-lock --registry=https://registry.npmjs.org/ ts-node@^10 || true
RUN cd packages/webdriverio && (npm run build || npx tsc-silent -p tsconfig.json --suppress @ || true)
# Skip-download left the npm chromedriver with NO binary, so `find -exec` matched nothing.
# CREATE the symlink at the expected path (lib/chromedriver/chromedriver) pointing at the
# apt system chromedriver (version-matched to chromium 120).
RUN for d in $(find . -type d -name chromedriver -path '*/node_modules/chromedriver' 2>/dev/null); do \\
        mkdir -p "$d/lib/chromedriver"; ln -sf /usr/bin/chromedriver "$d/lib/chromedriver/chromedriver"; \\
    done || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class AxeCoreNpmImageDefault(Image):
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
        return AxeCoreNpmImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # test.patch modifies packages/webdriverio/src/test.ts. mocha runs it via ts-node,
        # but it imports the package's BUILT dist ('.') so rebuild after patching. CI=1 =>
        # the test's capabilities use --headless --no-sandbox.
        test_cmd = (
            "cd /home/{repo}/packages/webdriverio && "
            "(npm run build || npx tsc-silent -p tsconfig.json --suppress @ || true); "
            # TS_NODE_TRANSPILE_ONLY: the test.patch adds strict-mode type errors (unknown vs
            # Error|null) that ts-node+TS4.9 rejects; we want to RUN the test, not type-check it.
            "CI=true CHROME_BIN=/usr/bin/chromium TS_NODE_TRANSPILE_ONLY=true "
            "npx mocha --reporter json --timeout 60000 -r ts-node/register src/test.ts 2>&1 || true"
        ).format(repo=self.pr.repo)
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


@Instance.register("dequelabs", "axe-core-npm")
class AXE_CORE_NPM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AxeCoreNpmImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
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
