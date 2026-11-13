"""jsdom/jsdom repo config.

Layout (per-PR base):
  * ImageBase   -- node image + python-is-python3, clones jsdom, pins BASE_COMMIT,
                   fetches the pinned web-platform-tests submodule (depth 1, it is
                   only needed for wpt.py serve/lint and the testharness resources),
                   then the default hardening block scrubs history of both repos.
  * ImageDefault -- copies the patches and scripts and runs prepare.sh
                   (pin check, npm ci, idl codegen, WPT hosts file, hard gate).

Test command: the repo's own mocha runner, scoped to
  * test/api/                                 (JSDOM API tests, fast)
  * test/web-platform-tests/run-tuwpts.js     (all "to-upstream" WPT-format tests;
                                               this is where PR test patches add
                                               test/web-platform-tests/to-upstream/**)
The full upstream WPT suite (test:wpt) is skipped: it takes far too long.

The run scripts re-sync node_modules with `npm ci` when the working tree's
package.json / package-lock.json differ from HEAD (that is, a patch bumped a
dependency), then always regenerate lib/generated/idl via
`npm run prepare`, because the generated code depends on the installed webidl2js.
The step is the same in all three scripts.

Results are printed with mocha's TAP reporter (which prints fullTitle(), so describe
context is kept); every mocha invocation is preceded by an `MSWE_SUITE <spec>` marker
and test names are `<spec> > <fullTitle>`.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22.23.4-bookworm"
WPT_SUBMODULE = "test/web-platform-tests/tests"
WPT_HOSTS_FILE = "/home/wpt-hosts"
TEST_SPECS = ["test/api/", "test/web-platform-tests/run-tuwpts.js"]

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export npm_config_audit=false\n"
    "export npm_config_fund=false\n"
    "export npm_config_update_notifier=false\n"
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


def _npm_ci_with_retry() -> str:
    return (
        "ok=0\n"
        "for attempt in 1 2 3; do\n"
        "    if npm ci; then ok=1; break; fi\n"
        '    echo "npm ci attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        'test "$ok" = 1\n'
    )


def _test_block() -> str:
    # Same block in run.sh, test-run.sh and fix-run.sh.
    lines = [
        f"cat {WPT_HOSTS_FILE} >> /etc/hosts",
        "getent hosts web-platform.test",
        "",
        "# Re-sync node_modules only if a patch changed the dependency manifests.",
        "if ! git diff --quiet HEAD -- package.json package-lock.json; then",
        "    echo 'MSWE_LOG dependency manifests changed; running npm ci'",
        "    " + _npm_ci_with_retry().rstrip("\n").replace("\n", "\n    "),
        "fi",
        "# Regenerate the webidl2js output (depends on the installed webidl2js).",
        "npm run prepare",
        "",
        "rc=0",
    ]
    for spec in TEST_SPECS:
        lines.append(f'echo "MSWE_SUITE {spec}"')
        lines.append(f"npx mocha --reporter tap {spec} || rc=$?")
        lines.append(f'echo "MSWE_SUITE_END {spec} exit=$rc"')
    lines.append("exit $rc")
    return "\n".join(lines) + "\n"


class JsdomImageBase(Image):
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
        # run-tuwpts.js and wpt-server.js spawn `python ./wpt.py ...`; wpt.py
        # creates its own venv (_venv3) for `lint` and `serve`.
        return ["python-is-python3", "python3-venv"]

    def extra_setup(self) -> str:
        # Runs after `git checkout ${BASE_COMMIT}` and before the hardening block,
        # which also scrubs the submodule (Image._HARDENING_BLOCK, submodule foreach).
        return (
            "RUN set -eux; \\\n"
            "    for attempt in 1 2 3; do \\\n"
            f"        git -c protocol.version=2 submodule update --init --depth 1 {WPT_SUBMODULE} && break; \\\n"
            '        echo "submodule fetch attempt $attempt failed"; sleep 20; \\\n'
            "    done; \\\n"
            f"    test -f {WPT_SUBMODULE}/wpt.py; \\\n"
            "    test -z \"$(git submodule status | grep -E '^[-+U]')\""
        )


class JsdomImageDefault(Image):
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
        return JsdomImageBase(self.pr, self._config)

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
            f"test -f {WPT_SUBMODULE}/wpt.py\n"
            "\n"
            "# --- Section 2: provision ---\n"
            "node --version\n"
            "npm --version\n"
            "python --version\n"
            f"{_npm_ci_with_retry()}"
            "npm run prepare\n"
            f"(cd {WPT_SUBMODULE} && ./wpt make-hosts-file) > {WPT_HOSTS_FILE}\n"
            "# Create wpt.py's venv (pip installs its lint requirements) at build time;\n"
            "# run-tuwpts.js runs exactly this lint before starting `wpt.py serve`.\n"
            f"(cd {WPT_SUBMODULE} && python ./wpt.py lint --repo-root ../to-upstream --all)\n"
            "\n"
            "# --- Section 3: gate ---\n"
            f"grep -q 'web-platform.test' {WPT_HOSTS_FILE}\n"
            "test -x node_modules/.bin/mocha\n"
            "test -s lib/generated/idl/utils.js\n"
            'node -e "const { JSDOM } = require(\'./lib/api.js\'); '
            "const d = new JSDOM('<p>ok</p>'); "
            "if (d.window.document.querySelector('p').textContent !== 'ok') process.exit(1); "
            "require.resolve('mocha-sugar-free'); require('js-yaml'); require('undici');\"\n"
            f"{WPT_SUBMODULE}/_venv3/bin/python -c 'import yaml'\n"
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


@Instance.register("jsdom", "jsdom")
class JSDOM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JsdomImageDefault(self.pr, self._config)

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

        suite_re = re.compile(r"^MSWE_SUITE (\S+)\s*$")
        # mocha TAP reporter: "ok 12 <fullTitle>", "not ok 3 <fullTitle>",
        # "ok 7 <fullTitle> # SKIP -"
        tap_re = re.compile(r"^(not ok|ok) \d+ (.+?)\s*$")
        skip_re = re.compile(r"\s+# SKIP\b.*$")

        suite: Optional[str] = None
        for line in log.splitlines():
            m = suite_re.match(line)
            if m:
                suite = m.group(1)
                continue
            if suite is None:
                continue
            m = tap_re.match(line)
            if not m:
                continue
            status, title = m.group(1), m.group(2)
            is_skip = bool(skip_re.search(title))
            title = skip_re.sub("", title).strip()
            if not title:
                continue
            name = f"{suite} > {title}"
            if status == "not ok":
                failed_tests.add(name)
            elif is_skip:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
