import re
from json import JSONDecoder
from typing import Generator, Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base-5290-to-4979"

    def workdir(self) -> str:
        return "base-5290-to-4979"

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

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{self.global_env}

{DockerfileEnhancer._ENV_BLOCK}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@11

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        return "./node_modules/.bin/vitest run --reporter=verbose"

    def patch_excludes(self) -> str:
        return (
            "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' "
            "--exclude='*.gif' --exclude='*.ico' --exclude='*.webp' "
            "--exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' "
            "--exclude='*.eot' --exclude='pnpm-lock.yaml' "
            "--exclude='package-lock.json' --exclude='yarn.lock'"
        )

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -euo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash

cd {repo_dir} || exit 1

git reset --hard || exit 1
git clean -fdx -e node_modules || exit 1
bash /home/check_git_changes.sh || exit 1

git checkout --detach {self.pr.base.sha} || exit 1
bash /home/check_git_changes.sh || exit 1

export CI=true
export TZ=UTC
export HUSKY=0
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_update_notifier=false

if [ -f pnpm-lock.yaml ]; then
  pnpm install --frozen-lockfile || exit 1
else
  npm ci || exit 1
fi

./node_modules/.bin/playwright install-deps chromium || true
./node_modules/.bin/playwright install chromium || exit 1

./node_modules/.bin/vitest --version || exit 1
node -e "require('playwright').chromium.launch().then((b) => b.close()).then(() => console.log('DEPS_OK'));" || exit 1
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

export CI=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096

{self.test_command()}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

export CI=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096

git apply --whitespace=nowarn {self.patch_excludes()} /home/test.patch

{self.test_command()}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {repo_dir}

export CI=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096

git apply --whitespace=nowarn {self.patch_excludes()} /home/test.patch /home/fix.patch

{self.test_command()}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        )

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
{hardening}

RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("preactjs", "preact_5290_to_4979")
class PREACT_5290_TO_4979(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        repo_prefix = f"/home/{self.pr.repo}/"

        def normalize(name: str) -> str:
            return re.sub(r"\s+", " ", name).strip()

        def relative(path: str) -> str:
            index = path.find(repo_prefix)
            if index != -1:
                return path[index + len(repo_prefix) :]
            return path.lstrip("/")

        def iter_json_objects(text: str) -> Generator[dict, None, None]:
            decoder = JSONDecoder()
            pos = 0
            while True:
                start = text.find('{"', pos)
                if start == -1:
                    return
                try:
                    obj, end = decoder.raw_decode(text, start)
                except ValueError:
                    pos = start + 1
                    continue
                pos = max(end, start + 1)
                if isinstance(obj, dict):
                    yield obj

        for obj in iter_json_objects(clean_log):
            suites = obj.get("testResults")
            if not isinstance(suites, list):
                continue
            for suite in suites:
                if not isinstance(suite, dict):
                    continue
                file_name = relative(str(suite.get("name") or ""))
                assertions = suite.get("assertionResults")
                if not isinstance(assertions, list):
                    continue
                for assertion in assertions:
                    if not isinstance(assertion, dict):
                        continue
                    ancestors = assertion.get("ancestorTitles")
                    if not isinstance(ancestors, list):
                        ancestors = []
                    title = assertion.get("title") or ""
                    if title:
                        parts = [str(p) for p in ancestors if p] + [str(title)]
                    else:
                        parts = [str(assertion.get("fullName") or "")]
                    if not parts or not parts[-1]:
                        continue
                    if file_name:
                        parts.insert(0, file_name)
                    name = normalize(" > ".join(parts))
                    status = str(assertion.get("status") or "")
                    if status == "passed":
                        passed_tests.add(name)
                    elif status in ("failed", "broken"):
                        failed_tests.add(name)
                    elif status in ("skipped", "pending", "todo", "disabled"):
                        skipped_tests.add(name)

        if not passed_tests and not failed_tests and not skipped_tests:
            marker_re = re.compile(r"^(\s*)(✓|√|×|✗|✘|↓|❯)\s+(\S.*)$")
            project_re = re.compile(
                r"^\d+\s+(?:\([^)]*\)\s+)?"
                r"(?=\S*\.(?:ts|tsx|js|jsx|mjs|cjs|mts|cts)\b)"
            )
            duration_re = re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s|m)\s*$")
            counts_re = re.compile(r"\s*\((?:\d+\s+\w+(?:\s*\|\s*)?)+\)\s*$")
            retry_re = re.compile(r"\s*\(retry\s+x?\d+\)\s*$", re.IGNORECASE)
            file_re = re.compile(r"\.(?:ts|tsx|js|jsx|mjs|cjs|mts|cts)$")
            current_file = ""

            for line in clean_log.splitlines():
                match = marker_re.match(line)
                if not match:
                    continue

                marker = match.group(2)
                body = match.group(3)
                for pattern in (counts_re, retry_re, duration_re, counts_re):
                    body = pattern.sub("", body)
                body = project_re.sub("", normalize(body))
                if not body:
                    continue

                head = body.split(" > ")[0]
                if file_re.search(head) and " > " not in body:
                    current_file = head
                    continue

                if file_re.search(head):
                    name = body
                elif current_file:
                    name = normalize(f"{current_file} > {body}")
                else:
                    name = body

                if marker in ("✓", "√"):
                    passed_tests.add(name)
                elif marker in ("×", "✗", "✘"):
                    failed_tests.add(name)
                elif marker == "↓":
                    skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("preactjs", "preact")(PREACT_5290_TO_4979)
