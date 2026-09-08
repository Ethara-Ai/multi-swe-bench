"""openclaw/openclaw config.

A Node/TypeScript agent gateway (the project ships as `clawdbot`, then
`moltbot`, then `openclaw` across this dataset) laid out as a pnpm workspace:
the root package plus `ui`, `packages/*` and `extensions/*`. Tests are vitest 4
run from source -- no build step -- against `src/**/*.test.ts`,
`extensions/**/*.test.ts` and `test/format-error.test.ts`.

Covers the ten PRs in input/openclaw__openclaw_raw_dataset.jsonl -- 1437, 1450,
1535, 1624, 2509, 2649, 5042, 5405, 7473 and 7610. All ten target `main` and
carry neither `tag` nor `number_interval`, so Instance.create() looks every one
of them up under the single key "openclaw/openclaw" registered at the bottom of
this file.

All ten PRs share one base image, tagged `base`: it carries the node toolchain,
the apt packages and one clone of the repo, none of which differ per PR, so
tagging it per PR would build the same layers -- and re-clone -- ten times. That
shared layer keeps the clone unpinned and its history intact; each `pr-<number>`
image checks its own base commit out of that history and then applies
Image._HARDENING_BLOCK itself, so every PR image still ends up detached at its
own commit with the remote gone, every other ref deleted and nothing after the
base commit readable. The dataset is eleven images: one base and ten PR images.

The ten base commits span 2026-01-22 to 2026-02-13, and the repo's own test
entry point moves underneath them: `pnpm test` is `vitest run` at the two
oldest commits and `node scripts/test-parallel.mjs` from 1535 onward. The run
scripts therefore drive vitest directly, off the config files that are stable,
and mirror what .github/workflows/ci.yml runs at each commit:

    vitest run --config vitest.unit.config.ts        # when the split exists
    vitest run --config vitest.extensions.config.ts
    vitest run --config vitest.gateway.config.ts
    vitest run --config vitest.config.ts             # older commits: one lane
    vitest run --config vitest.e2e.config.ts <file>  # see the e2e note below

The union of the unit/extensions/gateway lanes is exactly the include list of
vitest.config.ts -- vitest.unit.config.ts is the base config minus
`src/gateway/**` and `extensions/**`, and the other two configs are those two
trees. Running them as separate processes is not cosmetic: test-parallel.mjs
keeps the gateway suite off the shared worker pool because those tests bind
sockets, and folding them back into one invocation reintroduces the flakiness
the repo split them out to avoid. A full pass over those three lanes takes
about 12 minutes in this image (792 + 73 + 33 files).

The e2e lane is the exception, and it is deliberately narrow. Nothing in
.github/workflows runs `pnpm test:e2e`, and a full pass over
vitest.e2e.config.ts in a container bears that out: 19 of its 52 files fail on
`Hook timed out in 10000ms` while a gateway binds its socket, and they take
another ~9 minutes to do it. Timing-dependent failures are worse than useless
here -- one that happens to pass in the fix stage and time out in the run stage
manufactures a fail-to-pass out of nothing. So the lane runs only the
`*.e2e.test.ts` files the PR's own test patch touches, which across this
dataset means PR 5042 alone
(src/config/config.legacy-config-detection.rejects-routing-allowfrom.e2e.test.ts,
a pure config-validation suite that exists at its base commit). Every other
PR's graded universe is the three lanes above, which is exactly what CI runs.

No build, no `pnpm build`, no dist/: the `checks` job in ci.yml installs and
runs vitest, and every fix patch in this dataset is TypeScript that vitest
transforms on the fly. `pnpm canvas:a2ui:bundle` runs in prepare.sh because CI
runs it before the test lanes, but it is best-effort -- src/canvas-host tests
write their own stub bundle when the real one is missing.

None of the ten test patches deletes a test file and none of the twenty patches
touches package.json or pnpm-lock.yaml, so there is no retired-suite pruning
and no post-patch reinstall: the dependency tree prepare.sh installs is the one
all three stages run against.

Only PR 1624 reaches into the `ui` workspace, whose suites all run in a real
chromium through @vitest/browser-playwright, so that browser is installed for
that PR alone. PR 1450 is the one PR whose fix patch does not apply to its own
base commit, conflicting on CHANGELOG.md and nothing else; fix-run.sh retries
without that file, which no test or source file reads.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_UI_PATH_RE = re.compile(r"^diff --git a/ui/", re.MULTILINE)

_E2E_TARGET_RE = re.compile(
    r"^diff --git a/(\S+\.e2e\.test\.[cm]?[jt]sx?) b/", re.MULTILINE
)


def _touches_ui(pr: PullRequest) -> bool:
    return bool(
        _UI_PATH_RE.search(pr.test_patch or "")
        or _UI_PATH_RE.search(pr.fix_patch or "")
    )


def _e2e_targets(pr: PullRequest) -> list[str]:
    return sorted(set(_E2E_TARGET_RE.findall(pr.test_patch or "")))


class OpenclawImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # One `base` tag serves all ten PRs, so this layer clones the repo once
        # and deliberately does NOT check out ${BASE_COMMIT} or prune history:
        # the ten PRs sit on ten different commits, and pinning here would strip
        # nine of them out of the shared clone. The per-PR checkout and the
        # hardening run in OpenclawImageDefault.dockerfile(), off the full
        # history this layer keeps.
        #
        # Two DockerfileEnhancer behaviours (image.py) keep that true:
        #   * _standardize_repo_fetch rewrites a hardcoded `git clone <url>`
        #     into a BASE_COMMIT-pinned sequence, but its Pattern-2 regex skips a
        #     clone written against the literal "${REPO_URL}" -- the ARG the
        #     infra block injects. Its Pattern 1 rewrites `COPY <repo>
        #     /home/<repo>` the same way, which is why this clones
        #     unconditionally instead of honouring config.need_clone: the COPY
        #     form cannot reach the build unpinned.
        #   * _inject_final_sanitize appends a BASE_COMMIT-pinned hardening block
        #     to any Dockerfile mentioning a clone, unless the content already
        #     carries the hardening marker line before its CMD. The comment below
        #     supplies that marker, and has to sit AFTER the clone -- the
        #     enhancer re-injects if a clone appears between the two.
        return f"""FROM {image_name}

