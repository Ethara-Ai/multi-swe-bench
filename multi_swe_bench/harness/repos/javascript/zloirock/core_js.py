import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ERA_MAX_PR = 1036

NODE_IMAGE = "node:14.21.3-bullseye"

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"

_BUILD = (
    "node_modules/.bin/zx scripts/generate-indexes.mjs\n"
    "node_modules/.bin/zx scripts/clean-and-copy.mjs\n"
    "node_modules/.bin/zx scripts/build-compat-data.mjs\n"
    "node_modules/.bin/zx scripts/build-compat-entries.mjs\n"
    "node_modules/.bin/zx scripts/build-compat-modules-by-versions.mjs\n"
    "node_modules/.bin/zx scripts/bundle.mjs\n"
)

_TEST_CMD = (
    "QUNIT=node_modules/qunit/bin/qunit.js\n"
    "missing_plan=0\n"
    "plans=0\n"
    "run_bundle() {\n"
    '    local name="$1" entry="$2" output="$3"\n'
    "    shift 3\n"
    '    echo "=== core-js qunit bundle: $name ==="\n'
    "    local build_rc=0\n"
    '    node_modules/.bin/webpack --entry "$entry" --output-filename "$output" || build_rc=$?\n'
    '    if [ "$build_rc" != 0 ]; then\n'
    '        echo "webpack exited with code $build_rc for the $name bundle" >&2\n'
    '        echo "not ok 0 <bundle failed to build>"\n'
    "        return 0\n"
    "    fi\n"
    "    local rc=0\n"
    '    node "$QUNIT" "$@" 2>&1 | tee "/tmp/qunit-$name.tap" || rc=$?\n'
    '    if grep -Eq "^1\\.\\.[0-9]+$" "/tmp/qunit-$name.tap"; then\n'
    "        plans=$((plans + 1))\n"
    "    else\n"
    '        echo "qunit exited with code $rc without a TAP plan for the $name bundle" >&2\n'
    "        missing_plan=1\n"
    "    fi\n"
    "}\n"
    "run_bundle global ./tests/tests/index.js tests.js packages/core-js-bundle/index.js tests/bundles/tests.js\n"
    "run_bundle pure ./tests/pure/index.js pure.js tests/bundles/pure.js\n"
    'if [ "$missing_plan" != 0 ] || [ "$plans" = 0 ]; then\n'
    "    exit 1\n"
    "fi\n"
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


class CoreJsImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class CoreJsImageDefault(Image):
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
        return CoreJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        return (
            f"{_SCRIPT_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# No lockfile is committed (.npmrc: package-lock=false), so resolve every range as\n"
            "# of the base commit's date instead of today. --ignore-scripts skips the browser\n"
            "# downloads (puppeteer, playwright, phantomjs) that only the karma suites use; no\n"
            "# dependency of the node unit tests needs an install script.\n"
            'export npm_config_before="$(git log -1 --format=%cI HEAD)"\n'
            'echo "prepare: resolving npm dependencies as of $npm_config_before"\n'
            "installed=0\n"
            "for attempt in 1 2 3; do\n"
            "    if npm install --ignore-scripts --no-audit --no-fund; then installed=1; break; fi\n"
            '    echo "prepare: npm install attempt $attempt failed; retrying in 15s"\n'
            "    rm -rf node_modules\n"
            "    sleep 15\n"
            "done\n"
            'if [ "$installed" != 1 ]; then\n'
            '    echo "prepare: npm install failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "# `npm run bootstrap`: installs core-js-builder / core-js-compat dependencies and\n"
            "# symlinks the local packages, so the bundle is built from this checkout's modules.\n"
            "bootstrapped=0\n"
            "for attempt in 1 2 3; do\n"
            "    if node_modules/.bin/lerna bootstrap --no-ci --ignore-scripts; then bootstrapped=1; break; fi\n"
            '    echo "prepare: lerna bootstrap attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'if [ "$bootstrapped" != 1 ]; then\n'
            '    echo "prepare: lerna bootstrap failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "# Build the bundles once so the gate can check them; the run scripts rebuild after\n"
            "# applying patches.\n"
            f"{_BUILD}"
            "node_modules/.bin/webpack --entry ./tests/tests/index.js --output-filename tests.js\n"
            "node_modules/.bin/webpack --entry ./tests/pure/index.js --output-filename pure.js\n"
            "\n"
            "# --- 3. gate ---\n"
            "# Node without native atob/btoa (the polyfill under test), QUnit's TAP CLI, the\n"
            "# builder linked to this checkout, and every bundle the run scripts load.\n"
            "node -e \"if (typeof atob !== 'undefined' || typeof btoa !== 'undefined') { console.error('native atob/btoa present'); process.exit(1); }\"\n"
            "node node_modules/qunit/bin/qunit.js --version\n"
            "test -x node_modules/.bin/webpack\n"
            "test -x node_modules/.bin/zx\n"
            'test "$(readlink packages/core-js-builder/node_modules/core-js)" = "../../core-js"\n'
            'test "$(readlink packages/core-js-builder/node_modules/core-js-compat)" = "../../core-js-compat"\n'
            "node -e \"console.log('builder webpack ' + require(require.resolve('webpack/package.json', { paths: ['packages/core-js-builder'] })).version)\"\n"
            "test -s packages/core-js-compat/modules.json\n"
            "test -s tests/bundles/tests.js\n"
            "test -s tests/bundles/pure.js\n"
            "node -e \"require('./packages/core-js-bundle/index.js'); if (typeof Array.prototype.at !== 'function') process.exit(1);\"\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_BUILD}"
            f"{_TEST_CMD}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_BUILD}"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_BUILD}"
            f"{_TEST_CMD}"
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

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("zloirock", "core-js")
class CoreJs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if pr.number > ERA_MAX_PR:
            raise ValueError(
                f"zloirock/core-js #{pr.number}: this config covers the lerna + webpack "
                f"layout (PRs <= {ERA_MAX_PR}) only"
            )
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CoreJsImageDefault(self.pr, self._config)

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

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        bundle_re = re.compile(r"^=== core-js qunit bundle: (\S+) ===$")
        result_re = re.compile(r"^(ok|not ok) \d+ (?:# (SKIP|TODO) )?(.*\S)$")

        bundle = None
        seen: dict[str, int] = {}
        for line in log.splitlines():
            line = line.rstrip()
            m = bundle_re.match(line)
            if m:
                bundle = m.group(1)
                continue
            if bundle is None:
                continue
            m = result_re.match(line)
            if not m:
                continue
            status, directive, title = m.groups()
            name = f"{bundle} > {title}"
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} #{seen[name]}"
            if directive:
                skipped_tests.add(name)
            elif status == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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
