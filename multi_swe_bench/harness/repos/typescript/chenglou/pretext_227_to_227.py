import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    generated = re.compile(r"(^|/)(dist|build|\.output|\.wxt)/")

    sections: list[tuple[str, list[str]]] = []
    preamble: list[str] = []
    current: Optional[tuple[str, list[str]]] = None

    for line in patch.splitlines(keepends=True):
        match = header.match(line.rstrip("\n"))
        if match:
            current = (match.group(1), [line])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
        else:
            preamble.append(line)

    kept: list[str] = list(preamble)
    for path, lines in sections:
        body = "".join(lines)
        is_binary = "GIT binary patch" in body or "Binary files " in body
        has_index = bool(re.search(r"^index [0-9a-f]+\.\.[0-9a-f]+", body, re.M))
        if is_binary and not has_index:
            continue
        if is_binary and "Binary files " in body:
            continue
        if generated.search(path):
            continue
        kept.append(body)

    return "".join(kept)


class PretextImageBase(Image):

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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base-bun"

    def workdir(self) -> str:
        return "base-bun"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["unzip"]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

ENV BUN_VERSION=1.4.4
RUN npm install --global --no-fund --no-audit "bun@${{BUN_VERSION}}" \\
    && bun --version
ENV PATH="/usr/local/bin:${{PATH}}"

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PretextImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return PretextImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(".", "fix.patch", f"{filtered_fix_patch}"),
            File(".", "test.patch", f"{filtered_test_patch}"),
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PATH="/usr/local/bin:$PATH"

cd /home/{pr.repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

bun install --frozen-lockfile

bun --version
bun -e "import('./src/layout.ts').then(() => console.log('DEPS_OK'))"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PATH="/usr/local/bin:$PATH"

cd /home/{pr.repo}
bun test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PATH="/usr/local/bin:$PATH"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bun test

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PATH="/usr/local/bin:$PATH"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bun test

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git config --global --add safe.directory /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
WORKDIR /home

{self.clear_env}

"""


@Instance.register("chenglou", "pretext_227_to_227")
class PRETEXT_227_TO_227(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PretextImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_file = re.compile(r"^(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx)):$")
        re_result = re.compile(r"^\((pass|fail|skip|todo)\)\s+(.*?)(?:\s+\[[\d.]+m?s\])?$")
        re_summary_start = re.compile(r"^\d+\s+tests?\s+(?:failed|skipped):$")

        current_file = ""
        for raw_line in clean_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if re_summary_start.match(line):
                break

            match = re_file.match(line)
            if match:
                current_file = match.group(1)
                continue

            match = re_result.match(line)
            if not match:
                continue

            status, name = match.group(1), match.group(2).strip()
            if not name:
                continue
            full_name = f"{current_file} > {name}" if current_file else name

            if status == "pass":
                passed_tests.add(full_name)
            elif status == "fail":
                failed_tests.add(full_name)
            else:
                skipped_tests.add(full_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("chenglou", "pretext")(PRETEXT_227_TO_227)