{self.global_env}

ENV DO_NOT_TRACK=1
ENV OPENCLAW_TELEMETRY_DISABLED=1
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git python3 make g++ ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable

# --shallow-since keeps every commit the dataset touches -- the ten base commits
# span 2026-01-22 to 2026-02-13 -- while dropping the pre-2026 history, which cuts
# the clone from a 1.5 GB+ pack to a fraction of it. Nothing is lost downstream:
# the per-PR hardening deletes every ref and repacks, so everything outside
# HEAD's ancestry is discarded from the graded image anyway.
#
# GitHub drops this repo's pack mid-transfer often enough to matter -- two
# builds died at ~30s in on "RPC failed; curl 56 GnuTLS recv error" /
# "fatal: early EOF". HTTP/1.1 keeps the transfer off the HTTP/2 multiplexed
# path those decode errors come from, and the retry loop absorbs the rest. One
# clone now feeds all ten PR images, so paying for retries here is cheap. The
# clone is not a bare `RUN git clone <url> /home/<repo>` line, so it is invisible
# to DockerfileEnhancer._standardize_repo_fetch either way.
RUN git config --global http.version HTTP/1.1 \\
    && git config --global http.postBuffer 524288000 \\
    && for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{self.pr.repo}; \\
        if git clone --shallow-since=2026-01-01 "${{REPO_URL}}" /home/{self.pr.repo}; then break; fi; \\
        echo "clone attempt ${{attempt}} failed; retrying in 15s" >&2; \\
        sleep 15; \\
    done \\
    && test -d /home/{self.pr.repo}/.git

# History hardening is deferred to the per-PR image, which ends with
# test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
# Keep that marker here so DockerfileEnhancer._inject_final_sanitize does not
# pin this shared base to one PR's BASE_COMMIT.

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenclawImageDefault(Image):
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
        return OpenclawImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if _touches_ui(self.pr):
            ui_setup = """
if corepack pnpm --dir ui exec playwright install --with-deps chromium; then
    touch /home/.openclaw-ui-lane
else
    echo "prepare: chromium unavailable; the ui lane will be skipped" >&2
fi
"""
        else:
            ui_setup = ""

        e2e_targets = _e2e_targets(self.pr)
        if e2e_targets:
            e2e_lane = """
if [ -f vitest.e2e.config.ts ] && grep -q 'e2e\\.test\\.ts' vitest.config.ts; then
    run_lane e2e vitest_run --config vitest.e2e.config.ts {targets}
fi
""".format(targets=" ".join(f"'{path}'" for path in e2e_targets))
        else:
            e2e_lane = ""

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The per-PR image checks this commit out of the shared base's full history
# before this script runs, so this is an assertion rather than a checkout.
test "$(git rev-parse HEAD)" = "{base_sha}"
bash /home/check_git_changes.sh

