import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─── Constants ────────────────────────────────────────────────────
REPO_DIR = "silverbullet"
# ──────────────────────────────────────────────────────────────────


class ImageBase(Image):
    """Base image for Era 4 (PR >= 1392): Deno 2.4.3."""

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
        return "denoland/deno:ubuntu-2.4.3"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageBase827_142(Image):
    """Base image for Era 2 (PR 827): Deno 1.42.0 — build_web.ts breaks with Deno 2.x."""

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
        return "denoland/deno:ubuntu-1.42.0"

    def image_tag(self) -> str:
        return "base-827-142"

    def workdir(self) -> str:
        return "base-827-142"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageBase827(Image):
    """Base image for Era 3 (PRs 828-1391): Deno 2.2.2 with granular unstable flags."""

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
        return "denoland/deno:ubuntu-2.2.2"

    def image_tag(self) -> str:
        return "base-827"

    def workdir(self) -> str:
        return "base-827"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageBase174(Image):
    """Base image for Era 1 (PR 174): Deno 1.28.1 with old --unstable flag."""

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
        return "denoland/deno:ubuntu-1.28.1"

    def image_tag(self) -> str:
        return "base-174"

    def workdir(self) -> str:
        return "base-174"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """PR image for Era 4 (PR >= 1392): Deno 2.4.3, `deno test -A`."""

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
        return ImageBase(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
deno task build || true

""".format(repo=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
deno test -A --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
deno task build || true
deno test -A --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
deno task build || true
deno test -A --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


class ImageDefault827_142(Image):
    """PR image for Era 2 (PR 827): Deno 1.42.0."""

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
        return ImageBase827_142(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
deno task build || true

""".format(repo=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
deno task build || true
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
deno task build || true
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


class ImageDefault827(Image):
    """PR image for Era 3 (PRs 828-1391): Deno 2.2.2, `deno test -A --unstable-kv --unstable-worker-options`."""

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
        return ImageBase827(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh
deno task build || true

""".format(repo=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
deno task build || true
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
deno task build || true
deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check

""".format(repo=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


class ImageDefault174(Image):
    """PR image for Era 1 (PR 174): Deno 1.28.1, `deno test -A --unstable`."""

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
        return ImageBase174(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Fix CDN drift: pin deno.land/std yaml URL (unpinned URL no longer resolves)
sed -i 's|https://deno.land/std/encoding/yaml.ts|https://deno.land/std@0.165.0/encoding/yaml.ts|g' import_map.json
sed -i 's|https://deno.land/std/encoding/yaml.ts|https://deno.land/std@0.165.0/encoding/yaml.ts|g' plugos/test_func.test.ts

deno task build || true

""".format(repo=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
deno test -A --unstable --no-lock --no-check \
  --ignore=plugs/markdown/markdown_render.test.ts,common/parser.test.ts,plug-api/lib/tree.test.ts,plugos/hooks/endpoint.test.ts,plugos/runtime.test.ts

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
deno task build || true
deno test -A --unstable --no-lock --no-check \
  --ignore=plugs/markdown/markdown_render.test.ts,common/parser.test.ts,plug-api/lib/tree.test.ts,plugos/hooks/endpoint.test.ts,plugos/runtime.test.ts

""".format(repo=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
deno task build || true
deno test -A --unstable --no-lock --no-check \
  --ignore=plugs/markdown/markdown_render.test.ts,common/parser.test.ts,plug-api/lib/tree.test.ts,plugos/hooks/endpoint.test.ts,plugos/runtime.test.ts

""".format(repo=REPO_DIR),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("silverbulletmd", "silverbullet")
class SilverBullet(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        if self.pr.number <= 174:
            return ImageDefault174(self.pr, self._config)
        if self.pr.number <= 827:
            return ImageDefault827_142(self.pr, self._config)
        if self.pr.number <= 1391:
            return ImageDefault827(self.pr, self._config)
        return ImageDefault(self.pr, self._config)

    def _test_cmd(self) -> str:
        if self.pr.number <= 174:
            return (
                "deno test -A --unstable --no-lock --no-check"
                " --ignore=plugs/markdown/markdown_render.test.ts,common/parser.test.ts,"
                "plug-api/lib/tree.test.ts,plugos/hooks/endpoint.test.ts,plugos/runtime.test.ts"
            )
        if self.pr.number <= 1391:
            return "deno test -A --unstable-kv --unstable-worker-options --no-lock --no-check"
        return "deno test -A --no-lock --no-check"

    def _build_cmd(self) -> str:
        return "deno task build || true"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f"bash -c 'cd /home/{REPO_DIR}; {self._build_cmd()}; {self._test_cmd()}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            f"bash -c 'cd /home/{REPO_DIR}; "
            f"git apply --whitespace=nowarn /home/test.patch; "
            f"{self._build_cmd()}; {self._test_cmd()}'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            f"bash -c 'cd /home/{REPO_DIR}; "
            f"git apply --whitespace=nowarn /home/test.patch /home/fix.patch; "
            f"{self._build_cmd()}; {self._test_cmd()}'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # Deno test runner output format:
        #   Test name ... ok (Xms)
        #   Test name ... FAILED (Xms)
        #   Test name ... ignored (Xms)
        re_pass = re.compile(r"^(.+?)\s+\.\.\.\s+ok(?:\s|$)")
        re_fail = re.compile(r"^(.+?)\s+\.\.\.\s+FAILED(?:\s|$)")
        re_skip = re.compile(r"^(.+?)\s+\.\.\.\s+ignored(?:\s|$)")

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).strip()
            if not clean:
                continue

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1).strip())
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(fail_match.group(1).strip())
                continue

            skip_match = re_skip.match(clean)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
