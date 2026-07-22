import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    # Representative ref for the shared warm install. The dataset spans the
    # v0.17.5 .. v1.6.4 release lines (2021-2022); the repo's DEFAULT branch today
    # is Remix v2 / React Router era, whose dependency tree has nothing in common
    # with this era -- warming from it would populate the yarn cache with packages
    # no instance ever asks for. v1.3.0 (2022-03-16) sits mid-span, so the cache it
    # leaves behind is the one each PR's prepare.sh actually reuses.
    # NOTE: this is a cache-warming ref ONLY. It must never be treated as a pin:
    # prepare.sh does `git reset --hard` + `git checkout <pr base sha>` before
    # anything else, so the per-PR commit is what actually gets built and tested.
    WARM_REF = "v1.3.0"

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        # Tier 1 (this image, tag "base"): SHARED by every PR. NO BASE_COMMIT --
        # it only clones the repo and installs the common dependencies, so the
        # expensive dependency download happens ONCE. Tier 2 (ImageDefault, tag
        # pr-<n>) carries the per-PR BASE_COMMIT, checks out that SHA and installs
        # only what that commit additionally needs, reusing the cache from here.
        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            fetch = f"COPY {repo} /home/{repo}"

        # Warm the SHARED dependency cache. `|| true` because a stale lockfile that
        # no longer resolves must never fail the shared base build -- the per-PR
        # install in prepare.sh is the authoritative one.
        warm_install = (
            f"WORKDIR /home/{repo}\n"
            f"RUN git checkout {self.WARM_REF} && "
            f"(yarn install --frozen-lockfile || yarn install || true)"
        )

        # The `# syntax` directive makes DockerfileEnhancer.enhance() return this
        # VERBATIM, so the ARG/ENV/LABEL block below is written out by hand (the
        # enhancer would otherwise also inject proxy/cert/MITM args, and would
        # rewrite the clone into a single-BASE_COMMIT pin + history strip -- fatal
        # for a base image SHARED across PRs, which must keep full history so each
        # PR can check out its own base SHA).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git jq \\
    && rm -rf /var/lib/apt/lists/*

{fetch}

{warm_install}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageBaseNode20(ImageBase):
    """Node 20 base for release lines that reach into the Remix v2 era.

    Remix v2 declares `"engines": {"node": ">=18.0.0"}`, so on the Node 16 base
    `yarn install` aborts with:
        error remix-monorepo@: The engine "node" is incompatible with this
        module. Expected version ">=18.0.0". Got "16.20.2"
    which leaves the fix stage with ZERO collected tests (report.py gate 1).

    Kept as a SEPARATE image tag rather than bumping the shared base: the
    v0.17..v1.6 bundles (23 of 24 instances) are 2021-2022 code that currently
    builds and passes on Node 16, and moving them to Node 20 risks breaking
    instances that already work. Routing is by release line (see Remix.dependency),
    so only v2-era bundles land here.
    """

    def dependency(self) -> Union[str, "Image"]:
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-node20"

    def workdir(self) -> str:
        return "base-node20"

    # v2-era lockfile; warming from v1.3.0 would cache the wrong tree entirely.
    WARM_REF = "v2.11.1"


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

    def dependency(self) -> Image | None:
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves our dockerfile() verbatim,
        # so the hardening below is applied by hand (anchored on HEAD, since
        # BASE_COMMIT is not a build-arg in FROM-an-image builds).
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Defense-in-depth against re-fetching the fix from GitHub by URL. The
    # hardening block deletes the cloned repo's `origin`, but a model could still
    # run `git fetch https://github.com/<org>/<repo> <future_sha>` to pull the
    # commits that come AFTER the base (where the fix lives). We blackhole every
    # github URL scheme at the git --system level so any git transport to github
    # is rewritten to an unroutable address and fails fast. (Authoritative block
    # is still eval-time network isolation, `docker run --network none`; this is
    # the belt-and-suspenders that survives even a networked run.)
    _GIT_NET_LOCKDOWN = (
        'RUN BH="https://0.0.0.0:1/"; \\\n'
        '    git config --system url."$BH".insteadOf "https://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "http://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "ssh://git@github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git@github.com:"; \\\n'
        '    git config --system url."$BH".insteadOf "https://codeload.github.com/"; \\\n'
        '    git config --system protocol.allow never; \\\n'
        '    git config --system protocol.file.allow always; \\\n'
        '    git config --system --unset-all credential.helper 2>/dev/null || true'
    )

    def _harden(self) -> str:
        """Git-history hardening for the per-PR image, applied AFTER prepare.sh
        has checked out THIS PR's base commit -> the commit to KEEP is the
        current HEAD. Mirrors the harness Image._HARDENING_BLOCK, anchored on
        HEAD instead of ${BASE_COMMIT}."""
        repo = self.pr.repo
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

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
yarn install --frozen-lockfile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "sanitize_patch.sh",
                # Drop binary-file stanzas that carry no `GIT binary patch` payload.
                #
                # The LHT dataset's diffs come from the GitHub compare API, which
                # renders a binary file as:
                #     diff --git a/x.ico b/x.ico
                #     index 00000000000..8830cf6821b     <- abbreviated, not full
                #     Binary files /dev/null and b/x.ico differ
                # i.e. an abbreviated index line and NO payload block. `git apply`
                # refuses that stanza ("cannot apply binary patch ... without full
                # index line") and, because git apply is ATOMIC, a single favicon
                # aborts the ENTIRE patch. Combined with the old `|| true` that
                # meant the fix patch silently never landed and the fix stage
                # scored identically to the test stage -> "no test transitioned
                # failed->passed" -> every report rejected.
                #
                # The bytes are unrecoverable from such a diff, and binary assets
                # (favicons, images) never affect jest outcomes, so the stanza is
                # dropped rather than applied. Everything else passes through
                # byte-for-byte. NOTE: no .format() on this string -- the awk body
                # is full of braces.
                """#!/bin/bash
set -eo pipefail

in_patch="$1"
out_patch="$2"

awk '
function flush() {
  if (started && !(isbin && !haspayload)) printf "%s", buf
}
/^diff --git / { flush(); buf=""; isbin=0; haspayload=0; started=1 }
{
  if (!started) { print; next }
  buf = buf $0 "\\n"
  if ($0 ~ /^Binary files /) isbin=1
  if ($0 ~ /^GIT binary patch/) haspayload=1
}
END { flush() }
' "$in_patch" > "$out_patch"

dropped=$(( $(grep -c '^diff --git ' "$in_patch" || true) - $(grep -c '^diff --git ' "$out_patch" || true) ))
echo "sanitize_patch: $in_patch -> $out_patch (dropped $dropped un-appliable binary stanza(s))"
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{pr.repo}
yarn build || true
npx jest --verbose --testPathPattern='packages/'

""".format(pr=self.pr),
            ),
            # NOTE on `yarn install` after `git apply` (both stages):
            # node_modules is installed in prepare.sh at IMAGE BUILD time, from the
            # BASE commit's manifests. An LHT patch spans a whole release line, so it
            # routinely changes root package.json + yarn.lock, adds NEW workspace
            # packages (e.g. packages/remix-cloudflare/) and new devDependencies
            # (e.g. jest-puppeteer). Against the stale tree that surfaced as
            # `error TS2307: Cannot find module '@remix-run/cloudflare'` from
            # `yarn build`, then jest aborting outright with "Preset jest-puppeteer
            # not found" -> 0 tests collected -> an empty, rejected fix stage.
            # Reinstalling re-links the workspace and pulls the new deps so the
            # patched tree actually builds. It runs in BOTH stages so the test and
            # fix numbers stay comparable (same procedure, only the patch differs).
            # Chromium download is skipped: the puppeteer-based projects are not in
            # --testPathPattern='packages/', the preset only has to RESOLVE for jest
            # config validation, and the download is slow and flaky.
            File(
                ".",
                "extract_stanza.sh",
                # Extract a single file's stanza out of a unified diff.
                # Used to recover jest harness config into the TEST stage; see
                # test-run.sh for why that is necessary and why it is not a leak.
                """#!/bin/bash
set -eo pipefail

in_patch="$1"
want="$2"
out="$3"

awk -v want="$want" '
/^diff --git / {
  keep = ($3 == "a/" want || $4 == "b/" want)
}
keep { print }
' "$in_patch" > "$out"

if [ -s "$out" ]; then
  echo "extract_stanza: recovered $want from $in_patch"
else
  echo "extract_stanza: $want not present in $in_patch"
fi
""",
            ),
            File(
                ".",
                "test-run.sh",
                # No `|| true` on git apply: a patch that fails to apply must fail
                # LOUDLY. Swallowing it silently produced a fix stage identical to
                # the test stage and a rejected report with no visible cause.
                #
                # JEST-CONFIG RECOVERY (the `if [ "$collected" -eq 0 ]` block):
                # build_lht_dataset.split_patches() buckets a diff by path keyword,
                # so `packages/**/__tests__/setup.ts` goes to test_patch while
                # `jest.config.js` (no test keyword in its path) goes to fix_patch --
                # even though the two changes are ATOMIC. When the test patch DELETES
                # a setup file whose registration lives in jest.config.js, the test
                # stage runs the OLD config pointing at a now-missing file and jest
                # aborts before running anything:
                #     Validation Error: Module <rootDir>/packages/remix-deno/
                #     __tests__/setup.ts in the setupFiles option was not found.
                # -> 0 suites collected -> the stage is unusable (report.py gate 1/4).
                # Recovering ONLY jest.config.js is not leaking the fix: that file is
                # jest harness wiring (which projects/setupFiles/globalSetup to load),
                # it is what split_patches should have classified as test infra, and
                # the guard fires ONLY when the stage already collected zero tests --
                # so an instance whose test stage works is never touched.
                """#!/bin/bash
set -eo pipefail

export CI=true
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export npm_config_engine_strict=false
export YARN_IGNORE_ENGINES=1
cd /home/{pr.repo}
bash /home/sanitize_patch.sh /home/test.patch /tmp/test.patch
git apply --whitespace=nowarn /tmp/test.patch
yarn install --ignore-engines
yarn build || true

set +e
npx jest --verbose --testPathPattern='packages/' 2>&1 | tee /tmp/test-stage.out
set -e

collected=$(grep -cE '^(PASS|FAIL) ' /tmp/test-stage.out || true)
if [ "$collected" -eq 0 ]; then
  echo ">>> test stage collected 0 suites; recovering jest harness config from fix.patch"
  bash /home/sanitize_patch.sh /home/fix.patch /tmp/fix.patch
  bash /home/extract_stanza.sh /tmp/fix.patch jest.config.js /tmp/jestcfg.patch
  if [ -s /tmp/jestcfg.patch ]; then
    git apply --whitespace=nowarn /tmp/jestcfg.patch || true
  fi

  # The recovered config can point at setup files that split_patches ALSO misfiled
  # into fix_patch -- typically pure renames, e.g.
  #   rename from packages/create-remix/__tests__/setupAfterEnv.ts   (has "test" -> test_patch)
  #   rename to   integration/helpers/setupAfterEnv.ts               (no keyword -> fix_patch)
  # Jest validates the WHOLE config (even projects outside --testPathPattern), so one
  # missing setup file aborts the run. Chase only the files jest itself names as
  # missing, bounded to 5 rounds so this can never walk the whole fix patch.
  for round in 1 2 3 4 5 6 7 8 9 10 11 12; do
    set +e
    npx jest --verbose --testPathPattern='packages/' 2>&1 | tee /tmp/test-stage.out
    set -e
    collected=$(grep -cE '^(PASS|FAIL) ' /tmp/test-stage.out || true)
    [ "$collected" -gt 0 ] && break
    # Only ever chase a path jest itself declares as a missing HARNESS entry
    # (setupFiles / setupFilesAfterEnv / globalSetup / globalTeardown / snapshotResolver).
    # That constraint is what keeps this from walking into application source:
    # a file can only be recovered if jest names it as test-harness wiring.
    missing=$(grep -oE 'Module <rootDir>/[^ ]+ in the (setup[A-Za-z]*|global[A-Za-z]*|snapshotResolver) option was not found' /tmp/test-stage.out \
              | head -1 | sed -E 's#Module <rootDir>/##; s# in the (setup|global|snapshot).*##')
    if [ -z "$missing" ]; then
      missing=$(grep -oE "Can(no|')t find module <rootDir>/[^ ]+" /tmp/test-stage.out \
                | head -1 | sed -E 's#.*<rootDir>/##')
    fi
    if [ -z "$missing" ]; then
      echo ">>> no further recoverable setup file named by jest"
      break
    fi
    echo ">>> round $round: jest reports missing setup file '$missing'; recovering from fix.patch"
    bash /home/extract_stanza.sh /tmp/fix.patch "$missing" /tmp/miss.patch
    if [ ! -s /tmp/miss.patch ]; then
      echo ">>> '$missing' not in fix.patch; cannot recover"
      break
    fi
    if ! git apply --whitespace=nowarn /tmp/miss.patch; then
      echo ">>> failed to apply '$missing'"
      break
    fi
    yarn build || true
  done
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
# Tests that shell out to `npm install` (e.g. replace-remix-imports-test) resolve
# UNPINNED deps against today's registry, which now serves packages requiring
# node>=18 (e.g. @testing-library/dom@10.4.1, published 2025-07-27). On the
# era-correct Node 16 base that aborts with EBADENGINE and shows up as a bogus
# PASS->FAIL regression (report.py gate 2). Relaxing the engine check keeps the
# 2022-era toolchain while letting those installs proceed.
export npm_config_engine_strict=false
export YARN_IGNORE_ENGINES=1
cd /home/{pr.repo}
bash /home/sanitize_patch.sh /home/test.patch /tmp/test.patch
bash /home/sanitize_patch.sh /home/fix.patch /tmp/fix.patch
git apply --whitespace=nowarn /tmp/test.patch /tmp/fix.patch
yarn install --ignore-engines
yarn build || true
npx jest --verbose --testPathPattern='packages/'

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

{self._GIT_NET_LOCKDOWN}

{self._harden()}

{self.clear_env}

"""


class ImageDefaultNode20(ImageDefault):
    """Per-PR image for v2-era bundles: identical to ImageDefault except it is
    built FROM the Node 20 base. All scripts/patches/hardening are inherited."""

    def dependency(self) -> Image | None:
        return ImageBaseNode20(self.pr, self.config)


@Instance.register("remix-run", "remix")
class Remix(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        # Era routing: a bundle whose release line reaches the Remix v2 series
        # needs Node >=18 (v2's package.json declares that engine), so it gets the
        # Node 20 base. Everything else stays on the era-correct Node 16 base that
        # the v0.17..v1.6 bundles were written for. Keyed off base.label (e.g.
        # "v1.6.4..v2.11.1") because the bundle's PR numbers are not ordered by era.
        label = (self.pr.base.label or "") if self.pr.base else ""
        if re.search(r"v2\.\d", label):
            return ImageDefaultNode20(self.pr, self._config)
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes first
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")
        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)

        # Deduplicate: a suite that failed takes precedence over passed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# LHT bundled-PR dataset instances: Instance.create() uses pr.number_interval as a
# registry-key substitute (see harness/instance.py) -- for a single-PR instance
# number_interval is empty and the plain "remix-run/remix" key above is used, but
# an LHT record bundles several PR numbers into one instance and stamps the exact
# dash-joined list (NOT a min-max range -- the bundle can have gaps) into
# number_interval, e.g. prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# Each bundle in remix-run__remix_lht_final.jsonl must resolve to a registered class,
# so alias every literal bundle string found in that dataset to Remix (same image/
# build logic regardless of which PRs were squashed into the instance).
_LHT_BUNDLE_INTERVALS = [
    "2145-2164-2199-2204-2205-2251-2267-2280-2290-2297-2310-2312-2322-2328-2333-2339-2343-2345-2347-2350-2351-2352-2357-2362-2363",
    "2999-3001-3005",
    "313-314-315-316-317-318-321-322-323-324-326-329-332-333-338-339-340-343-344-345-346-347-349-350-351-352-353",
    "3142-3150-3442-3443-3458-3463-3467-3470-3472-3473-3476-3477-3478-3481-3482-3483-3484-3485-3486-3487-3488-3489-3490-3491-3496-3497-3498-3501-3503-3511-3517-3520-3525",
    "342-354-359-361-363-365-366-370",
    "356-369-371-378-382-384-385-386-388-392-395-396-397-398-399-400-401-402-404-406-407-408-409-410-411-412-413-417-419-424-426-431-435",
    "859-1723-1876-2027-2445-2455-2542-2602-2803-2879-3012-3113-3190-3284-3299-3325-3349-3376-3420-3529-3569-3599-3611-3626-3639-3656-3675-3676-3677-3688-3694-3697-3709-3716-3736-3743-3745-3763-3764-3766-3774-3777-3783-3793-3796-3797-3801-3803-3815-3817-3834-3841-3851-3860-3867-3868-3869-3872-3874-3875-3879-3880-3884-3889-3916-3917-3918-3923-3924-3929-3930-3936-3943-3953-3963-3964-3966-3970-3983-3985-3987-3989-4000-4001-4010-4013-4016-4022-4030-4034-4036-4041-4046-4049-4053-4061-4063-4069-4070-4129-4130-4152-4159-4169-4205-4208-4235-4238-4245-4255-4269-4276-4277-4284-4290-4301-4323-4329-4355-4359-4365-4376-4378-4381-4382-4385-4391-4392-4408-4410-4413-4418-4419-4445-4446-4471-4528-4538-4539-4561-4566-4572-4610-4611-4612-4619-4624-4644-4646-4696-4706-4709-4715-4717-4718-4725-4731-4732-4734-4736-4738-4739-4749-4754-4756-4757-4781-4785-4797-4800-4801-4803-4815-4821-4825-4829-4852-4865-4868-4871-4880-4884-4892-4893-4895-4898-4900-4901-4914-4918-4919-4920-4922-4936-5013-5014-5030-5034-5040-5041-5042-5057-5092-5125-5128-5133-5136-5137-5141-5148-5149-5151-5160-5163-5178-5204-5206-5220-5223-5242-5243-5256-5260-5266-5311-5318-5320-5337-5342-5351-5367-5370-5372-5376-5377-5388-5402-5406-5434-5435-5455-5472-5477-5486-5538-5545-5585-5623-5640-5651-5658-5670-5727-5742-5767-5822-5829-5842-5865-5884-5892-5915-5916-5945-5947-5959-5969-5971-5972-5973-5976-5989-5991-5997-6005-6019-6024-6025-6050-6059-6064-6069-6070-6071-6082-6087-6089-6102-6103-6123-6136-6138-6145-6153-6178-6190-6209-6239-6243-6254-6262-6277-6303-6304-6307-6327-6329-6333-6334-6337-6338-6346-6352-6375-6377-6397-6398-6442-6446-6461-6473-6482-6486-6487-6489-6499-6545-6551-6585-6597-6619-6623-6650-6651-6717-6750-6756-6757-6764-6771-6775-6781-6796-6800-6830-6832-6840-6843-6860-6879-6884-6893-6904-6910-6912-6920-6938-6943-6984-6989-7004-7010-7022-7023-7037-7038-7049-7057-7065-7070-7078-7108-7115-7119-7124-7128-7132-7146-7148-7161-7162-7163-7168-7177-7191-7218-7224-7225-7228-7268-7339-7433-7435-7458-7465-7469-7474-7476-7482-7483-7484-7488-7489-7490-7492-7501-7513-7520-7523-7531-7536-7544-7550-7559-7561-7578-7580-7597-7620-7622-7680-7692-7702-7705-7707-7711-7747-7752-7777-7778-7783-7800-7801-7802-7803-7804-7844-7857-7877-7886-7900-7910-7920-7925-7955-7959-7970-7971-7981-7985-8007-8015-8020-8034-8036-8037-8043-8053-8098-8118-8123-8148-8149-8150-8161-8167-8202-8205-8212-8244-8247-8272-8286-8287-8297-8299-8307-8308-8312-8314-8317-8318-8323-8327-8360-8373-8402-8409-8410-8412-8417-8418-8419-8421-8425-8426-8439-8441-8447-8451-8483-8487-8491-8501-8722-8778-8785-8787-8789-8808-8815-8821-8822-8823-8824-8825-8827-8839-8851-8853-8854-8857-8863-8870-8873-8893-8896-8899-8900-8909-8914-8920-8922-8928-8948-8950-8952-8956-8957-8958-8959-8972-8981-9009-9017-9023-9031-9036-9045-9075-9077-9082-9088-9094-9118-9126-9133-9152-9163-9197-9205-9210-9228-9230-9235-9237-9260-9292-9294-9329-9335-9336-9345-9371-9378-9381-9409-9425-9429-9430-9433-9480-9481-9491-9511-9512-9525-9533-9534-9535-9539-9540-9543-9545-9546-9547-9548-9558-9565-9568-9577-9581-9582-9585-9618-9680-9685-9687-9694-9696-9739-9744-9749-9768-9774-9782-9791",
    "1232-1259-1438-1485-1495-1754-1854-1915-1916-1986-1997-1999-2011-2037-2040-2051-2059-2061-2063-2066-2080-2081-2084-2085-2089-2090-2094-2095-2096-2097-2099-2111-2113-2121-2124-2126-2130-2132-2142",
    "1260-1331-1417-1418-1500-1808-1905-1922-1937-1947-1954-1965-1969-1971-1984-1985-1992-1994-1998-2003-2013-2019-2025-2028",
    "1316-2804-3080-3085-3460-3526-3540-3542-3543-3546-3554-3555-3557-3558-3561-3567-3571-3579-3588-3591-3592",
    "1428-1562-1970-2035-2103-2141-2143-2144-2156-2158-2181-2182-2185-2186-2188-2189-2193-2194-2196-2197-2200-2212-2225-2243-2249-2250-2260-2269-2271-2286-2291-2301-2314-2315-2316-2318-2319-2325-2326-2327-2330",
    "1537-1977-2043-2065-2150-2257-2403-2412-2430-2456-2495-2513-2527-2529-2530-2558-2561-2569-2571-2573-2574-2575-2576-2579-2581-2583-2585-2586-2587-2595-2596-2601-2613-2617-2627-2630-2631-2635-2637-2638-2639-2640-2641-2642-2643-2647-2652-2653-2655-2657-2659-2661-2662-2664-2665",
    "1811-2107-2155-2187-2285-2317-2331-2415-2447-2449-2451-2454-2466-2480-2482-2483-2484-2486-2493-2496-2499-2502-2507-2510-2514-2515-2516-2525-2531-2541-2544-2545-2568",
    "1822-2401-2450-2459-2460-2528-2556-2562-2593-2634-2663-2667-2669-2670-2676-2682-2694-2695-2704-2705-2710-2717-2721-2727-2731-2733-2738-2757-2759-2764-2767-2785-2791",
    "2014-2224-2275-2309-2441-2736-2779-2865-2938-2960-2961-2982-2988-2992-3013-3018-3019-3026-3031-3033-3038-3039-3042-3043-3045-3059-3061-3064-3068-3070-3071-3072-3073-3074-3075-3076-3077-3078-3079-3081-3083-3084-3087-3094-3097-3107-3109-3110-3111-3114-3115-3117-3119-3121-3135-3141-3144-3146-3148-3151-3152-3154-3162-3167-3168-3169-3182-3183-3184-3187-3189-3191-3198-3206-3207-3218",
    "2057-2218-2332-2388-2824-2864-2876-2944-3091-3181-3208-3215-3216-3221-3223-3225-3226-3228-3229-3231-3233-3235-3236-3239-3242-3243-3245-3249-3250-3253-3254-3255-3259-3266-3268-3269-3274-3278-3279-3283-3285-3287-3288-3289-3290-3291-3315-3319-3329-3345-3353-3356-3358-3359-3360-3362-3368-3370-3371-3375-3381-3384-3391-3413-3417-3418-3426-3431-3432-3436-3440-3445-3447-3459",
    "2313-2822-3188-3276-3441-3521-3547-3553-3573-3593-3595-3598-3602-3610-3619-3623-3631-3633-3637-3642-3644-3662-3664",
    "239-241-242-245-250-251-252-253-254-256-259-260-261",
    "2547-2671-2711-2722-2741-2742-2763-2765-2771-2780-2783-2784-2787-2789-2790-2794-2802-2805-2806-2807-2808-2809-2814-2818-2819-2829-2837-2838-2839-2840",
    "264-269-270-271-273-276-277-278-280-283-284-285-286-289-290-292-293-294-299-301-303",
    "2739-2786-2841-2843-2855-2862-2863-2867-2869-2870-2871-2872-2873-2874-2875-2901-2939-2945-2957-2964-2975-2977-2990",
    "357-383-422-427-439-445-446-455-473-475-476-477-482-483-486-490-495-502-506-507-508-512-521-530-534-540-549-560-563-568-569-575-583-587-604-610-619-630-641-666-670-684-710-713-720-734-754-755-757-758-766-767-770-772-775-787-789-791-794-798-802-807-814-816-818-825-828-838-843-847-849-853-855-858-860-861-864-870-877-884-887-890-891-892-895-905-908-910-926-930-939-940-949-955-956-966-974-976-981-983-984-985-987-988-989-1026-1027-1039-1040-1047-1050-1058-1060-1061-1067-1070-1071-1075-1076-1084-1085-1087-1089-1090-1092-1094-1097-1109-1114-1116-1117-1120-1122-1124-1125",
    "623-1534-1661-1761-1982-2137-2138-2220-2264-2268-2273-2282-2296-2329-2342-2353-2355-2359-2369-2370-2374-2375-2376-2377-2379-2380-2381-2392-2393-2395-2396-2398-2406-2411-2420-2421-2431-2434-2436-2438-2443-2444",
    "739-832-871-936-1100-1103-1123-1130-1175-1180-1181-1199-1203-1234-1237-1240-1352-1397-1416-1462-1470-1504-1538-1546-1563-1586-1605-1634-1638-1653-1656-1664-1667-1669-1686-1698-1709-1712-1743-1745-1749-1750-1763-1764-1765-1766-1767-1768-1769-1775-1780-1781-1783-1789-1801-1835-1836-1839-1843-1846-1847-1861-1863-1869-1875-1881-1882-1883-1888-1903-1913-1958-1975-1976",
]

for _interval in _LHT_BUNDLE_INTERVALS:
    Instance.register("remix-run", _interval)(Remix)
