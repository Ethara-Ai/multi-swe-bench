import copy
import json
import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_JSON_BEGIN = "##### MSWEB-JEST-JSON-BEGIN"
_JSON_END = "##### MSWEB-JEST-JSON-END"


def _normalise_identity(name: str) -> str:
    """Collapse a test id to printable ASCII so encoding noise cannot fork it.

    Stage logs are captured in chunks; a multi-byte UTF-8 sequence split across a
    chunk boundary decodes to U+FFFD, and the split lands at a different offset in
    each stage. The SAME test then acquires a different name per stage and
    Report.__post_init__ unions them as two entries -- one showing NONE where its
    other spelling appeared, which is the Rule 4 anomaly path.

    Dropping non-ASCII and collapsing whitespace makes both spellings converge,
    because the hole left behind is the same width either way.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", name)).strip()


# ---------------------------------------------------------------- shared base
# One base image for the era, pinned to the NEWEST base commit among the PRs it
# serves (PR 10680, 2020-02-26) rather than to whichever PR builds first.
#
# The enhancer appends `git checkout ${BASE_COMMIT}` and then scrubs history down
# to it, asserting
#     test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# so the image holds ONLY commits reachable from BASE_COMMIT, and a later PR can
# check out its own commit only if that commit is an ANCESTOR of the pinned one.
# build_dataset.py:629 takes BASE_COMMIT from `image.pr.base.sha`, so a static tag
# without anchoring makes correctness depend on build order. These five PRs run
# oldest-first in the JSONL (10198 2019-07-11 ... 10680 2020-02-26), so the base
# would have been pinned to 10198 and the other FOUR checkouts would have failed.
#
# Verified with `git merge-base --is-ancestor` against a local clone: 10198,
# 10217, 10447 and 10599 are all ancestors of 10680.
#
# CONSTRAINT: the anchor must stay the newest commit in the range this era serves.
# A PR with a newer base commit will not exist in the scrubbed image -- that fails
# loudly in prepare.sh's `git checkout`, never silently on the wrong tree; move the
# anchor (and the tag) forward when adding one.
_ERA_ANCHOR_SHA = "e9ea523c5bd0d76c5966489f8923695ef619adbf"  # PR 10680
_ERA_RANGE = "10680-to-10198"


def _anchor_pr(pr: PullRequest) -> PullRequest:
    """Copy of ``pr`` whose ``base.sha`` is the era anchor (shared ImageBase only)."""
    anchored = copy.deepcopy(pr)
    anchored.base.sha = _ERA_ANCHOR_SHA
    return anchored


# Era 3: yarn classic + jest, node:12-buster-slim (Debian Buster)
# yarn.lock v1 format, jest in devDeps, lerna for bootstrapping
# PRs #7358-#11973 (master, main, feature branches)
# WORKAROUND: deleted git dep @lerna/collect-updates must be removed from resolutions
# Test output (jest --verbose --ci):
#   PASS packages/.../test/index.js
#   ✓ test name (Xms)     — pass
#   ✕ test name (Xms)     — fail
#   ○ skipped test name    — skip


class BabelClassicJestImageBase(Image):
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
        # FULL buster image, not -slim. The base Dockerfile must clone the repo
        # BEFORE prepare.sh exists, so git and a real CA bundle have to be present
        # already. Measured: node:12-buster-slim has git=MISSING and no
        # /etc/ssl/certs/ca-certificates.crt at all, which made `git clone` die with
        #     server certificate verification failed. CAfile: none
        # The full variant (buildpack-deps lineage) ships git, ca-certificates, make
        # and python3, so no apt layer is needed and the base Dockerfile stays the
        # minimal FROM / WORKDIR / clone shape the enhancer expects.
        return "node:12-buster"

    def image_tag(self) -> str:
        # Range-named shared base, the established form in this tree (70 configs
        # use `base-<hi>-to-<lo>`). The HIGH end is the anchor commit, so the name
        # states its own validity: every PR it claims to serve is <= the anchor and
        # therefore an ancestor whose commit survives the scrub.
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        parts = [f"FROM {image_name}"]
        if self.global_env:
            parts.append(self.global_env)
        parts.append("WORKDIR /home/")
        parts.append(code)
        if self.clear_env:
            parts.append(self.clear_env)
        return "\n".join(parts) + "\n"


class BabelClassicJestImageDefault(Image):
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
        # Anchored: all PRs in the era share one base pinned to the newest commit.
        return BabelClassicJestImageBase(_anchor_pr(self.pr), self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# package.json pins @lerna/collect-updates to a git branch on a Babel fork. That
# ref still resolves for most of this era but NOT for every PR: at PR 10198 it
# points at nicolo-ribaudo/lerna#babel-collect-updates, whose branch no longer
# exists, and yarn then fails the entire install. Strip the resolution ONLY if the
# first install actually fails, so the common case keeps the upstream dependency
# graph intact instead of editing package.json unconditionally.
if ! yarn install --ignore-engines --frozen-lockfile; then
    # Strip the resolution from package.json...
    sed -i '/@lerna.*collect-updates/d' package.json || true
    # ...AND the matching yarn.lock block, which is the part that actually binds.
    # The lock entry is keyed for BOTH the plain semver and the git URL:
    #   "@lerna/collect-updates@3.14.2", "@lerna/collect-updates@https://...lerna.git#babel-collect-updates":
    #     resolved "https://github.com/nicolo-ribaudo/lerna.git#89eab830be04..."
    # so lerna's own `@lerna/collect-updates@^3.14.2` dependency still resolves to
    # the dead git ref even after package.json is cleaned, and yarn fails with
    #   Extracting tar content of undefined failed
    # This sed deletes the header line and its indented body, stopping at the next
    # top-level entry, so yarn re-resolves the package from the npm registry.
    sed -i '/collect-updates@https/,/^[^[:space:]]/ {{ /^[^[:space:]]/!d; /collect-updates@https/d }}' yarn.lock || true
    yarn install --ignore-engines || true
fi
if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi

# Restore whatever the fallback edited. node_modules and build output are
# gitignored; package.json and yarn.lock are NOT, so without this the tree is left
# dirty and the assertion below fails after a perfectly good install.
git checkout -- package.json yarn.lock 2>/dev/null || true

# Hard assertions. These -- not the installer's exit code -- decide whether the
# environment is usable, since the installs above end in `|| true` to tolerate the
# native-addon failures that are common and benign on arm64.
node -e "require.resolve('jest')"
node -e "require.resolve('babel-jest')"
test -f Makefile

# Deliberately last, with no `exit 0` after it: this script's exit status IS the
# clean-tree check.
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export BABEL_ENV=test
cd /home/{pr.repo}
sed -i '/@lerna.*collect-updates/d' package.json || true
sed -i '/nicolo-ribaudo\\/lerna/d' package.json || true
yarn install --ignore-engines || true
if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi
make build || true
jest_status=0
rm -f /tmp/msweb-jest.json
BABEL_ENV=test node node_modules/jest/bin/jest.js --maxWorkers=4 --ci --json --outputFile=/tmp/msweb-jest.json || jest_status=$?
echo "##### MSWEB-JEST-EXIT: $jest_status"
echo '##### MSWEB-JEST-JSON-BEGIN'
cat /tmp/msweb-jest.json 2>/dev/null || true
echo
echo '##### MSWEB-JEST-JSON-END'
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export BABEL_ENV=test
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
sed -i '/@lerna.*collect-updates/d' package.json || true
sed -i '/nicolo-ribaudo\\/lerna/d' package.json || true
yarn install --ignore-engines || true
if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi
make build || true
jest_status=0
rm -f /tmp/msweb-jest.json
BABEL_ENV=test node node_modules/jest/bin/jest.js --maxWorkers=4 --ci --json --outputFile=/tmp/msweb-jest.json || jest_status=$?
echo "##### MSWEB-JEST-EXIT: $jest_status"
echo '##### MSWEB-JEST-JSON-BEGIN'
cat /tmp/msweb-jest.json 2>/dev/null || true
echo
echo '##### MSWEB-JEST-JSON-END'

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export BABEL_ENV=test
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
sed -i '/@lerna.*collect-updates/d' package.json || true
sed -i '/nicolo-ribaudo\\/lerna/d' package.json || true
yarn install --ignore-engines || true
if [ -f node_modules/.bin/lerna ]; then
    ./node_modules/.bin/lerna bootstrap -- --ignore-engines || true
fi
make build || true
jest_status=0
rm -f /tmp/msweb-jest.json
BABEL_ENV=test node node_modules/jest/bin/jest.js --maxWorkers=4 --ci --json --outputFile=/tmp/msweb-jest.json || jest_status=$?
echo "##### MSWEB-JEST-EXIT: $jest_status"
echo '##### MSWEB-JEST-JSON-BEGIN'
cat /tmp/msweb-jest.json 2>/dev/null || true
echo
echo '##### MSWEB-JEST-JSON-END'

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        parts = [f"FROM {name}:{tag}"]
        if self.global_env:
            parts.append(self.global_env)
        if proxy_setup:
            parts.append(proxy_setup)
        parts.append(copy_commands)
        parts.append(prepare_commands)
        if proxy_cleanup:
            parts.append(proxy_cleanup)
        if self.clear_env:
            parts.append(self.clear_env)
        return "\n".join(parts) + "\n"


@Instance.register("babel", "babel_classic_jest")
class babel_classic_jest(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BabelClassicJestImageDefault(self.pr, self._config)

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
        """Classify every assertion from jest's ``--json`` report.

        Replaces a line-scraping reporter parse. The old approach matched both
        ``PASS <path>`` (a *suite header*) and ``✓ <name>`` (an actual test)
        into the same set, so files and tests shared one namespace and a file
        could collide with a test of the same name. It also could not distinguish
        a suite that FAILED TO LOAD -- jest prints ``FAIL <path>`` with no
        ``✗`` lines for those -- from a suite whose tests merely failed.

        jest ``--json`` gives the two facts separately and unambiguously::

            testResults[].name                    -> /home/babel/packages/.../x.js
            testResults[].assertionResults[]
                .fullName                         -> "describe > it does a thing"
                .status                           -> passed | failed | pending | todo

        which is exactly the ``<source file>::<test name>`` identity this project
        requires, with no regex fragility and no ANSI dependence.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        text = _ANSI_ESCAPE.sub("", test_log or "")
        start = text.rfind(_JSON_BEGIN)
        end = text.rfind(_JSON_END)
        if start == -1 or end == -1 or end <= start:
            # jest never got far enough to write a report. An empty TestResult is
            # the honest answer: Report.check() then rejects the stage rather than
            # a partial parse inventing passes.
            return TestResult(
                passed_count=0, failed_count=0, skipped_count=0,
                passed_tests=set(), failed_tests=set(), skipped_tests=set(),
            )

        try:
            report = json.loads(text[start + len(_JSON_BEGIN):end].strip())
        except (ValueError, TypeError):
            return TestResult(
                passed_count=0, failed_count=0, skipped_count=0,
                passed_tests=set(), failed_tests=set(), skipped_tests=set(),
            )

        prefix = f"/home/{self.pr.repo}/"
        for suite in report.get("testResults") or []:
            path = (suite.get("name") or "").replace("\\", "/")
            idx = path.find(prefix)
            rel = path[idx + len(prefix):] if idx != -1 else path.lstrip("/")

            cases = suite.get("assertionResults") or []

            # A suite that fails to LOAD (missing module, syntax error, bad
            # import) reports status "failed" with an EMPTY assertionResults list.
            # Counting only assertions renders it invisible: the stage reports its
            # surviving tests as passing and zero failures, so a catastrophically
            # broken environment looks healthy. Record one synthetic failure keyed
            # on the file so Report.check() can see it.
            if not cases and suite.get("status") == "failed":
                failed.add(_normalise_identity(f"{rel}::<test suite failed to run>"))
                continue

            for case in cases:
                name = case.get("fullName") or case.get("title") or ""
                if not name:
                    continue
                ident = _normalise_identity(f"{rel}::{name}")
                status = case.get("status")
                if status == "passed":
                    passed.add(ident)
                elif status == "failed":
                    failed.add(ident)
                else:  # pending / todo / disabled / skipped
                    skipped.add(ident)

        # A name can never occupy two buckets; failure wins over a retry's pass.
        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
