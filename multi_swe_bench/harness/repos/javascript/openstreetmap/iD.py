import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class IDImageBase(Image):
    """Repo-level base: node + Chromium (headless, --no-sandbox) + build + npm install.
    iD's karma runs specs in ChromeHeadless; in a root container Chrome needs --no-sandbox,
    injected via a CHROME_BIN wrapper. karma-spec-reporter is added so per-test names are
    emitted (the repo's default 'progress' reporter only prints summaries)."""

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
RUN apt-get update && apt-get install -y git chromium && rm -rf /var/lib/apt/lists/*

# Chrome refuses to run as root without --no-sandbox; wrap the binary so karma's
# ChromeHeadless launcher picks it up via CHROME_BIN.
RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage --disable-gpu "$@"\\n' > /usr/local/bin/chrome-headless \\
    && chmod +x /usr/local/bin/chrome-headless
ENV CHROME_BIN=/usr/local/bin/chrome-headless

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN npm install --force || npm install --force || true
RUN npm install --no-save karma-spec-reporter || true
# `npm run build` is run-s css+data+js; build:data FAILS (network fetch) and bails the chain
# so build:js never runs and karma's `dist/iD.js` (global `iD`) is missing. Run each step
# independently with tolerance so build:js emits dist/iD.js + build:css emits dist/iD.css.
RUN npm run build:css || true
RUN npm run build:data || true
RUN npm run build:js || true

{self.clear_env}

CMD ["/bin/bash"]
"""


class IDImageDefault(Image):
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
        return IDImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Patch karma.conf to emit per-test output (spec reporter). plugins is explicit so
        # add the plugin there too. Then run karma headless (single-run) over all specs.
        patch_conf = (
            "sed -i \"s/plugins: \\[/plugins: ['karma-spec-reporter',/\" config/karma.conf.js; "
            "sed -i \"s/reporters: \\[[^]]*\\]/reporters: ['spec']/\" config/karma.conf.js"
        )
        # karma tests run against the built bundle dist/iD.js, NOT source — so REBUILD it
        # after the patches are applied (else fix.patch's source change never reaches the
        # bundle the browser loads, and the new test fails identically in test & fix stages).
        test_cmd = (
            "npm run build:js || true; "
            f"{patch_conf}; "
            "CHROME_BIN=/usr/local/bin/chrome-headless "
            "npx karma start config/karma.conf.js --single-run 2>&1 || true"
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


@Instance.register("openstreetmap", "iD")
class ID(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return IDImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # karma-spec-reporter emits `✓ name` / `✗ name` (jest-like). Identity = title.
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        for raw in clean.split("\n"):
            line = raw.rstrip()
            m = re.match(r"^\s*([✓✔])\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line)
            if m:
                passed.add(m.group(2).strip())
                continue
            m = re.match(r"^\s*([✗✕×])\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line)
            if m:
                failed.add(m.group(2).strip())
        passed -= failed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
