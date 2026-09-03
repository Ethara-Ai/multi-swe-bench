"""lobehub/lobehub config for PRs 71-6452 (single-app era, vitest)."""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest file-level metadata: (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class LobeHubImageBaseEarly(Image):
    """Base image for lobehub early era (PRs 71-6452, single Next.js app)."""

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-early"

    def workdir(self) -> str:
        return "base-early"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Shared base for the whole era: clones the repo ONCE so the 1.2 GB clone
        # layer is not duplicated across every per-PR image. It deliberately does
        # NOT check out ``${BASE_COMMIT}`` and does NOT prune git history: a single
        # ``base-early`` tag is shared by every PR in this era, but each PR has a
        # different ``base.sha``, so pinning here would strip every other PR's
        # commit out of history. The per-PR checkout + hardening run in
        # ``prepare.sh``, which executes as a build layer of the per-PR image.
        #
        # Two DockerfileEnhancer interactions to keep in mind (image.py):
        #   * ``_standardize_repo_fetch`` rewrites a hardcoded ``git clone <url>``
        #     into a BASE_COMMIT-pinned sequence. Its Pattern-2 regex carries the
        #     negative lookahead ``(?!"\$\{REPO_URL\}")``, so writing the clone
        #     against the literal ``"${REPO_URL}"`` (injected as an ARG by the
        #     infra block) leaves it untouched.
        #   * ``_inject_final_sanitize`` appends a BASE_COMMIT-pinned hardening
        #     block to any Dockerfile that mentions ``git clone`` -- unless the
        #     content already carries the hardening marker line before its CMD.
        #     The comment below supplies that marker, so the shared base stays
        #     unpinned. It must sit AFTER the clone: the enhancer re-injects if a
        #     clone/fetch appears between the marker and the CMD.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git libvips-dev && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# History hardening is deferred to prepare.sh, which runs per-PR and ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to a single PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class LobeHubImageDefaultEarly(Image):
    """PR-specific image for lobehub early era."""

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
        return LobeHubImageBaseEarly(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

if [ "$PKG_MANAGER" = "pnpm" ]; then
    PNPM_VERSION=$(node -e "try {{ const pm = require('./package.json').packageManager; console.log(pm.split('@')[1]); }} catch(e) {{ console.log('latest'); }}")
    npm install -g "pnpm@${{PNPM_VERSION}}"
    pnpm install --no-frozen-lockfile || true
else
    npm install --legacy-peer-deps || true
fi
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

if [ "$PKG_MANAGER" = "pnpm" ]; then
    pnpm vitest run --reporter=verbose 2>&1 || true
else
    npx vitest run --reporter=verbose 2>&1 || true
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

set +e
if [ "$PKG_MANAGER" = "pnpm" ]; then
    pnpm vitest run --reporter=verbose 2>&1 || true
else
    npx vitest run --reporter=verbose 2>&1 || true
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

PKG_MANAGER=$(node -e "try {{ const pm = require('./package.json').packageManager; if (pm && pm.startsWith('pnpm@')) console.log('pnpm'); else console.log('npm'); }} catch(e) {{ console.log('npm'); }}")

set +e
if [ "$PKG_MANAGER" = "pnpm" ]; then
    pnpm vitest run --reporter=verbose 2>&1 || true
else
    npx vitest run --reporter=verbose 2>&1 || true
fi
""".format(repo=self.pr.repo),
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

        # The repo is cloned once in the shared base image, so this layer does
        # NOT clone (and needs no ``REPO_URL``): it only checks out this PR's
        # commit in the inherited working tree.
        #
        # This per-PR image chains to a base *Image* (not a string), so
        # DockerfileEnhancer returns this dockerfile verbatim and does NOT
        # auto-inject git-history hardening. We therefore check out
        # ``${BASE_COMMIT}`` and apply ``Image._HARDENING_BLOCK`` manually so
        # the fix / future commits cannot be read out of git history (reward
        # hacking). ``BASE_COMMIT`` is pinned to *this* PR's ``base.sha``, which
        # also prunes the full history inherited from the shared base.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("lobehub", "lobehub_6452_to_71")
class LOBEHUB_6452_TO_71(Instance):
    """Instance for lobehub PRs 71-6452 (single-app era, vitest)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LobeHubImageDefaultEarly(self.pr, self._config)

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

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Vitest test-level pass: ✓ or ✔
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                passed_tests.add(name)
                continue

            # Vitest test-level fail: × or ✕ or ✗
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest file-level FAIL
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                failed_tests.add(name)
                continue

            # Vitest skipped: ↓ or ○
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                name = _clean_test_name(m.group(1))
                skipped_tests.add(name)
                continue

        # Dedup: worst wins
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
