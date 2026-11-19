import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    generated = re.compile(r"(^|/)(dist|build|\.output|\.next)/")

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


def _base_sha(pr: PullRequest) -> str:
    return pr.base.sha


class UmamiImageBase(Image):

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
        return "base-pnpm12-vitest4"

    def workdir(self) -> str:
        return "base-pnpm12-vitest4"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git openssl python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@12
RUN pnpm --version

{self.clear_env}

"""


class UmamiImageDefault(Image):

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
        return UmamiImageBase(self.pr, self.config)

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
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {base_sha} 2>/dev/null \\
    || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {base_sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

export CI=true
export DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"

pnpm install --ignore-scripts || true

pnpm generate:db:client || pnpm build-db-client || true

node -e "require.resolve('vitest/package.json')" || pnpm exec vitest --version
test -d src/generated/prisma || test -d node_modules/.prisma
echo DEPS_OK

""".format(pr=self.pr, base_sha=_base_sha(self.pr)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
pnpm exec vitest run --no-file-parallelism --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
pnpm exec vitest run --no-file-parallelism --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export DATABASE_URL="postgresql://dummy:dummy@localhost:5432/dummy"
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm exec vitest run --no-file-parallelism --reporter=verbose

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

        if self.config.need_clone:
            clone = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{clone}

{prepare_commands}

WORKDIR /home/{self.pr.repo}
ARG BASE_COMMIT="{_base_sha(self.pr)}"
RUN git config --global --add safe.directory /home/{self.pr.repo}
{Image._HARDENING_BLOCK}
WORKDIR /home

{self.clear_env}

"""


@Instance.register("umami-software", "umami_4592_to_4450")
class UMAMI_4592_TO_4450(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UmamiImageDefault(self.pr, self._config)

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

        re_result = re.compile(
            r"^[\s|]*([✓×✗↓○])\s+"
            r"(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx)\s+>\s+.*?)$"
        )
        re_trailing = re.compile(
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?"
            r"(?:\s+\((?:\d+\s+tests?|retry\s+x\d+|\d+\s+skipped)[^)]*\))?"
            r"(?:\s+\[skipped\])?$"
        )

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue

            match = re_result.match(line)
            if not match:
                continue

            symbol, name = match.group(1), match.group(2)
            name = re_trailing.sub("", name).strip()
            if not name or " > " not in name:
                continue

            if symbol == "✓":
                passed_tests.add(name)
            elif symbol in ("×", "✗"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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


Instance.register("umami-software", "umami")(UMAMI_4592_TO_4450)
