import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class VerdaccioImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # .nvmrc at the base commit pins 14, and .github/workflows/ci.yml runs
        # the matrix on 10/12/14 -- 14 is the newest version this tree was ever
        # proved on, and jest 26 / babel 7.12 predate the Node 16 ESM changes.
        return "node:14"

    def image_tag(self) -> str:
        # PR-scoped, not a bare "base". The tag is what the PR layer's FROM
        # resolves to (image_full_name() = image_name():image_tag()), so a
        # repo-wide "base" would have every verdaccio PR share one mutable
        # image. That is sharper here than elsewhere: dockerfile() below hard
        # codes a fetch of refs/pull/<N>/head, so a shared tag would let one PR
        # inherit an image built around a different pull request entirely.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Unlike most configs, the clone is written in the "${REPO_URL}" form on
        # purpose. base.sha 041977d3 is not reachable from any branch of
        # verdaccio/verdaccio -- it survives only as the parent of
        # refs/pull/2072/head -- so the checkout the enhancer would inject after a
        # plain clone fails with "reference is not a tree".
        # DockerfileEnhancer._standardize_repo_fetch guards its rewrite with
        # `(?!"\\$\\{REPO_URL\\}")` (harness/image.py:380), so writing the clone
        # this way leaves the fetch sequence below in our hands while every other
        # injection -- ARGs, proxy, CA certs, labels and the history-hardening
        # block -- still applies.
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n'
                f"\n"
                f"WORKDIR /home/{self.pr.repo}\n"
                f"\n"
                f'RUN git fetch origin "+refs/pull/{self.pr.number}/head:refs/heads/pr-{self.pr.number}" || true\n'
                f"RUN git reset --hard\n"
                f"RUN git checkout ${{BASE_COMMIT}}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # Deliberately no `apt-get`. node:14 is Debian buster, whose mirrors are
        # archived -- `apt-get update` returns 404 and would fail the build. The
        # full (non-slim) node image already ships everything needed here:
        # git 2.20.1, curl, python3, make and g++ for node-gyp.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN npm install -g pnpm@5.5.12

# The repo's own .npmrc pins `registry = https://registry.verdaccio.org` (the
# project dogfooded its own registry). That host no longer resolves, so every
# install dies on ENOTFOUND and node_modules never appears. Override via env
# rather than rewriting .npmrc: npm config precedence puts env above the
# project file, and editing the file would dirty the tree that
# check_git_changes.sh asserts is clean. The file's `always-auth = true` is
# left alone -- verified harmless, anonymous installs succeed with it set.
ENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/

{code}

{self.clear_env}

# Emitted explicitly, unlike every other config. DockerfileEnhancer normally
# appends this as part of _standardize_repo_fetch's clone replacement
# (harness/image.py:369) -- but the clone above is deliberately written in the
# "${{REPO_URL}}" form to escape that rewrite, so the CMD came with it and the
# image silently inherited node:14's own `CMD ["node"]`, dropping anyone who
# ran it into a JS REPL instead of a shell. Restoring it here also puts
# _inject_final_sanitize back on its normal path, where the history-hardening
# block lands just before the CMD rather than at the tail.
CMD ["/bin/bash"]
"""


class VerdaccioImageDefault(Image):
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
        return VerdaccioImageBase(self.pr, self.config)

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
                "install-mockdate.sh",
                """#!/bin/bash
set -e

# mockdate is a date-mocking test utility. The PR happens to add it in
# fix.patch (package.json + pnpm-lock.yaml) while test.patch is what imports
# it, so in the test stage the two spec files that `import MockDate` die at
# import time -- jest never names their cases, and every case in them lands in
# n2p (NONE -> PASS) instead of f2p (FAIL -> PASS). Providing it up front lets
# those suites load, so the cases fail on their real assertions instead.
#
# Installed by unpacking the tarball, NOT `pnpm add`: pnpm would rewrite
# package.json and pnpm-lock.yaml, and fix.patch edits exactly those two files
# -- `git apply` would then conflict. mockdate@3.0.2 has no dependencies
# (verified against the registry), so a bare unpack is complete.
#
# Placed in the workspace root node_modules, which Node's resolution reaches by
# walking up from packages/core/htpasswd/tests/. Must run *after* every
# `pnpm install`, since pnpm prunes node_modules entries absent from the
# lockfile.
cd /tmp
npm pack mockdate@3.0.2 --registry=https://registry.npmjs.org/ > /dev/null
mkdir -p /home/{pr.repo}/node_modules/mockdate
tar -xzf /tmp/mockdate-3.0.2.tgz -C /home/{pr.repo}/node_modules/mockdate --strip-components=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pnpm recursive install --registry=https://registry.npmjs.org/ || true

