import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:10-buster"
_INTERVAL_NAME = "nuxt_2736_to_147"


class ImageBase(Image):
    """Base image for Nuxt v0.9–v1.x era (ava test runner, npm/yarn)."""

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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def workdir(self) -> str:
        return "base-{name}".format(name=_INTERVAL_NAME)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        # SHARED base (tag base-<interval>) — the `# syntax` directive makes
        # DockerfileEnhancer.enhance() skip it, so the enhancer doesn't rewrite the
        # clone to checkout ${{BASE_COMMIT}} + gc-prune and pin the shared base to a
        # single commit (which breaks every other PR). Per-PR hardening is embedded
        # in ImageDefault below.
        return """# syntax=docker/dockerfile:1.6
FROM {image_name}

{global_env}

WORKDIR /home/

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class ImageDefault(Image):
    """Per-PR image for Nuxt v0.9–v1.x era."""

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
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def _test_files(self) -> list[str]:
        files = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch):
            if ".test." in m or ".spec." in m:
                files.append(m)
        return files

    def files(self) -> list[File]:
        test_files = self._test_files()
        test_files_str = " ".join(test_files)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                _RUN_TESTS_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        # Per-PR anti-cheat hardening. dependency() returns an Image, so
        # DockerfileEnhancer emits this Dockerfile verbatim (it only auto-injects
        # the hardening into str-dependency/base images), hence we embed
        # Image._HARDENING_BLOCK ourselves. ENV BASE_COMMIT resolves the block's
        # ${BASE_COMMIT}; WORKDIR pins the repo dir so the hardening RUN (detach
        # onto BASE_COMMIT -> drop every ref/remote -> GC unreachable objects ->
        # self-audit) operates on the checkout prepare.sh produced.
        return """FROM {name}:{tag}

ENV BASE_COMMIT={base_sha}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{hardening}

{clear_env}

CMD ["/bin/bash"]
""".format(
            name=name,
            tag=tag,
            base_sha=self.pr.base.sha,
            global_env=self.global_env,
            copy_commands=copy_commands,
            repo=self.pr.repo,
            hardening=Image._HARDENING_BLOCK,
            clear_env=self.clear_env,
        )


@Instance.register("nuxt", _INTERVAL_NAME)
class NuxtAva(Instance):
    """Nuxt v0.9–v1.x: ava test runner, npm/yarn."""

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Ava output patterns:
        #   ✔ test name
        #   ✖ test name
        #   - test name (skipped)
        # Ava colorizes its reporter output, so each "✔ name" line is actually
        # "\x1b[32m✔\x1b[39m name" — strip ANSI escapes first or the ✔/✖ anchors
        # never match and every test is mis-counted as 0.
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        re_pass = re.compile(r"^\s*[✔✓]\s+(.+?)$")
        re_fail = re.compile(r"^\s*[✖✗×]\s+(.+?)$")
        re_skip = re.compile(r"^\s*[-]\s+(.+?)$")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_fail.match(line)
            if m:
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# Shell script templates
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Install with yarn if yarn.lock present, else npm
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi

# Build (tests import from lib/)
npm run build || true

"""

_RUN_TESTS_SH = """#!/bin/bash
cd /home/{repo}
TEST_FILES="$@"

# Run ava with the test files
if [ -n "$TEST_FILES" ]; then
    npx ava --verbose --serial $TEST_FILES 2>&1 || true
else
    npx ava --verbose --serial test/ 2>&1 || true
fi
"""

_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
bash /home/run_tests.sh {test_files}
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true

# Reinstall if package.json changed, then build
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi
npm run build || true

bash /home/run_tests.sh {test_files}
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}

# Reinstall if package.json changed, then build
if [ -f yarn.lock ]; then
    yarn install --ignore-engines || true
else
    npm install || true
fi
npm run build || true

bash /home/run_tests.sh {test_files}
"""


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Delivered bundles routed to this era class. Instance.create() resolves
# nuxt/<number_interval> -> NuxtAva. Bundle-level; data-derived from the
# delivered resolved set (regenerate if it changes).
_BUNDLE_NIS = [
    "147-157",
    "186-229",
    "2580-2584-2594-2608-2610-2617-2628-2633-2634-2642-2654-2670-2673-2674-2679-2687",
    "2696-2698-2703-2718-2725",
    "272-274-281-282-313-346-384-392-403-411",
    "2736-2742-2748-2754-2755-2766-2773-2779-2783-2784-2790-2831-2861-2883-2884-2898-2900-2909-2915-2920",
    "372-456-488-507-515-516-520-553",
    "4127-4129",
    "592-600-616-617-637",
    "633-662-668-724-765-768-776-780-853-1022-1040-1136-1209-1368-1390-1392-1478-1480-1517-1723-1757-1782-1840-1860-1865-1868-1910-1914-1973-1974-1976-1981-1982-1985-1986-1994-2016-2029-2030-2032-2035-2036-2070-2081-2096-2100-2101-2105-2126-2127-2132-2136-2148-2152-2153-2154-2155-2158-2164-2172-2173-2181-2189-2190-2199-2205-2207-2212-2217-2218-2219-2220-2224-2229-2232-2234-2239-2244-2245-2250-2252-2258-2261-2265-2269-2276-2284-2288-2291-2298-2299-2301-2303-2305-2309-2313-2316-2325-2333-2339-2340-2348-2349-2353-2361-2365-2368-2377-2379-2380-2382-2383-2384-2400-2410-2411-2412-2415-2417-2431-2432-2467-2487-2490-2502",
]
for _ni in _BUNDLE_NIS:
    Instance.register("nuxt", _ni)(NuxtAva)
