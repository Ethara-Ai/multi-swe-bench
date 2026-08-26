import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SwrvImageBase(Image):
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
        # The repo's own CI (.github/workflows/main.yml) pins node 12.x, but this
        # is a 2020-era Vue-CLI/webpack-4 stack with no .nvmrc, and node:12 is
        # long EOL. Verified in Docker on node:20-bookworm: `yarn install
        # --frozen-lockfile` resolves the committed yarn.lock v1 cleanly (only
        # benign peer-dep warnings), `yarn build` succeeds, and the full suite is
        # 25/25 passing at this base commit -- provided NODE_OPTIONS below is set.
        return "node:20-bookworm"

    def image_tag(self) -> str:
        # Per-PR: the injected hardening block detaches at one ${BASE_COMMIT} and
        # prunes every other ref, so a shared tag would let whichever PR built
        # first pin the commit for all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Rendered after `git checkout ${BASE_COMMIT}`, with WORKDIR already at
        # /home/swrv.
        #
        # webpack 4 (via @vue/cli-service 4.x) hashes with MD4 through Node's
        # crypto, which OpenSSL 3 -- shipped in Node 17+ -- refuses. Without the
        # legacy provider the SSR suite dies with
        #   error:0308010C:digital envelope routines::unsupported
        # at webpack/lib/util/createHash.js, which is an ENVIRONMENT artifact,
        # not a real test result: it would fail identically at the run, test and
        # fix stages and silently corrupt the f2p/n2p signal. Verified in Docker:
        # setting this turns that crash into a clean 25/25 baseline.
        return """ENV NODE_OPTIONS=--openssl-legacy-provider"""

    def dockerfile(self) -> str:
        # Reimplements Image.dockerfile() rather than calling super(), for one
        # reason only: the base class hardcodes its own
        # "ENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8" here, and
        # DockerfileEnhancer._ENV_BLOCK (injected into every rendered Dockerfile,
        # this one included) already sets both -- plus TZ and the proxy/CA vars --
        # earlier in the same file. That duplicate is a harmless no-op (same value
        # set twice) but this project's Dockerfile QC flags it, and fixing the
        # shared base class would touch every other default-template repo rather
        # than just this one. So: everything below is byte-for-byte identical to
        # Image.dockerfile() except that one ENV pair is dropped and WORKDIR
        # /home/ is kept on its own line.
        base_img = self.dependency()
        if isinstance(base_img, Image):
            raise NotImplementedError(
                "Subclass must override dockerfile() or return a string from dependency()"
            )

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]

        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        repo = _safe_path_component(self.pr.repo)
        clone_section = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

        extra_setup = self.extra_setup()

        sections = [f"FROM {base_img}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")

        sections.append(apt_command)
        sections.append(clone_section)
        sections.append(f"WORKDIR /home/{repo}")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")

        if extra_setup:
            sections.append(extra_setup)

        sections.append(self._HARDENING_BLOCK)

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class SwrvImageDefault(Image):
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
        return SwrvImageBase(self.pr, self.config)

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

cd /home/{pr.repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm the yarn cache + node_modules so the three graded runs do not each pay a
# full cold install. `|| true` because a warm-up hiccup must not fail the image
# build -- the graded runs decide pass/fail, not this. Verified in Docker that
# this leaves the worktree clean (node_modules/, dist/ and esm/ are all
# gitignored), which the clean-tree assertions above depend on.
yarn install --frozen-lockfile || true
yarn build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
# install -> build -> test, the identical three-step sequence used by
# test-run.sh and fix-run.sh so the only thing that varies between the graded
# stages is which patch got applied.
#
# The install is NOT redundant with prepare.sh: fix.patch edits package.json and
# yarn.lock (it adds jest-date-mock, which test.patch imports), so the fix stage
# genuinely needs a re-resolve. Running it at every stage keeps the command
# consistent instead of special-casing one script.
#
# The build is required, not optional: tests/ssr.spec.ts imports '../../esm',
# which only exists after `yarn build`. Without it that suite fails with
# "Cannot find module '../../esm'" at every stage -- verified in Docker.
#
# --testTimeout raises jest's 10s default. tests/ssr.spec.ts spins up a real
# webpack SSR render: measured 4.5-6.4s natively and observed timing out at
# 10.5s under mere container load. Under arm64 QEMU emulation (this image is
# built linux/amd64 + linux/arm64) that is far slower still, so the 10s default
# would turn a passing test into a guaranteed failure on one arch only --
# fabricating a f2p/n2p signal from an emulation artifact. Applied identically
# in all three graded scripts so the stages stay comparable.
yarn install --frozen-lockfile || true
yarn build || true
yarn test --verbose --testTimeout=120000

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Same install -> build -> test sequence as run.sh / fix-run.sh; see run.sh.
# Verified in Docker: with only test.patch applied, tests/use-swrv.spec.tsx
# fails to COMPILE (TS2307: Cannot find module 'jest-date-mock') because that
# dependency is added by fix.patch, so this suite contributes 0 tests while
# tests/ssr.spec.ts still runs and reports normally.
yarn install --frozen-lockfile || true
yarn build || true
yarn test --verbose --testTimeout=120000

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
# Same install -> build -> test sequence as run.sh / test-run.sh; see run.sh.
# The install matters most here: fix.patch adds jest-date-mock to package.json
# and yarn.lock, so without a re-resolve the new tests could not resolve their
# import. Verified in Docker: 27 passed / 1 todo, including the two new ttl
# tests that test.patch adds.
yarn install --frozen-lockfile || true
yarn build || true
yarn test --verbose --testTimeout=120000

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Kong", "swrv")
class Swrv(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SwrvImageDefault(self.pr, self._config)

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
        # jest via `vue-cli-service test:unit --verbose`. Formats captured
        # verbatim from this repo at 7395c85c:
        #   "    ✓ should return data after hydration (3ms)"
        #   "    ✕ deliberately failing probe test (4ms)"
        #   "    ✎ todo mutate triggers revalidations"
        # The failing form was captured by injecting a deliberately-failing test
        # into the suite, not guessed -- jest prints no ✕ line at all on a clean
        # run, so the pass/todo formats alone would not have revealed it.
        #
        # The trailing "(NNms)" is optional: jest omits it for fast tests and
        # always omits it for todo entries, hence the non-greedy name capture
        # plus an optional duration group.
        #
        # Note this repo's own bug shape: with only test.patch applied the
        # use-swrv suite fails to COMPILE (missing jest-date-mock), so it emits
        # no per-test lines at all and simply contributes nothing here. That is
        # correct and intentional -- a suite that never ran must not be counted
        # as passing OR failing. Do not "fix" this by scraping jest's
        # "Tests:" summary line as a fallback: that aggregate counts suites that
        # did run and would silently attribute their numbers to the suite that
        # did not.
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        def strip_ansi(text: str) -> str:
            return re.compile(r"\x1B\[[0-?9;]*[mGKH]").sub("", text)

        re_pass = re.compile(r"^[✓√]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_fail = re.compile(r"^[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_todo = re.compile(r"^[✎]\s+todo\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")

        for raw_line in test_log.splitlines():
            line = strip_ansi(raw_line).strip()

            m = re_todo.match(line)
            if m:
                name = m.group(1)
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                continue

            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )