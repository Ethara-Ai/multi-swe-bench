import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ERA_RANGE = "451-to-268"

_NODE_IMAGE = "node:12-bullseye"


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

    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_ERA_RANGE}"

    def workdir(self) -> str:
        return f"base-{_ERA_RANGE}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        from multi_swe_bench.harness.image import DockerfileEnhancer

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/"
                f"{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        raw = f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""

        class _Shim:
            pr = self.pr

            @staticmethod
            def dependency():
                return image_name

            @staticmethod
            def dockerfile():
                return raw

        enhanced = DockerfileEnhancer.enhance(_Shim())

        scrub_start = enhanced.find("RUN git reset --hard")
        cmd_start = enhanced.find('CMD ["/bin/bash"]')
        if scrub_start != -1 and cmd_start != -1 and scrub_start < cmd_start:
            enhanced = enhanced[:scrub_start] + enhanced[cmd_start:]
        return enhanced


_STAGE_HEADER = r"""set -eo pipefail
export CI=true

cd /home/__REPO__

"""


_APPLY_TEST = r"""git apply --whitespace=nowarn /home/test.patch
"""


_APPLY_FIX = r"""git apply --whitespace=nowarn /home/fix.patch
"""


_INSTALL_PATCH_DEPS = r"""MISSING_DEPS=$(node -e "var fs = require('fs'); var base = JSON.parse(require('child_process').execSync('git show HEAD:package.json')); var head = JSON.parse(fs.readFileSync('package.json', 'utf8')); var out = []; ['dependencies', 'devDependencies'].forEach(function (k) { var a = base[k] || {}; var b = head[k] || {}; Object.keys(b).forEach(function (n) { if (a[n] !== b[n] && !fs.existsSync('node_modules/' + n + '/package.json')) out.push(n + '@' + b[n]); }); }); process.stdout.write(out.join(' '));")
if [ -n "$MISSING_DEPS" ]; then
  for attempt in 1 2 3; do npm install --no-save --before="$(git log -1 --format=%cI HEAD)" --legacy-peer-deps --ignore-scripts --no-audit --no-fund $MISSING_DEPS && break; npm cache clean --force || true; done
fi

"""


_JEST_TWEAK = r"""node -e "var p=require('./package.json');p.jest=p.jest||{};p.jest.globals=Object.assign({},p.jest.globals,{'ts-jest':{diagnostics:false}});require('fs').writeFileSync('package.json',JSON.stringify(p,null,2)+'\n')"

"""


_RUN_TESTS_BODY = r"""set +e
set -uo pipefail

TEST_TIMEOUT="${HR_TEST_TIMEOUT:-900}"
MIN_RESULTS="${HR_MIN_RESULTS:-0}"

: > /tmp/hr-test.log
CI=true npx tsdx test --env=jsdom --verbose --ci \
  --testTimeout="${TEST_TIMEOUT}000" >> /tmp/hr-test.log 2>&1
rc=$?
cat /tmp/hr-test.log

results=$(grep -cE "^[[:space:]]*(✓|✔|√|✕|✗|✘|×|○)[[:space:]]+" /tmp/hr-test.log)
echo "HR RUNNER: jest rc=$rc, $results result lines collected"

if [ "$rc" -ge 2 ]; then
  echo "HR RUNNER: INFRASTRUCTURE FAILURE: jest exited $rc, which is not a test failure"
  echo "HR RUNNER: the results above are not trustworthy"
  exit "$rc"
fi

if [ "$results" -lt "$MIN_RESULTS" ]; then
  echo "HR RUNNER: INFRASTRUCTURE FAILURE: collected $results result lines, expected at least $MIN_RESULTS"
  exit 1
fi

exit 0
"""


_CHECK_GIT_CHANGES = """set -e

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


def _prepare_sh(pr: PullRequest) -> str:
    return "\n".join(
        [
            "set -e",
            "",
            f"cd /home/{pr.repo}",
            "git reset --hard",
            "git clean -fdx",
            "bash /home/check_git_changes.sh",
            f"git checkout --detach {pr.base.sha}",
            "bash /home/check_git_changes.sh",
            "",
            "BEFORE=$(git log -1 --format=%cI HEAD)",
            "for attempt in 1 2 3; do"
            " npm install --before=\"$BEFORE\" --legacy-peer-deps --ignore-scripts --no-audit --no-fund && break;"
            " npm cache clean --force || true;"
            " done",
            "",
            "rm -rf node_modules/@types/glob \\",
            "       node_modules/@types/minimatch \\",
            "       node_modules/@types/react-datepicker/node_modules/@types/react",
            "",
            "git checkout -- package.json",
            "",
            "node -e \"var path = require('path'); var fs = require('fs');"
            " var tsdxDir = path.dirname(require.resolve('tsdx/package.json'));"
            " require.resolve('jest/package.json', { paths: [tsdxDir] });"
            " require.resolve('react/package.json');"
            " require.resolve('enzyme/package.json');"
            " require.resolve('enzyme-adapter-react-16/package.json');"
            " if (!fs.existsSync('node_modules/cheerio/package.json')) process.exit(1);\"",
            "npx tsdx --version",
            "",
        ]
    )


_PR_SCRUB = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""


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

    def dependency(self) -> "ImageBase":
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stage = _STAGE_HEADER.replace("__REPO__", self.pr.repo)

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
                _CHECK_GIT_CHANGES,
            ),
            File(
                ".",
                "prepare.sh",
                _prepare_sh(self.pr),
            ),
            File(
                ".",
                "run.sh",
                stage
                + _INSTALL_PATCH_DEPS
                + _JEST_TWEAK
                + _RUN_TESTS_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                stage
                + _APPLY_TEST
                + _INSTALL_PATCH_DEPS
                + _JEST_TWEAK
                + _RUN_TESTS_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                stage
                + _APPLY_TEST
                + _APPLY_FIX
                + _INSTALL_PATCH_DEPS
                + _JEST_TWEAK
                + _RUN_TESTS_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{_PR_SCRUB}
{self.clear_env}

"""


@Instance.register("HospitalRun", "components_451_to_268")
class COMPONENTS_451_TO_268(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", log)

        file_re = re.compile(r"^\s*(?:PASS|FAIL)\s+(\S+\.(?:tsx|ts|jsx|js))")
        status_re = re.compile(
            r"^(?P<indent> *)(?P<mark>[✓✔√✕✗✘×○])"
            r"\s+(?P<name>.*?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
        )
        noise_prefixes = (
            "●",
            "at ",
            "Tests:",
            "Test Suites:",
            "Snapshots:",
            "Time:",
            "Ran all test suites",
            "console.",
            "> ",
            "PASS ",
            "FAIL ",
            "RUNS ",
        )

        current_file = ""
        hierarchy: list[str] = []

        for raw in clean.splitlines():
            line = raw.rstrip()

            file_match = file_re.match(line)
            if file_match:
                current_file = file_match.group(1)
                hierarchy = []
                continue

            if not line.strip():
                continue

            status_match = status_re.match(line)
            if status_match:
                name = status_match.group("name").strip()
                if name.startswith("skipped "):
                    name = name[len("skipped ") :]
                if not name:
                    continue
                parts = []
                if current_file:
                    parts.append(current_file)
                if hierarchy:
                    parts.append(" > ".join(hierarchy))
                parts.append(name)
                full_name = "::".join(parts)

                mark = status_match.group("mark")
                if mark in "✓✔√":
                    passed_tests.add(full_name)
                elif mark == "○":
                    skipped_tests.add(full_name)
                else:
                    failed_tests.add(full_name)
                continue

            stripped = line.strip()
            if any(stripped.startswith(p) for p in noise_prefixes):
                continue
            if not current_file:
                continue

            indent = len(line) - len(line.lstrip(" "))
            if indent < 2:
                continue
            level = indent // 2
            hierarchy = hierarchy[: level - 1]
            hierarchy.append(stripped)

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
