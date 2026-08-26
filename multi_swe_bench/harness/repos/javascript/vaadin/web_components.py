import hashlib
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_CMD = "yarn test --group button"


def _runner(repo: str, apply_block: str) -> str:
    return """#!/bin/bash
set -eo pipefail

cd /home/{repo}

{apply_block}{test_cmd}
""".format(
        repo=repo,
        apply_block=apply_block,
        test_cmd=_TEST_CMD,
    )


def _apply(patch_path: str) -> str:

    return """if ! git apply --whitespace=nowarn {patch}; then
    if ! git apply --3way --whitespace=nowarn {patch}; then
        echo "PATCH_APPLY_FAILED: {patch}"
        exit 1
    fi
fi
""".format(patch=patch_path)


class WebComponentsImageBase(Image):
    """Per-PR base for vaadin/web-components.

    Pinned to this PR's own BASE_COMMIT and carrying the complete history
    scrub, so `pr-<N>` has no scrub at all. See the module docstring for the
    tag choice.
    """

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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")
        chromium_install = (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        chromium \\\n"
            "        fonts-liberation \\\n"
            "        libasound2 \\\n"
            "        libatk-bridge2.0-0 \\\n"
            "        libatk1.0-0 \\\n"
            "        libatspi2.0-0 \\\n"
            "        libcairo2 \\\n"
            "        libcups2 \\\n"
            "        libdrm2 \\\n"
            "        libgbm1 \\\n"
            "        libgtk-3-0 \\\n"
            "        libnspr4 \\\n"
            "        libnss3 \\\n"
            "        libpango-1.0-0 \\\n"
            "        libx11-xcb1 \\\n"
            "        libxcomposite1 \\\n"
            "        libxdamage1 \\\n"
            "        libxext6 \\\n"
            "        libxfixes3 \\\n"
            "        libxkbcommon0 \\\n"
            "        libxrandr2 \\\n"
            "        libxrender1 \\\n"
            "        libxshmfence1 \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

        chrome_wrapper = (
            "RUN printf '%s\\n' \\\n"
            "        '#!/bin/sh' \\\n"
            "        'exec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage --headless=new \"$@\"' \\\n"
            "        > /usr/local/bin/chrome-wrapper \\\n"
            "    && chmod +x /usr/local/bin/chrome-wrapper"
        )

        runtime_env = (
            'ENV YARN_CACHE_FOLDER="/usr/local/share/.cache/yarn" \\\n'
            '    npm_config_loglevel="warn" \\\n'
            '    CI="true" \\\n'
            '    CHROME_PATH="/usr/local/bin/chrome-wrapper" \\\n'
            '    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1"'
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            runtime_env,
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*",
            chromium_install,
            chrome_wrapper,
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            "RUN yarn install --frozen-lockfile --non-interactive",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class WebComponentsImageDefault(Image):

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
        return WebComponentsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{repo} is not a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}

# The base already cloned, checked out {sha} and installed dependencies.
# This layer only proves that state is what it claims to be - deliberately
# no `git clean -fdx`, which would delete the node_modules/ the base
# spent ~70 s producing.
git reset --hard
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"
test -d node_modules
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(".", "run.sh", _runner(self.pr.repo, "")),
            File(
                ".",
                "test-run.sh",
                _runner(self.pr.repo, _apply("/home/test.patch")),
            ),
            File(
                ".",
                "fix-run.sh",
                _runner(
                    self.pr.repo,
                    _apply("/home/test.patch") + _apply("/home/fix.patch"),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


@Instance.register("vaadin", "web-components")
class WebComponents(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebComponentsImageDefault(self.pr, self._config)

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
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for match in re.finditer(r"^\s*❌\s+(.+?)\s*$", clean_log, re.MULTILINE):
            failed_tests.add(match.group(1))

        passed_count = 0
        failed_count = 0
        skipped_count = 0

        # WTR prints this summary line twice - once with `0/M test files |
        # 0 passed, 0 failed` (initial progress-bar state) and once with the
        # real `M/M ... | X passed, Y failed` tally. `re.search` would grab
        # the first (always zeros); `re.findall` + take-last returns the
        # real final counts. See the module docstring.
        summary_matches = re.findall(
            r"Chrome:\s*\|[^|]*\|\s*\d+/\d+\s*test\s*files\s*\|\s*"
            r"(\d+)\s*passed"
            r"(?:,\s*(\d+)\s*failed)?"
            r"(?:,\s*(\d+)\s*skipped)?",
            clean_log,
        )
        if summary_matches:
            last = summary_matches[-1]
            passed_count = int(last[0])
            failed_count = int(last[1] or 0)
            skipped_count = int(last[2] or 0)

        if failed_tests and len(failed_tests) != failed_count:
            failed_count = len(failed_tests)
        log_fingerprint = hashlib.sha1(clean_log.encode("utf-8")).hexdigest()[:12]
        passed_tests: set[str] = {
            f"__wtr_unnamed_passed_{log_fingerprint}_{i:04d}__"
            for i in range(passed_count)
        }

        return TestResult(
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