corepack enable
node --version
corepack pnpm --version

export CI=true

corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true || \\
    corepack pnpm install --frozen-lockfile --ignore-scripts=false \\
        --config.engine-strict=false --config.enable-pre-post-scripts=true

corepack pnpm canvas:a2ui:bundle || \\
    echo "prepare: a2ui bundle failed; the suites stub it themselves" >&2
{ui_setup}""".format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha, ui_setup=ui_setup
                ),
            ),
            File(
                ".",
                "run-suites.sh",
                """#!/bin/bash
set -uo pipefail

export CI=true
cd /home/{repo}

vitest_run() {{
    corepack pnpm exec vitest run --reporter=verbose --silent=passed-only "$@"
}}

run_lane() {{
    label="$1"
    shift
    echo "===== openclaw-lane: ${{label}} ====="
    "$@" || echo "===== openclaw-lane-failed: ${{label}} (exit $?) ====="
}}

if [ -f vitest.unit.config.ts ] && [ -f vitest.extensions.config.ts ] \\
        && [ -f vitest.gateway.config.ts ]; then
    run_lane unit vitest_run --config vitest.unit.config.ts
    run_lane extensions vitest_run --config vitest.extensions.config.ts
    run_lane gateway vitest_run --config vitest.gateway.config.ts
else
    run_lane unit vitest_run --config vitest.config.ts
fi
{e2e_lane}
if [ -f /home/.openclaw-ui-lane ]; then
    run_lane ui corepack pnpm --dir ui exec vitest run \\
        --config vitest.config.ts --reporter=verbose --silent=passed-only
fi

exit 0
""".format(repo=self.pr.repo, e2e_lane=e2e_lane),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "fix-run: fix.patch conflicts; retrying without CHANGELOG.md" >&2
    if ! git apply --whitespace=nowarn --exclude=CHANGELOG.md /home/fix.patch; then
        echo "Error: git apply fix.patch failed" >&2
        exit 1
    fi
fi

bash /home/run-suites.sh
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("OpenclawImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The clone lives in the shared base, so this layer only checks out this
        # PR's commit in the inherited working tree -- no clone, no REPO_URL.
        # Chaining to a base *Image* (not a string) makes DockerfileEnhancer
        # return this dockerfile verbatim, so Image._HARDENING_BLOCK is applied
        # by hand: it detaches at BASE_COMMIT, drops the remote and deletes every
        # other ref, which both removes the nine other PRs' commits and puts the
        # fix commit and everything after it out of reach of a reward-hacking
        # agent. BASE_COMMIT is baked in as a literal default because the harness
        # only passes it as a build arg to images whose dependency is a string.
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


@Instance.register("openclaw", "openclaw")
class Openclaw(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawImageDefault(self.pr, self._config)

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

        re_lane = re.compile(r"^=+\s*openclaw-lane:\s*(\S+)\s*=+$")

        timing = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s|m))?"
        re_pass = re.compile(rf"^\s*[✓✔]\s+(.+?){timing}\s*$")
        re_fail = re.compile(rf"^\s*[×✕✗]\s+(.+?){timing}\s*$")
        re_skip = re.compile(rf"^\s*[↓○◌]\s+(.+?){timing}\s*$")

        re_test_name = re.compile(r"\.test\.[cm]?[jt]sx?\s+>\s+\S")

        lane = ""

        def qualify(name: str) -> str:
            name = re.sub(r"\s+", " ", name).strip()
            return f"ui > {name}" if lane == "ui" else name

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip()

            banner = re_lane.match(line.strip())
            if banner:
                lane = banner.group(1)
                continue

            m = re_fail.match(line)
            if m and re_test_name.search(m.group(1)):
                failed_tests.add(qualify(m.group(1)))
                continue

            m = re_pass.match(line)
            if m and re_test_name.search(m.group(1)):
                passed_tests.add(qualify(m.group(1)))
                continue

            m = re_skip.match(line)
            if m and re_test_name.search(m.group(1)):
                skipped_tests.add(qualify(m.group(1)))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