# htpasswd's src imports @verdaccio/commons-api and @verdaccio/file-locking,
# whose package.json `main` points at build/index.js -- unbuilt, every test
# file fails to resolve them. The `...` suffix builds the package together
# with its workspace dependencies, instead of the whole monorepo (which would
# also drag in the website).
pnpm --filter verdaccio-htpasswd... run build || true

bash /home/install-mockdate.sh || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# utils.test.ts evaluates HtpasswdHashAlgorithm.bcrypt at module scope (line 21,
# inside defaultHashConfig), and that enum exists only once fix.patch adds it to
# src/utils.ts. Without the fix the import throws, jest collects nothing, and all
# 30 of the file's cases report NONE for this stage -- 26 of them pre-existing
# passes that then land in p2p unverified rather than measured. The file exists
# at base.sha, so restoring that revision lets those 26 actually run.
# The snapshot has to go back with it: leaving test.patch's newer .snap against
# the older test body fails 4 assertions on snapshot mismatch alone, which would
# surface as bogus f2p. The 4 cases test.patch adds here are absent from the base
# revision, so they stay NONE and classify as n2p -- correct, since they cannot
# run without the fix either way.
git checkout HEAD -- packages/core/htpasswd/tests/utils.test.ts
git checkout HEAD -- packages/core/htpasswd/tests/__snapshots__/utils.test.ts.snap
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export BABEL_ENV=test

cd /home/{pr.repo}
# prepare.sh runs `pnpm recursive install`, and pnpm 5.5.12 re-resolves and
# re-serialises pnpm-lock.yaml in the working tree (369 insertions / 357
# deletions against the base commit). fix.patch carries a pnpm-lock.yaml hunk,
# and `git apply` is atomic -- that one stale hunk aborts the entire apply, so
# jest never runs and the fix stage reports (0, 0, 0). Restore just that file so
# the hunk lands on its expected context; the install below rewrites it anyway.
git checkout -- pnpm-lock.yaml
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm recursive install --registry=https://registry.npmjs.org/ || true
bash /home/install-mockdate.sh || true

cd /home/{pr.repo}/packages/core/htpasswd
export PATH="/home/{pr.repo}/node_modules/.bin:$PATH"
jest --verbose --runInBand --ci

""".format(pr=self.pr),
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


@Instance.register("verdaccio", "verdaccio")
class Verdaccio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VerdaccioImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Jest suite header, one per spec file:
        #     PASS tests/htpasswd.test.ts (5.2 s)
        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        # Verbose per-case lines, printed beneath their suite header:
        #       ✓ should create htpasswd file (12 ms)
        #       ✕ node version error
        #       ○ skipped some case
        #       ✎ todo some case
        # jest/config.js sets `verbose: false`, so the run scripts pass
        # `--verbose` explicitly. Without it only the suite headers survive and a
        # case flipping FAIL -> PASS inside an otherwise-passing file is invisible.
        re_case = re.compile(
            r"^\s*(?:(?P<pass>[✓✔])|(?P<fail>[✕✗×])|(?P<skip>[○◯])|(?P<todo>✎))\s+"
            r"(?:skipped\s+|todo\s+)?"
            r"(?P<name>.*?)"
            r"(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )

        # Case names are only the leaf `it(...)` title, so the same title in two
        # spec files would merge. Prefix each with its suite.
        current_suite = ""
        cases_in_suite = 0
        pending_suite = None  # (status, name) not yet credited to any case

        def flush_suite():
            # A suite that printed no case lines (transform error, empty file,
            # missing module) still carries a result worth keeping -- and for this
            # PR that matters: without the fix, `import MockDate from 'mockdate'`
            # cannot resolve and the whole file fails before any case runs.
            if pending_suite and cases_in_suite == 0:
                status, name = pending_suite
                if status == "FAIL":
                    failed_tests.add(name)
                else:
                    passed_tests.add(name)

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line)

            m = re_suite.match(line)
            if m:
                flush_suite()
                current_suite = m.group(2)
                cases_in_suite = 0
                pending_suite = (m.group(1), current_suite)
                continue

            m = re_case.match(line)
            if not m:
                continue

            name = m.group("name").strip()
            if not name:
                continue
            cases_in_suite += 1
            full_name = f"{current_suite}::{name}" if current_suite else name

            if m.group("fail"):
                failed_tests.add(full_name)
            elif m.group("skip") or m.group("todo"):
                skipped_tests.add(full_name)
            else:
                passed_tests.add(full_name)

        flush_suite()

        # Deduplicate - worst result wins.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
