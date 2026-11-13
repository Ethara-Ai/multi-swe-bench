"""paperjs/paper.js repo config -- pnpm + Vitest era.

Era: base commits after the switch from Yarn to pnpm (e923fb183) and from
QUnit to Vitest (57db7ff26, test files test/tests/<Name>.test.js), October 2026.
Install: `pnpm install --frozen-lockfile` with the pnpm version pinned by the
`packageManager` field (pnpm@12.9.0); pnpm-workspace.yaml sets nodeLinker:
hoisted and only lets canvas run its install script.
Earlier base commits use paper_js_2300_to_99999.

The interval name is nominal: paper.js merges old PR numbers onto new develop
commits, so the era is decided by the base commit's toolchain, and each record's
`number_interval` field names the era file explicitly. PR in this era: #2408.

Layout (per-PR base):
  * ImageBase   -- node:24 (the version CI uses) + fonts-liberation (the text
                   tests expect Arial metrics) + node-canvas build deps
                   (fallback if no prebuilt binary is found). Clones paper.js,
                   fetches BASE_COMMIT by SHA if the clone has no ref to it, pins
                   it, and the default hardening block scrubs history.
  * ImageDefault -- copies the patches and scripts and runs prepare.sh
                   (pin check, pnpm install, hard gate).

Test command: `pnpm exec vitest run --project node --reporter=tap-flat`, the
`test:node` script CI's test job runs, on Node with jsdom + node-canvas. The
`browser` project (Playwright's own Chromium download) and the `screenshots`
project (Playwright's Docker image) are not run; every test/tests/*.test.js file
runs in the node project too. dist/paper-full.js is a symlink to src/load.js on
develop, so the tests load the sources and no build is needed.
tap-flat prints one line per test, `ok N - <file> > <describe> > <test> # time=..`;
test names are `<file> > <describe> > <test>`, without the number or the timing;
a name repeated within one log gets ` #2`, ` #3`... in output order.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:24.22.2-bookworm"
PNPM_VERSION = "12.9.0"
TEST_CMD = "pnpm exec vitest run --project node --reporter=tap-flat"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export HUSKY=0\n"
    "export npm_config_update_notifier=false\n"
    "export npm_config_fund=false\n"
    "export npm_config_audit=false\n"
)

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def _pnpm_install() -> str:
    return (
        "ok=0\n"
        "for attempt in 1 2 3; do\n"
        "    if pnpm install --frozen-lockfile; then ok=1; break; fi\n"
        '    echo "pnpm install attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        'test "$ok" = 1\n'
    )


def _test_block() -> str:
    # Same block in run.sh, test-run.sh and fix-run.sh.
    return f"{TEST_CMD}\n"


class PaperJs2300To99999ImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "fonts-liberation",
            "libcairo2-dev",
            "libpango1.0-dev",
            "libjpeg-dev",
            "libgif-dev",
            "librsvg2-dev",
            "pkg-config",
        ]

    def dockerfile(self) -> str:
        # The default per-PR Dockerfile, with one addition: some base commits
        # (e.g. #2156's) are on no branch any more, so a plain clone does not
        # contain them. Fetch the commit by SHA when it is missing.
        pin = "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}"
        fetch = (
            'RUN git cat-file -e "${BASE_COMMIT}^{commit}" 2>/dev/null \\\n'
            '    || git fetch --no-tags origin "${BASE_COMMIT}"\n'
        )
        default = super().dockerfile()
        if default.count(pin) != 1:
            raise RuntimeError("unexpected default Dockerfile layout")
        return default.replace(pin, fetch + pin)


class PaperJs2300To99999ImageDefault(Image):
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
        return PaperJs2300To99999ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _header(self) -> str:
        return f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}\ncd /home/{self.pr.repo}\n\n"

    def _prepare_sh(self) -> str:
        sha = self.pr.base.sha
        return (
            f"{self._header()}"
            "# --- Section 1: pin ---\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "test -f pnpm-lock.yaml\n"
            "test -f vitest.config.mjs\n"
            "\n"
            "# --- Section 2: provision ---\n"
            "node --version\n"
            f"node -e \"process.exit(require('./package.json').packageManager === 'pnpm@{PNPM_VERSION}' ? 0 : 1)\"\n"
            f"npm install -g pnpm@{PNPM_VERSION}\n"
            "pnpm --version\n"
            f"{_pnpm_install()}"
            "\n"
            "# --- Section 3: gate ---\n"
            "git diff --quiet -- pnpm-lock.yaml package.json\n"
            f'test "$(pnpm --version)" = "{PNPM_VERSION}"\n'
            "pnpm exec vitest --version\n"
            "node -e \"require('canvas').createCanvas(10, 10).getContext('2d'); "
            "require('jsdom'); require('resemblejs'); "
            "require('./node_modules/prepro/lib/node.js');\"\n"
            "test -f /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf\n"
            "pnpm exec vitest list --project node --filesOnly > /tmp/vitest-files.txt\n"
            "grep 'test/tests/Point.test.js' /tmp/vitest-files.txt\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{self._header()}{_test_block()}"

    def _test_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_block()}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_block()}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("paperjs", "paper_js_2300_to_99999")
class PAPER_JS_2300_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PaperJs2300To99999ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        log = log.replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # vitest tap-flat: "ok 12 - <file> > <describe> > <test> # time=1.23ms",
        # "not ok 3 - ...", "ok 7 - ... # SKIP", "ok 8 - ... # TODO"
        tap_re = re.compile(r"^(not ok|ok) \d+ - (.+?)\s*$")
        time_re = re.compile(r"\s+# time=[\d.]+m?s\s*$")
        directive_re = re.compile(r"\s+# (SKIP|TODO)\b.*$", re.IGNORECASE)

        seen: dict[str, int] = {}
        for line in log.splitlines():
            m = tap_re.match(line)
            if not m:
                continue
            status, title = m.group(1), m.group(2)
            title = time_re.sub("", title)
            is_skip = bool(directive_re.search(title))
            title = directive_re.sub("", title).strip()
            if not title:
                continue
            # Some tests share a name within a describe block (vitest allows
            # it); tap-flat lists them in file and declaration order, so number
            # the repeats to keep every test distinct and stable across stages.
            seen[title] = seen.get(title, 0) + 1
            if seen[title] > 1:
                title = f"{title} #{seen[title]}"
            if status == "not ok":
                failed_tests.add(title)
            elif is_skip:
                skipped_tests.add(title)
            else:
                passed_tests.add(title)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
