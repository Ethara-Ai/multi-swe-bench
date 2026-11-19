import dataclasses
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
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


def _split_sections(patch: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split a unified diff into (preamble_lines, [(path, section_text), ...])."""
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
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

    return preamble, [(path, "".join(lines)) for path, lines in sections]


# A co-located unit test, by filename convention: `foo.spec.ts`, `foo.test.tsx`,
# `foo.spec.mts`, ... Anchored on the basename so a *source* file that merely
# lives under a directory containing "spec" is not caught.
_SPEC_FILE = re.compile(r"(^|/)[^/]+\.(spec|test)\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$")

# Paths the upstream collector already routes to the test patch, by substring.
_COLLECTOR_TEST_WORDS = ("test", "tests", "e2e", "testing")


def _is_collector_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(word in lowered for word in _COLLECTOR_TEST_WORDS)


def _rebalance_split(fix_patch: str, test_patch: str) -> tuple[str, str]:
    """Move co-located `*.spec.ts` files from the fix patch to the test patch.

    The upstream collector classifies a file as test code only when its *path*
    contains "test", "tests", "e2e" or "testing"
    (multi_swe_bench/collect/build_dataset.py). Trilium co-locates its vitest
    specs beside the source they cover using the `*.spec.ts` convention, e.g.
    `apps/client/src/services/search.spec.ts`. That path contains none of those
    substrings, so every such spec is misrouted into the fix patch, and only
    `vitest.config.mts` (which contains "test") and the Playwright `e2e/`
    directories land in the test patch.

    The result is an inverted test/fix split that makes resolution impossible:

      * The test patch adds no test vitest can collect, so the `run` and `test`
        stages are byte-for-byte identical and nothing transitions fail -> pass.
      * The fix patch supplies both the implementation *and* the specs that
        score it, so every credited test trips `Report.check()`'s cheating
        guard (rule 5) - correctly, since as recorded the fix really is
        self-satisfying.

    Restoring the specs to the test patch gives the harness the split the PR
    actually has: tests introduced by the test patch, implementation by the fix
    patch. Only whole per-file diff sections are moved between the two patches;
    no hunk is rewritten and nothing is dropped, so applying test.patch then
    fix.patch reproduces exactly the same tree as before.
    """
    fix_pre, fix_sections = _split_sections(fix_patch)
    test_pre, test_sections = _split_sections(test_patch)

    moved = [
        (path, body)
        for path, body in fix_sections
        if _SPEC_FILE.search(path) and not _is_collector_test_path(path)
    ]
    if not moved:
        return fix_patch, test_patch

    moved_paths = {path for path, _ in moved}
    kept_fix = [(p, b) for p, b in fix_sections if p not in moved_paths]

    new_fix = "".join(fix_pre) + "".join(body for _, body in kept_fix)
    new_test = (
        "".join(test_pre)
        + "".join(body for _, body in test_sections)
        + "".join(body for _, body in moved)
    )
    return new_fix, new_test



def _rebalanced_pr(pr: PullRequest) -> PullRequest:
    """Return `pr` with the co-located spec files moved test-ward.

    Returns a copy when the split actually changes, so the caller's object is
    left alone; returns `pr` unchanged when there is nothing to move.
    """
    fix_patch, test_patch = _rebalance_split(pr.fix_patch, pr.test_patch)
    if fix_patch == pr.fix_patch and test_patch == pr.test_patch:
        return pr
    return dataclasses.replace(pr, fix_patch=fix_patch, test_patch=test_patch)



class TriliumImageBase(Image):

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
        return "base-pnpm12-vitest5"

    def workdir(self) -> str:
        return "base-pnpm12-vitest5"

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
    ca-certificates curl git python3 build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@12.10.1
RUN pnpm --version

{self.clear_env}

"""


class TriliumImageDefault(Image):

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
        return TriliumImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # `self.pr` is already rebalanced for the instance path (see
        # TRILIUM_12023_TO_11311.__init__); `_rebalance_split` is idempotent, so
        # applying it here too keeps the image correct when an image is built
        # directly from a raw PullRequest, without double-moving anything.
        fix_patch, test_patch = _rebalance_split(
            self.pr.fix_patch, self.pr.test_patch
        )
        filtered_fix_patch = _sanitize_patch(fix_patch)
        filtered_test_patch = _sanitize_patch(test_patch)

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
git cat-file -e {pr.base.sha} 2>/dev/null \\
    || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

pnpm install --ignore-scripts || true

pnpm exec vitest --version
test -f vitest.config.ts
echo DEPS_OK

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
rc=0
cd /home/Trilium
PKGS=$(pnpm -r exec node -e \\
    "const p=require('./package.json'); if(p.scripts&&p.scripts.test) console.log(p.name)" \\
    2>/dev/null | grep -v '^$' | grep -v '@triliumnext/ckeditor5' | sort)
for PKG in $PKGS; do
    echo "===== PACKAGE: $PKG ====="
    pnpm --filter="$PKG" exec vitest run \\
        --pool=forks \\
        --maxWorkers=4 \\
        --testTimeout=30000 --hookTimeout=30000 \\
        --reporter=verbose \\
        || rc=$?
done
exit $rc

""",
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
ARG BASE_COMMIT="{self.pr.base.sha}"
RUN git config --global --add safe.directory /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

WORKDIR /home

{self.clear_env}

"""


@Instance.register("TriliumNext", "Trilium_12023_to_11311")
class TRILIUM_12023_TO_11311(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        # Correct the collector's inverted test/fix split once, here, so that
        # BOTH the patch files baked into the image (TriliumImageDefault.files)
        # and Report.check()'s own bookkeeping see the same split.
        #
        # Report.build() passes `instance.pr.test_patch` / `instance.pr.fix_patch`
        # straight to Report (harness/report.py:605), which derives
        # `test_patch_files` / `fix_patch_files` from them
        # (harness/report.py:105-108). Rebalancing only inside `files()` would
        # fix what runs in the container while leaving the report still
        # believing the fix patch owns every spec file -- so rule 5's cheating
        # guard would keep rejecting the credited tests. Rebalancing the
        # PullRequest itself keeps the two in agreement.
        self._pr = _rebalanced_pr(pr)
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TriliumImageDefault(self.pr, self._config)

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

        re_root = re.compile(r"RUN\s+v[\d.]+\s+(/home/\S+)")
        re_result = re.compile(
            r"^[\s|]*([✓×✗↓○])\s+"
            r"(?:\|[^|]+\|\s+)?"
            r"(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts)\s+>\s+.*?)$"
        )
        re_trailing = re.compile(
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?"
            r"(?:\s+\((?:\d+\s+tests?|retry\s+x\d+|\d+\s+skipped)[^)]*\))?"
            r"(?:\s+\[skipped\])?$"
        )

        prefix = ""
        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue

            match = re_root.search(line)
            if match:
                root = match.group(1).rstrip("/")
                rel = root[len("/home/Trilium"):].strip("/")
                prefix = f"{rel}/" if rel else ""
                continue

            match = re_result.match(line)
            if not match:
                continue

            symbol, name = match.group(1), match.group(2)
            name = re_trailing.sub("", name).strip()
            if not name or " > " not in name:
                continue
            if prefix and not name.startswith(prefix):
                name = prefix + name

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


Instance.register("TriliumNext", "Trilium")(TRILIUM_12023_TO_11311)
