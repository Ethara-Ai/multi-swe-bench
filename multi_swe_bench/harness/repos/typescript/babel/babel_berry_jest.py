import re
from typing import Optional, Union
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Era 4: yarn berry (bundled yarnPath) + jest, node:16-slim
# .yarnrc.yml with yarnPath: .yarn/releases/yarn-2.4.x.cjs, no packageManager field
# PRs #10853-#13727 (main, next-8-dev, feature branches)
# Install via: node .yarn/releases/yarn-*.cjs install (no corepack)
# Test output (jest --verbose --ci):
#   PASS packages/.../test/index.js
#   ✓ test name (X ms)    — pass
#   ✕ test name (X ms)    — fail
#   ○ skipped test name    — skip


class BabelBerryImageBase(Image):
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
        return "node:16-slim"

    def image_tag(self) -> str:
        return f"base-berry-jest-{self.pr.base.sha[:8]}"

    def workdir(self) -> str:
        return f"base-berry-jest-{self.pr.base.sha[:8]}"

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

        apt_cmd = (
            "RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\\n"
            "    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
            "    sed -i '/bullseye-updates/d' /etc/apt/sources.list && \\\n"
            "    apt-get update && apt-get install -y --no-install-recommends ca-certificates git make python3 && rm -rf /var/lib/apt/lists/*"
        )

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )
        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "ARG BASE_COMMIT"
            ),
            "ENV DEBIAN_FRONTEND=noninteractive \\\n    LANG=C.UTF-8 \\\n    TZ=UTC",
            label,
            "WORKDIR /home/",
            apt_cmd,
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.strip(),
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class BabelBerryImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return BabelBerryImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
YARN_BIN=$(find .yarn/releases -name 'yarn-*.cjs' | head -1)
node $YARN_BIN install || true
""".format(pr=self.pr),
            ),

            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
YARN_BIN=$(find .yarn/releases -name 'yarn-*.cjs' | head -1)
node $YARN_BIN install || true
make build || true
BABEL_ENV=test node $YARN_BIN node $(node $YARN_BIN bin jest) --verbose --ci || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
YARN_BIN=$(find .yarn/releases -name 'yarn-*.cjs' | head -1)
node $YARN_BIN install || true
make build || true
BABEL_ENV=test node $YARN_BIN node $(node $YARN_BIN bin jest) --verbose --ci || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
YARN_BIN=$(find .yarn/releases -name 'yarn-*.cjs' | head -1)
node $YARN_BIN install || true
make build || true
BABEL_ENV=test node $YARN_BIN node $(node $YARN_BIN bin jest) --verbose --ci || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        sections = [
            f"FROM {name}:{tag}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            copy_commands,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip(),
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


@Instance.register("babel", "13214-13229-13294")
class babel_berry_jest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BabelBerryImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$"),
        ]

        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[✕×✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$"),
        ]

        skipped_res = [
            re.compile(r"^\s*○\s+skipped\s+(.+)$"),
        ]

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m and m.group(1) not in passed_tests and m.group(1) not in failed_tests:
                    skipped_tests.add(m.group(1))

        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
